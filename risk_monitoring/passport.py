"""Сборка ``ai_analysis`` для внешней БД: AI-паспорт документа и общий формат рисков.

Чистые функции без сети и БД — их покрывают тесты ``tests/test_rm_passport.py``.

Формат согласован с паспортом документа во внешней системе (ветка ``feature/rm-document-passport``):
интерфейс читает ``doc_type``, ``readability``, ``analyzed_at``, ``model``, ``resume``, ``confidence``,
``raw_data`` (с ``evidence``), ``total_doc_risk``, ``risks[]`` (``rule_id``, ``title``, ``description``,
``level``, ``confidence``, ``law``, ``evidence``, ``verification_needed``) и ``similar_doc_ids[]``
(``id``, ``similarity``). Остальные блоки (``passport``, ``version_changes``, ``risk_profile``,
``pipeline``) — расширение по концепции риск-мониторинга (гл. 6.4.6.10, 18.4–18.12); интерфейс
их пока не показывает и собирает в ``unknown_keys``.

Эмбеддинги во внешнюю БД не отправляются (хранятся в Postgres сервиса, ``ai_document_chunks``).
"""
import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from risk_monitoring import risk_catalog

SCHEMA_VERSION_FILE = "rm-file-1"
SCHEMA_VERSION_XML = "rm-xml-1"

MAX_EVIDENCE_PER_RISK = 5


def now_iso() -> str:
    """Текущее время в ISO-формате (секунды, локальное время сервиса)."""
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def clamp01(value: Any) -> Optional[float]:
    """Приводит число к диапазону 0–1; проценты (> 1) делит на 100.

    Args:
        value: Число, строка с числом или ``None``.

    Returns:
        Optional[float]: Значение 0–1 (4 знака) или ``None``.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v > 1:
        v = v / 100.0
    return round(min(1.0, max(0.0, v)), 4)


def pipeline_block(stage: str, status: str, model: Optional[str], prompt_versions: Optional[Dict[str, str]] = None,
                   previous_scope_id: Optional[int] = None, debug: bool = False, attempt: int = 1,
                   result: Optional[str] = None, error: Optional[str] = None,
                   export_date: Optional[str] = None) -> Dict[str, Any]:
    """Служебный блок ``pipeline``: по нему пайплайн понимает, что документ уже проанализирован.

    Args:
        stage: Этап (``S1a`` — XML-событие, ``S1b`` — файл).
        status: ``completed`` / ``failed`` / ``skipped``.
        model: Модель LLM.
        prompt_versions: Версии промптов по видам анализа.
        previous_scope_id: id предыдущей версии документа (файла или XML), если она найдена.
        debug: Повторный анализ в режиме отладки.
        attempt: Номер попытки.
        result: Уточнение результата (``no_text`` и т. п.).
        error: Текст ошибки для ``failed``.
        export_date: Дата выгрузки ЕИС (для ночного прогона).

    Returns:
        Dict[str, Any]: Блок ``pipeline``.
    """
    block = {
        "schema_version": SCHEMA_VERSION_FILE if stage == "S1b" else SCHEMA_VERSION_XML,
        "catalog_version": risk_catalog.CATALOG_VERSION,
        "stage": stage,
        "status": status,
        "attempt": attempt,
        "debug": bool(debug),
        "previous_scope_id": previous_scope_id,
        "model": model,
        "prompt_versions": prompt_versions or {},
        "analyzed_at": now_iso(),
    }
    if result:
        block["result"] = result
    if error:
        block["error"] = error[:500]
    if export_date:
        block["export_date"] = export_date
    return block


# ------------------------------------------------------------------------------ риски

def to_external_risks(compact_risks: Iterable[Dict[str, Any]], source: str = "ai_file") -> List[Dict[str, Any]]:
    """Переводит риски ``DocumentRiskAnalyzer.to_compact`` во внешний формат ``risks[]``.

    Несколько фрагментов одного кода объединяются в один риск: во внешней БД уникальность
    по ``(file_id, rule_id)``. Уровень — максимальный, цитаты — списком (до 5).

    Args:
        compact_risks: Риски с полями ``code``, ``severity``, ``title``, ``explanation``, ``fragment``,
            ``section``, ``page``, ``confidence``, ``law``, ``verification_needed``.
        source: Источник риска (``ai_file``, ``version_diff``, ``rule``, ``ai_event``).

    Returns:
        List[Dict[str, Any]]: Риски во внешнем формате.
    """
    items = []
    for r in compact_risks or []:
        code = r.get("code") or r.get("rule_id")
        if not code:
            continue
        level = risk_catalog.SEVERITY_LEVEL.get(r.get("severity"), clamp01(r.get("level")) or 0.3)
        items.append({
            "rule_id": code,
            "title": r.get("title") or risk_catalog.title(code),
            "description": r.get("explanation") or r.get("description") or "",
            "level": level,
            "confidence": clamp01(r.get("confidence")) or 0.0,
            "law": r.get("law") or risk_catalog.default_law(code),
            "evidence": [{
                "fragment": r.get("fragment"),
                "section": r.get("section"),
                "page": r.get("page"),
                "confidence": clamp01(r.get("confidence")),
                "quote_verified": True,
            }] if r.get("fragment") else list(r.get("evidence") or []),
            "verification_needed": list(r.get("verification_needed") or []),
            "severity": r.get("severity"),
            "source": source,
        })
    return merge_risks(items)


def merge_risks(*lists: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Объединяет списки рисков по ``rule_id``.

    Уровень и уверенность — максимум; описания — первое непустое (остальные не теряются в evidence);
    цитаты и действия эксперта — объединение без повторов; ``sources`` — все источники риска.

    Args:
        *lists: Списки рисков во внешнем формате.

    Returns:
        List[Dict[str, Any]]: По одному риску на код, по убыванию ``level × confidence``.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for lst in lists:
        for r in lst or []:
            code = r.get("rule_id")
            if not code:
                continue
            cur = merged.get(code)
            if cur is None:
                cur = dict(r)
                cur["category"] = risk_catalog.category(code)
                cur["evidence"] = list(r.get("evidence") or [])
                cur["verification_needed"] = list(r.get("verification_needed") or [])
                cur["sources"] = list(r.get("sources") or [])
                if r.get("source") and r["source"] not in cur["sources"]:
                    cur["sources"].append(r["source"])
                cur["title"] = r.get("title") or risk_catalog.title(code)
                cur["law"] = r.get("law") or risk_catalog.default_law(code)
                merged[code] = cur
                continue
            cur["level"] = max(cur.get("level") or 0, r.get("level") or 0)
            cur["confidence"] = max(cur.get("confidence") or 0, r.get("confidence") or 0)
            if not cur.get("description") and r.get("description"):
                cur["description"] = r["description"]
            if not cur.get("law") and r.get("law"):
                cur["law"] = r["law"]
            for ev in r.get("evidence") or []:
                if ev not in cur["evidence"]:
                    cur["evidence"].append(ev)
            for v in r.get("verification_needed") or []:
                if v not in cur["verification_needed"]:
                    cur["verification_needed"].append(v)
            for src in list(r.get("sources") or []) + ([r["source"]] if r.get("source") else []):
                if src not in cur["sources"]:
                    cur["sources"].append(src)
    out = list(merged.values())
    for r in out:
        r["evidence"] = r["evidence"][:MAX_EVIDENCE_PER_RISK]
        r.pop("source", None)
    out.sort(key=lambda r: (r.get("level") or 0) * (r.get("confidence") or 0), reverse=True)
    return out


def total_risk(risks: Sequence[Dict[str, Any]]) -> float:
    """Интегральный риск документа 0–1: «шумовое ИЛИ» по рискам.

    ``1 − Π(1 − level × confidence)``: один серьёзный риск даёт высокий балл, а пустые категории
    его не занижают (в отличие от среднего по категориям в старом ``FileProcessor``).

    Args:
        risks: Риски во внешнем формате.

    Returns:
        float: Балл 0–1 (4 знака).
    """
    p = 1.0
    for r in risks or []:
        w = (clamp01(r.get("level")) or 0.0) * (clamp01(r.get("confidence")) or 0.0)
        p *= (1.0 - w)
    return round(1.0 - p, 4)


def risk_profile(risks: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Риск-профиль по категориям (концепция, гл. 10.3): число рисков, максимальный уровень, балл.

    Args:
        risks: Риски во внешнем формате.

    Returns:
        Dict[str, Dict[str, Any]]: ``{"DOC": {"count": 2, "max_level": 0.9, "score": 0.83}, ...}``.
    """
    prof: Dict[str, Dict[str, Any]] = {}
    for r in risks or []:
        cat = risk_catalog.category(r.get("rule_id", ""))
        prof.setdefault(cat, {"count": 0, "max_level": 0.0, "_risks": []})
        prof[cat]["count"] += 1
        prof[cat]["max_level"] = max(prof[cat]["max_level"], clamp01(r.get("level")) or 0.0)
        prof[cat]["_risks"].append(r)
    for cat, v in prof.items():
        v["score"] = total_risk(v.pop("_risks"))
    return prof


def risks_comparison(previous_codes: Optional[Iterable[str]], current_codes: Iterable[str]) -> Optional[Dict[str, List[str]]]:
    """Сопоставление рисков новой версии документа с предыдущей.

    Для внешней системы: риск, который был в предыдущей версии и не выявлен в новой, считается
    снятым (``not_found_in_new_version``); общий — переходит в новую версию с работой аналитика.

    Args:
        previous_codes: Коды рисков предыдущей версии (``None`` — предыдущей версии нет).
        current_codes: Коды рисков новой версии.

    Returns:
        Optional[Dict[str, List[str]]]: ``persisting``, ``new``, ``not_found_in_new_version`` или ``None``.
    """
    if previous_codes is None:
        return None
    prev, cur = set(c for c in previous_codes if c), set(c for c in current_codes if c)
    return {
        "persisting": sorted(prev & cur),
        "new": sorted(cur - prev),
        "not_found_in_new_version": sorted(prev - cur),
    }


# ------------------------------------------------------------------- прочие блоки паспорта

def similar_doc_ids(similar: Sequence[Dict[str, Any]], self_file_id: Optional[int] = None,
                    limit: int = 10) -> List[Dict[str, Any]]:
    """Похожие документы в формате паспорта: ``[{"id": file_id, "similarity": 0.93, ...}]``.

    ``AIRepo.find_similar_documents`` возвращает документы ``ai_documents`` со списком внешних
    файлов; паспорту нужны id файлов. Один документ может соответствовать нескольким файлам
    (одинаковое содержимое в разных закупках) — каждый файл выводится отдельно.

    Args:
        similar: Результат поиска похожих (``external_file_ids``, ``avg_similarity``, ``coverage`` …).
        self_file_id: id анализируемого файла (исключается).
        limit: Максимум строк.

    Returns:
        List[Dict[str, Any]]: По убыванию схожести, без повторов.
    """
    out: Dict[int, Dict[str, Any]] = {}
    for s in similar or []:
        sim = s.get("avg_similarity", s.get("similarity"))
        for fid in s.get("external_file_ids") or []:
            if fid is None or (self_file_id is not None and int(fid) == int(self_file_id)):
                continue
            row = {
                "id": int(fid),
                "similarity": round(float(sim), 4) if sim is not None else None,
                "coverage": s.get("coverage"),
                "near_duplicate": bool(s.get("near_duplicate")),
                "exact_text_duplicate": bool(s.get("exact_text_duplicate")),
            }
            if int(fid) not in out or (row["similarity"] or 0) > (out[int(fid)]["similarity"] or 0):
                out[int(fid)] = row
    rows = sorted(out.values(), key=lambda r: r["similarity"] or 0, reverse=True)
    return rows[:limit]


def readability_block(extraction: Optional[Dict[str, Any]], pages: Optional[int], text_chars: int,
                      ocr_used: Optional[bool] = None) -> Dict[str, Any]:
    """Блок ``readability`` паспорта: оценка модели + объём текста и число страниц.

    Args:
        extraction: Результат ``DataExtractor`` (поле ``readability``).
        pages: Число страниц по разметке ``FileReader`` (``None`` для форматов без страниц).
        text_chars: Длина извлечённого текста.
        ocr_used: Применялось ли распознавание (``Страница N (OCR)`` в тексте).

    Returns:
        Dict[str, Any]: ``status``, ``readability_score``, ``main_language``, ``pages``, ``text_chars``, ``ocr`` …
    """
    rd = dict((extraction or {}).get("readability") or {})
    rd["pages"] = pages
    rd["text_chars"] = int(text_chars or 0)
    if ocr_used is not None:
        rd["ocr"] = bool(ocr_used)
    return rd


def doc_type_block(type_info: Optional[Dict[str, Any]], type_decode: Optional[str]) -> Dict[str, Any]:
    """Блок ``doc_type``: код типа, расшифровка, уверенность, признаки.

    Args:
        type_info: Ответ ``TypeDetector`` (``detected_type``, ``confidence``, ``issues``, ``key_indicators``).
        type_decode: Название типа по справочнику.

    Returns:
        Dict[str, Any]: Блок для паспорта.
    """
    ti = dict(type_info or {})
    return {
        "detected_type": ti.get("detected_type") or "unknown",
        "type_decode": type_decode or ti.get("type_decode") or "Неизвестный документ",
        "confidence": clamp01(ti.get("confidence")),
        "issues": ti.get("issues") or [],
        "key_indicators": ti.get("key_indicators") or [],
    }


def passport_block(profile: Optional[Dict[str, Any]], extraction: Optional[Dict[str, Any]],
                   file_meta: Dict[str, Any], purchase: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Расширенный AI-паспорт (концепция, гл. 6.4.6.10, 18.4): общая информация, структура,
    семантика, технические объекты, итоговая оценка.

    Args:
        profile: Ответ ``DocumentProfiler`` (может быть ``None`` при сбое LLM).
        extraction: Результат ``DataExtractor``.
        file_meta: Строка ``risk_monitoring_files`` (тип владельца, версия XML, размер …).
        purchase: Паспорт закупки (номер, предмет, способ …).

    Returns:
        Dict[str, Any]: Блок ``passport``.
    """
    pr = profile or {}
    raw = (extraction or {}).get("raw_data") or {}
    return {
        "general": {
            "file_name": file_meta.get("file_name"),
            "file_type": file_meta.get("file_type"),
            "file_size": file_meta.get("file_size"),
            "owner_type": file_meta.get("owner_type"),
            "owner_number": file_meta.get("owner_number"),
            "eis_version": file_meta.get("eis_version"),
            "xml_source_type": file_meta.get("xml_source_type"),
            "document_name": (raw.get("document_name") or {}).get("value") if isinstance(raw.get("document_name"), dict) else raw.get("document_name"),
            "purchase": purchase or None,
        },
        "purpose": pr.get("purpose"),
        "structure": {
            "main_sections": pr.get("main_sections") or [],
            **(pr.get("structure") or {}),
        },
        "key_conditions": pr.get("key_conditions") or [],
        "semantics": {
            "topics": pr.get("topics") or [],
            "keywords": pr.get("keywords") or [],
            "classification": pr.get("classification") or {},
        },
        "technical_objects": pr.get("technical_objects") or {"brands": [], "models": [], "manufacturers": [], "standards": []},
        "assessment": pr.get("assessment") or {},
        "recommendations": pr.get("recommendations") or [],
        "confidence": clamp01(pr.get("confidence")),
    }


def build_file_analysis(*, file_meta: Dict[str, Any], type_info: Optional[Dict[str, Any]], type_decode: Optional[str],
                        extraction: Optional[Dict[str, Any]], profile: Optional[Dict[str, Any]],
                        risk_compact: Optional[Dict[str, Any]], extra_risks: Sequence[Dict[str, Any]],
                        similar: Sequence[Dict[str, Any]], version_changes: Optional[Dict[str, Any]],
                        previous_codes: Optional[Iterable[str]], pages: Optional[int], text_chars: int,
                        ocr_used: Optional[bool], purchase: Optional[Dict[str, Any]], pipeline: Dict[str, Any],
                        model: Optional[str], content_sha256: Optional[str]) -> Dict[str, Any]:
    """Собирает ``ai_analysis`` файла (паспорт документа).

    Args:
        file_meta: Строка ``risk_monitoring_files``.
        type_info: Ответ ``TypeDetector``.
        type_decode: Название типа документа.
        extraction: Ответ ``DataExtractor`` (``readability``, ``raw_data``).
        profile: Ответ ``DocumentProfiler``.
        risk_compact: ``DocumentRiskAnalyzer.to_compact``.
        extra_risks: Дополнительные риски во внешнем формате (сравнение редакций).
        similar: Похожие документы (``AIRepo``).
        version_changes: Результат сравнения с предыдущей версией (или ``None``).
        previous_codes: Коды рисков предыдущей версии (``None`` — её нет).
        pages: Число страниц.
        text_chars: Длина текста.
        ocr_used: Применялось ли распознавание.
        purchase: Паспорт закупки.
        pipeline: Блок ``pipeline``.
        model: Модель LLM.
        content_sha256: SHA-256 содержимого файла.

    Returns:
        Dict[str, Any]: ``ai_analysis`` для ``PATCH /api/risk-monitoring/ai-analysis``.
    """
    rc = risk_compact or {}
    risks = merge_risks(to_external_risks(rc.get("risks") or [], "ai_file"), extra_risks or [])
    pr = profile or {}
    raw = (extraction or {}).get("raw_data") or {}
    resume = pr.get("resume") or raw.get("summary") or rc.get("summary") or ""
    confidence = clamp01(pr.get("confidence"))
    if confidence is None:
        confidence = clamp01((type_info or {}).get("confidence"))
    result = {
        "schema_version": SCHEMA_VERSION_FILE,
        "status": "completed",
        "doc_type": doc_type_block(type_info, type_decode),
        "readability": readability_block(extraction, pages, text_chars, ocr_used),
        "raw_data": raw,
        "resume": resume,
        "confidence": confidence if confidence is not None else 0.0,
        "risks": risks,
        "total_doc_risk": total_risk(risks),
        "risk_profile": risk_profile(risks),
        "risk_summary": rc.get("summary"),
        "risks_comparison": risks_comparison(previous_codes, [r["rule_id"] for r in risks]),
        "similar_doc_ids": similar_doc_ids(similar, file_meta.get("id")),
        "passport": passport_block(profile, extraction, file_meta, purchase),
        "version_changes": version_changes,
        "content_sha256": content_sha256,
        "model": model,
        "analyzed_at": now_iso(),
        "pipeline": pipeline,
    }
    return result


def no_text_analysis(file_meta: Dict[str, Any], reason: str, pipeline: Dict[str, Any], model: Optional[str],
                     content_sha256: Optional[str] = None) -> Dict[str, Any]:
    """``ai_analysis`` для документа, из которого не удалось извлечь текст.

    Это законченный результат (``pipeline.status = completed``, ``result = no_text``): файл не берётся
    в анализ повторно, в паспорте видно, почему нет анализа.

    Args:
        file_meta: Строка ``risk_monitoring_files``.
        reason: Причина.
        pipeline: Блок ``pipeline``.
        model: Модель LLM.
        content_sha256: SHA-256 содержимого.

    Returns:
        Dict[str, Any]: ``ai_analysis``.
    """
    return {
        "schema_version": SCHEMA_VERSION_FILE,
        "status": "completed",
        "doc_type": {"detected_type": "unknown", "type_decode": "Неизвестный документ", "confidence": None,
                     "issues": [reason]},
        "readability": {"status": "deny", "issues": [reason], "readability_score": 0, "pages": None, "text_chars": 0},
        "raw_data": {},
        "resume": reason,
        "confidence": 0.0,
        "risks": [],
        "total_doc_risk": 0.0,
        "similar_doc_ids": [],
        "content_sha256": content_sha256,
        "model": model,
        "analyzed_at": now_iso(),
        "pipeline": pipeline,
        "file_name": file_meta.get("file_name"),
    }
