"""Сводное экспертное заключение (этап 5): сборка ``data`` из фактов хранилища знаний.

Принципы:

* ``data`` содержит **ровно ключи формы** из справочника ``pe_form_fields`` (ключи не хардкодятся);
  значение, которое нельзя подтвердить доказательством, не ставится (``None``) и остаётся эксперту;
* доказательства (документ, страница, цитата, пояснение модели) лежат отдельно в ``trace``;
* блоки III–IV (``field3``, ``field4``) формулирует LLM только по **проверенным** результатам;
* тексты документов повторно не читаются: используются факты ``pe_facts`` и сохранённые тексты
  (для проверки цитат).

Модуль не зависит от сети: LLM и построение фактов передаются снаружи.
"""
import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from configs.config import Config
from configs.logger import get_logger
from knowledge_store import assessment, eis_notice, facts as facts_mod, forms, repository as repo

logger = get_logger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "summary_blocks_prompt.txt"
RESULT_KINDS = (forms.KIND_PRESENCE, forms.KIND_COMPLIANCE)
TEXT_BLOCK_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 4500}},
    "required": ["text"],
}
BLOCK_TITLES = {"field3": "вывод", "field4": "заключение"}  # «Блок III. Вывод», «Блок IV. Заключение»

LlmCall = Callable[[List[dict], dict], Awaitable[str]]
FactsBuilder = Callable[[], Awaitable[Any]]


class SummaryError(Exception):
    """Сводное ЭЗ собрать нельзя; ``http_status`` и ``message`` пригодны для ответа API.

    Attributes:
        http_status: Предлагаемый HTTP-статус ответа (409 — не хватает данных, 422 — форма не поддерживается).
        message: Сообщение для пользователя.
    """

    http_status = 409

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NoDataError(SummaryError):
    """По экспертизе нет документов в хранилище знаний."""


class TextPurgedError(SummaryError):
    """Тексты документов уже очищены политикой хранения."""


class UnsupportedFormError(SummaryError):
    """Для экспертизы нет формы заключения в справочнике."""

    http_status = 422


class NoFactsError(SummaryError):
    """Факты ещё не построены."""


def _as_dict(value: Any) -> Optional[dict]:
    """JSONB из asyncpg приходит строкой — приводит к ``dict`` (``None`` для пустого/непонятного)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def normalize_result(value: Any) -> Optional[int]:
    """Приводит значение критерия к ``0``/``1``/``2`` (в данных встречаются и числа, и строки).

    Args:
        value: Значение из факта.

    Returns:
        Optional[int]: ``0``, ``1`` или ``2``; ``None``, если значение не распознано.
    """
    if isinstance(value, bool):
        return int(value)
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number in (0, 1, 2) else None


_WEAK_QUOTE_RE = re.compile(r"^(?:.{0,90}\s)?(?:не\s+требуется|не\s+установлен\w*|не\s+предусмотрен\w*|информация\s+отсутствует)\W*$",
                            re.IGNORECASE | re.DOTALL)


def weak_quote(quote: Any) -> bool:
    """Цитата слишком общая, чтобы подтверждать отсутствие сведений.

    Фразы вроде «Информация отсутствует» или «Обеспечение … не требуется» встречаются в печатной форме
    у любого пустого раздела и сами по себе не доказывают, что именно требуемой сведения нет: значение «0»
    с такой цитатой ставится как предложение модели (``proposed``), а не как проверенное. Пустая цитата
    для «0» допустима (отсутствие нечем процитировать) и слабой не считается.
    """
    text = re.sub(r"\s+", " ", str(quote or "")).strip()
    return bool(text) and bool(_WEAK_QUOTE_RE.match(text))


def evidence_ok(fact: dict, doc_text: Optional[str]) -> bool:
    """Проверяет доказательство факта (валидатор 5.4).

    Факты из XML извещения (``eis_xml``) считаются доказанными структурой XML. Для остальных ответ «1»
    требует цитаты, которая дословно есть в тексте документа; для «0»/«2» цитата необязательна, но
    если она указана, то тоже должна быть в тексте. Значение без подтверждения ставится в заключение как
    предложение модели (статус ``proposed`` в ``trace``) — см. :func:`assemble`.

    Args:
        fact: Строка ``pe_facts`` (``dict``) с уже разобранным ``value``.
        doc_text: Полный текст документа, на который ссылается факт (``None`` — документа нет).

    Returns:
        bool: ``True``, если значение можно поставить в заключение.
    """
    if fact.get("source") == facts_mod.SOURCE_EIS:
        return True
    quote = (fact.get("quote") or "").strip()
    if not quote:
        return fact["result"] != 1
    return bool(doc_text) and facts_mod.quote_in_text(quote, doc_text)


METHOD_STEMS = {"нормативн": "нормативный", "затратн": "затратный", "тарифн": "тарифный",
                "проектно-сметн": "проектно-сметный", "рыночн": "сопоставления рыночных цен"}


def used_nmck_method(documents: Dict[int, dict]) -> Tuple[Optional[str], Optional[str]]:
    """Метод расчёта НМЦК, указанный в документах («Используемый метод определения НМЦК…»).

    Args:
        documents: ``document_id`` → описание документа с полем ``text``.

    Returns:
        Tuple[Optional[str], Optional[str]]: ``(ключ метода из METHOD_STEMS, найденная строка)``; ``(None, None)``,
        если метод в текстах не назван.
    """
    for doc in documents.values():
        match = re.search(r"определения\s+Н\(?М?\)?ЦК[^\n]{0,300}", doc.get("text") or "", re.I)
        if not match:
            continue
        snippet = match.group(0)
        for stem in METHOD_STEMS:
            if stem in snippet.lower() or (stem == "рыночн" and "анализ рынка" in snippet.lower()):
                return stem, snippet.strip()
    return None, None


def apply_nmck_method_rule(fields: Sequence[dict], documents: Dict[int, dict], data: Dict[str, Any],
                           trace: Dict[str, Any]) -> None:
    """Критерии «расчёт НМЦК … методом» при другом применённом методе — «2» (не применимо).

    Методы взаимоисключающие, поэтому критерии про неприменённые методы получают ``2`` детерминированно
    (модель в этих случаях ошибочно ставит «0» и порождает ложные замечания). Критерий применённого метода
    остаётся за моделью. Изменяет ``data``/``trace`` на месте.

    Args:
        fields: Поля формы.
        documents: Документы экспертизы с текстами.
        data: ``data`` (изменяется на месте).
        trace: ``trace`` (изменяется на месте).
    """
    used, snippet = used_nmck_method(documents)
    if not used:
        return
    for field in fields:
        match = re.search(r"расчета\s+НМЦК\s+([\w-]+)\s+методом", field.get("label") or "", re.I)
        if not match or field["field_key"] not in data or field["value_kind"] not in RESULT_KINDS:
            continue          # у текстового поля `*_text` та же подпись, что у критерия — его не трогаем
        stem = next((k for k in METHOD_STEMS if match.group(1).lower().startswith(k)), None)
        if stem and stem != used:
            data[field["field_key"]] = 2
            trace[field["field_key"]] = {"status": "derived", "rule": f"в документах применён метод «{METHOD_STEMS[used]}»; "
                                         "другие методы расчёта НМЦК не применимы", "quote": snippet[:300]}


def section_result(children: Sequence[Optional[int]]) -> Optional[int]:
    """Итог подраздела по его критериям: есть «0» → ``0``; все решены → ``1``; иначе ``None``.

    Args:
        children: Значения дочерних критериев (``None`` — не решён).

    Returns:
        Optional[int]: Итог подраздела.
    """
    if any(v == 0 for v in children):
        return 0
    if children and all(v is not None for v in children):
        return 1
    return None


def numbered(items: Sequence[str]) -> str:
    """Нумерованный перечень («1. …»), как пишут эксперты в полях ``*_text``."""
    return "\n".join(f"{i}. {text}" for i, text in enumerate(items, 1))


_FRAGMENT_FORMS = {"фрагмент": "документ", "фрагмента": "документа", "фрагменту": "документу", "фрагментом": "документом",
                   "фрагменте": "документе", "фрагменты": "документы", "фрагментов": "документов",
                   "фрагментам": "документам", "фрагментами": "документами", "фрагментах": "документах"}


def defragment(text: Optional[str]) -> Optional[str]:
    """Заменяет слово «фрагмент» (внутренний термин поиска) на «документ» в нужном падеже.

    «В предоставленных фрагментах отсутствует…» → «В предоставленных документах отсутствует…»;
    «Фрагменты не содержат…» → «Документы не содержат…». Регистр первой буквы сохраняется.

    Args:
        text: Любой текст заключения.

    Returns:
        Optional[str]: Текст без слова «фрагмент».
    """
    if not text:
        return text

    def repl(m: "re.Match") -> str:
        """Подставляет форму слова «документ» с тем же регистром первой буквы."""
        word = m.group(0)
        new = _FRAGMENT_FORMS.get(word.lower(), "документ")
        return new.capitalize() if word[:1].isupper() else new

    return re.sub(r"(?i)\bфрагмент(?:ов|ами|ах|ам|ы|а|у|ом|е)?\b", repl, text)


def clean_comment(comment: Optional[str], filename: Optional[str] = None) -> Optional[str]:
    """Убирает из пояснения модели ссылки на «фрагменты» (их нумерация эксперту ничего не говорит).

    «В фрагменте 6 указано…» → «В документе «имя» указано…» (или «В документации указано…»).

    Args:
        comment: Пояснение модели.
        filename: Имя документа факта.

    Returns:
        Optional[str]: Очищенный текст.
    """
    if not comment:
        return comment
    name = f"«{filename}»" if filename else ""

    def repl(m: "re.Match") -> str:
        """«В фрагменте N» → «В документе «имя»»; «из фрагмента N» → «из документа «имя»»."""
        prep = m.group(1)
        if prep.lower() == "в":
            return f"{prep} {'документе ' + name if name else 'документации'}".strip()
        return f"{prep} {'документа ' + name if name else 'документации'}".strip()

    text = re.sub(r"(?i)\b(в|из)\s+фрагмент(?:е|ах|ов|а)\s*\d+(?:\s*(?:,|и|-)\s*\d+)*", repl, comment)
    text = re.sub(r"(?i)\bфрагмент(?:е|ах|ов|а)?\s*\d+", "документ", text)
    return defragment(text).strip()


def assemble(fields: Sequence[dict], fact_rows: Sequence[dict], documents: Dict[int, dict],
             procurement: Optional[dict] = None, preset: Optional[Dict[str, Tuple[Any, dict]]] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Собирает ``data`` и ``trace`` по полям формы (без LLM).

    Args:
        fields: Поля формы (``pe_form_fields``), в порядке ``ordinal``.
        fact_rows: Факты экспертизы (``pe_facts``); для одного ключа берётся последний.
        documents: ``document_id`` → ``{"filename", "doc_code", "text"}``.
        procurement: Паспорт закупки (``nmck``, ``advance``) для числовых полей общих сведений.
        preset: Готовые значения полей, оценённых моделью по документам (:func:`assessment.compute_assessed`):
            ``ключ → (значение, запись trace)``; они ставятся до расчёта итогов подразделов.

    Returns:
        Tuple[dict, dict]: ``(data, trace)``. ``data`` содержит каждый ключ формы (``None`` — оставлено
        эксперту); ``trace[key]`` — ``status`` (``verified`` / ``unverified`` / ``no_fact`` / ``derived`` /
        ``from_passport``), источник, документ, страница, цитата, пояснение.
    """
    by_key: Dict[str, dict] = {}
    for row in fact_rows:
        value = _as_dict(row.get("value")) or {}
        prev = by_key.get(row["fact_key"])
        if prev and prev.get("source") == facts_mod.SOURCE_EIS and row.get("source") != facts_mod.SOURCE_EIS:
            continue          # детерминированный факт XML/печатной формы важнее ответа модели
        by_key[row["fact_key"]] = {**row, "value_obj": value, "result": normalize_result(value.get("value")),
                                   "verified": bool(value.get("verified")), "comment": value.get("comment")}
    data: Dict[str, Any] = {f["field_key"]: None for f in fields}
    trace: Dict[str, Any] = {}
    kinds = {f["field_key"]: f["value_kind"] for f in fields}

    for field in fields:
        key, kind = field["field_key"], field["value_kind"]
        if kind not in RESULT_KINDS:
            continue
        fact = by_key.get(key)
        if not fact:
            trace[key] = {"status": "no_fact"}
            continue
        doc = documents.get(fact.get("document_id")) or {}
        entry = {"status": "unverified", "source": fact.get("source"), "document": doc.get("filename"),
                 "page": fact.get("page"), "quote": fact.get("quote"),
                 "comment": clean_comment(fact["comment"], doc.get("filename"))}
        result = fact["result"]
        proven = bool(fact["verified"]) and evidence_ok(fact, doc.get("text"))
        if result is None:
            pass
        elif fact.get("source") == facts_mod.SOURCE_EIS or (result == 1 and proven):
            tentative = fact.get("source") == facts_mod.SOURCE_EIS and fact["value_obj"].get("verified") is False
            data[key], entry["status"] = result, ("proposed" if tentative else "verified")
            if fact["value_obj"].get("origin"):
                entry["origin"] = fact["value_obj"]["origin"]
        else:
            # значение без подтверждённой цитаты («отсутствует» нечем процитировать; «1» модель не смогла
            # подтвердить) ставится как предложение модели: эксперт видит его в trace и проверяет
            generic = result == 0 and weak_quote(fact.get("quote"))
            data[key], entry["status"] = result, ("verified" if proven and not generic else "proposed")
            if generic:
                entry["note"] = "«0» подтверждён только общей фразой («информация отсутствует», «не требуется»): требует проверки экспертом"
            if not proven and fact["value_obj"].get("model_quote"):
                entry["model_quote"] = fact["value_obj"]["model_quote"]
        if fact["value_obj"].get("error"):
            entry["error"] = fact["value_obj"]["error"]
        if result is None and fact["value_obj"].get("value") == 3:
            entry["note"] = "по представленным документам определить нельзя — оставлено эксперту"
        trace[key] = entry

    for key, (value, entry) in (preset or {}).items():
        if key in data and (key not in assessment.FILL_IF_EMPTY or data[key] is None):
            if value is None:           # оценка не удалась: значение остаётся эксперту, причина видна в trace
                trace.setdefault(key, {})["assessment_error"] = entry.get("error")
            else:
                data[key], trace[key] = value, entry

    apply_nmck_method_rule(fields, documents, data, trace)

    # итоги подразделов — из решённых дочерних критериев
    for field in fields:
        key = field["field_key"]
        if field["value_kind"] != forms.KIND_SECTION:
            continue
        children = [data[k] for k, kind in kinds.items()
                    if kind in RESULT_KINDS and k.startswith(key + "_") and k[len(key) + 1:len(key) + 2].isdigit()]
        data[key] = section_result(children)
        trace[key] = {"status": "derived", "rule": "0 — есть несоответствие; 1 — все критерии решены; иначе эксперту"}

    # комментарии `<ключ>_text` — в стиле экспертов: нумерованный перечень существа замечаний
    labels = {f["field_key"]: f.get("label") or "" for f in fields}

    def children(base: str) -> List[str]:
        """Критерии-«листья» раздела/подраздела по префиксу ключа."""
        return [k for k in data if kinds.get(k) in RESULT_KINDS and k.startswith(base + "_")
                and k[len(base) + 1:len(base) + 2].isdigit()]

    for key in list(data):
        if not (key.endswith("_text") and kinds.get(key) == forms.KIND_TEXT):
            continue
        base = key[:-5]
        if base in kinds and kinds[base] in RESULT_KINDS:
            if data.get(base) != 0:
                continue
            comment = trace.get(base, {}).get("comment")
            if comment:
                data[key] = numbered([comment])
                trace[key] = {"status": "derived", "rule": f"пояснение к {base}"}
            continue
        kids = children(base)
        if not kids:
            continue
        bad = [k for k in kids if data[k] == 0 and trace.get(k, {}).get("comment")]
        if bad:
            lines = [f"{eis_notice.criterion_number(labels.get(k, '')) or ''} {trace[k]['comment']}".strip() for k in bad]
            head = ("По результатам экспертной оценки соответствия информации, представленной в извещении, "
                    "необходимо обратить внимание на следующие пункты:\n") if base == "field2_1" else ""
            data[key] = head + numbered(lines)
            trace[key] = {"status": "derived", "rule": f"замечания критериев {base}_*"}
        elif base == "field2_1" and all(data[k] is not None for k in kids):
            data[key] = "Информация, представленная в извещении, соответствует требованиям действующего законодательства Российской Федерации."
            trace[key] = {"status": "derived", "rule": "по критериям блока 1 замечаний нет"}

    fill_general_info(fields, by_key, documents, procurement or {}, data, trace)
    return data, trace


def _number(text: Any) -> Optional[float]:
    """Число из строки вида ``1 234 567,89`` или ``1234567.89``; ``None``, если числа нет."""
    match = re.search(r"\d[\d\s\u00a0]*(?:[.,]\d+)?", str(text or ""))
    if not match:
        return None
    try:
        return float(re.sub(r"[\s\u00a0]", "", match.group()).replace(",", "."))
    except ValueError:
        return None


def _extraction_raw(doc: dict) -> dict:
    """``raw_data`` результата ``TypeDataExtractor`` документа (``{}``, если нет)."""
    raw = (_as_dict(doc.get("extraction")) or {}).get("raw_data")
    return raw if isinstance(raw, dict) else {}


def fill_general_info(fields: Sequence[dict], by_key: Dict[str, dict], documents: Dict[int, dict],
                      procurement: dict, data: Dict[str, Any], trace: Dict[str, Any]) -> None:
    """Заполняет общие сведения (блок «Экспертное заключение» и блок I), изменяя ``data``/``trace`` на месте.

    Источники по убыванию надёжности: XML извещения ЕИС (факты ``eis_xml``: идентификационный код,
    наименование объекта, НМЦК, аванс), паспорт закупки, данные ``TypeDataExtractor`` документов
    (предмет закупки, НМЦК) — последние помечаются ``proposed``. «Состав комплекта документации» собирается
    из загруженных документов. Финансирование в документах не определяется — остаётся эксперту.

    Args:
        fields: Поля формы.
        by_key: Факты по ключам (с разобранным ``value_obj``).
        documents: ``document_id`` → описание документа (``filename``, ``doc_code``, ``extraction``).
        procurement: Паспорт закупки.
        data: ``data`` (изменяется на месте).
        trace: ``trace`` (изменяется на месте).
    """
    kinds = {f["field_key"]: f["value_kind"] for f in fields}

    def xml_value(number: str) -> Tuple[Optional[str], Optional[str]]:
        """Значение и путь из доказательства факта XML по номеру критерия (``путь = значение``)."""
        for f in fields:
            if eis_notice.criterion_number(f.get("label", "")) != number:
                continue
            fact = by_key.get(f["field_key"])
            if fact and fact.get("source") == facts_mod.SOURCE_EIS and fact["result"] == 1 and " = " in (fact.get("quote") or ""):
                path, value = fact["quote"].split(" = ", 1)
                return value.strip(), path
        return None, None

    def put(key: str, value: Any, status: str, source: str, quote: Optional[str] = None) -> None:
        """Записывает значение, если ключ есть в форме и ещё не заполнен."""
        if key in data and data[key] is None and value not in (None, ""):
            data[key] = value
            trace[key] = {"status": status, "source": source, "quote": (quote or "")[:300] or None}

    docs = list(documents.values())
    notice_docs = sorted(docs, key=lambda d: d.get("doc_code") != "docIzvejenieFiles")

    # XML извещения
    name, _ = xml_value("1.11")
    put("name", name, "verified", facts_mod.SOURCE_EIS, name)
    code, _ = xml_value("1.8")
    put("inn", code, "verified", facts_mod.SOURCE_EIS, code)
    price, _ = xml_value("1.18")
    put("field1_1", _number(price), "verified", facts_mod.SOURCE_EIS, price)
    advance, path = xml_value("1.22")
    if advance and _number(advance) is not None:
        put("field1_2", _number(advance), "verified", facts_mod.SOURCE_EIS, advance)
        if "field1_2_unit" in data and data["field1_2_unit"] is None:
            data["field1_2_unit"] = "%" if ("sumInPercents" in (path or "") or "%" in (path or "")) else "руб."
            trace["field1_2_unit"] = {"status": "derived", "rule": "единица по полю XML извещения"}

    # паспорт закупки
    for key, column in (("field1_1", "nmck"), ("field1_2", "advance")):
        if key in data and data[key] is None and procurement.get(column) is not None:
            data[key] = float(procurement[column])
            trace[key] = {"status": "from_passport", "source": "pe_procurements"}

    # данные, извлечённые из документов (предложение модели)
    for doc in notice_docs:
        raw = _extraction_raw(doc)
        subject = (raw.get("procurement_subject") or {}).get("description")
        put("name", (subject or "").strip()[:500], "proposed", doc.get("filename") or "extraction", subject)
        for item in raw.get("finances") or []:
            if isinstance(item, dict) and re.search(r"нмцк|начальн", str(item.get("context") or ""), re.I):
                put("field1_1", _number(item.get("value")), "proposed", doc.get("filename") or "extraction",
                    (item.get("evidence") or {}).get("fragment") if isinstance(item.get("evidence"), dict) else None)

    # шифр документа (`code`) — реестровый номер закупки в ЕИС (19 цифр): из имени файла или текста извещения
    for doc in notice_docs + docs:
        match = (re.search(r"(?<!\d)(0\d{18})(?!\d)", doc.get("filename") or "")
                 or re.search(r"(?<!\d)(0\d{18})(?!\d)", (doc.get("text") or "")[:20000]))
        if match:
            put("code", match.group(1), "derived", doc.get("filename") or "notice", match.group(1))
            break

    # наименование и идентификационный код из текста печатной формы извещения (если XML нет)
    for doc in notice_docs:
        text = doc.get("text") or ""
        match = re.search(r"Наименование объекта закупки[\s:|\-–—]*([^\n|]{5,300})", text, re.I)
        if match:
            put("name", match.group(1).strip(), "proposed", doc.get("filename") or "notice", match.group(0).strip())
        match = re.search(r"(?<!\d)(\d{36})(?!\d)", text)
        if match:
            put("inn", match.group(1), "proposed", doc.get("filename") or "notice", match.group(1))

    # состав комплекта документации
    if "documents" in data and data["documents"] is None and docs:
        try:
            from evaluate_documents.type_data_extractor import DOCUMENT_TYPE_MAPPING as titles
        except Exception:  # noqa: BLE001 — справочник названий нужен только для подписи
            titles = {}
        lines, seen = [], set()
        for doc in docs:
            line = f"{titles.get(doc.get('doc_code'), doc.get('doc_code') or 'Документ')}: {doc.get('filename') or '—'}"
            if line not in seen:
                seen.add(line)
                lines.append(line)
        data["documents"] = "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))
        trace["documents"] = {"status": "derived", "rule": "список загруженных документов экспертизы"}

    # прочие мета/числовые/текстовые поля — только из подтверждённых фактов
    for key, fact in by_key.items():
        if key in data and kinds.get(key) in (forms.KIND_META, forms.KIND_NUMBER, forms.KIND_TEXT) \
                and data[key] is None and fact["verified"] and fact["value_obj"].get("value") not in (None, ""):
            data[key] = fact["value_obj"]["value"]
            trace[key] = {"status": "verified", "source": fact.get("source"), "quote": fact.get("quote")}


def collect_remarks(fields: Sequence[dict], data: Dict[str, Any], trace: Dict[str, Any]) -> List[dict]:
    """Список проверенных замечаний (критерии со значением ``0``) для блоков III–IV.

    Args:
        fields: Поля формы.
        data: Собранный ``data``.
        trace: Собранный ``trace``.

    Returns:
        list[dict]: ``{"field_key", "criterion", "comment", "quote"}`` в порядке полей формы.
    """
    remarks = []
    for field in fields:
        key = field["field_key"]
        if field["value_kind"] in RESULT_KINDS and data.get(key) == 0:
            item = trace.get(key, {})
            remarks.append({"field_key": key, "number": eis_notice.criterion_number(field["label"]) or "",
                            "criterion": field["label"], "comment": item.get("comment"), "document": item.get("document"),
                            "quote": (item.get("quote") or "")[:300]})
    return remarks


# Структурированные ответы модели для блоков III–IV: короткие поля с ограничениями. Текст блока собирается кодом,
# поэтому «разгон» модели (десятки тысяч символов) невозможен.
FIELD3_SCHEMA = {
    "type": "object",
    "properties": {
        "notice": {"type": "string", "maxLength": 500},
        "items": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
            "type": "object",
            "properties": {"numbers": {"type": "string", "maxLength": 60}, "text": {"type": "string", "maxLength": 400}},
            "required": ["numbers", "text"]}},
    },
    "required": ["notice", "items"],
    "additionalProperties": False,
}
FIELD4_SCHEMA = {
    "type": "object",
    "properties": {
        "areas": {"type": "array", "minItems": 1, "maxItems": 6, "items": {
            "type": "object",
            "properties": {"area": {"type": "string", "maxLength": 80}, "summary": {"type": "string", "maxLength": 300}},
            "required": ["area", "summary"]}},
        "note": {"type": "string", "maxLength": 400},
        "recommendations": {"type": "array", "minItems": 1, "maxItems": 6, "items": {"type": "string", "minLength": 10, "maxLength": 260}},
    },
    "required": ["areas", "note", "recommendations"],
    "additionalProperties": False,
}
BLOCK_SCHEMAS = {"field3": FIELD3_SCHEMA, "field4": FIELD4_SCHEMA}
MANY_REMARKS = 8       # с такого числа замечаний заключение пишется развёрнуто (образец 2)


def _clip(text: Any, limit: int) -> str:
    """Строка без лишних пробелов, обрезанная по границе слова."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= limit:
        return value
    return value[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


_DANGLING = re.compile(r"(?:\s+(?:и|а|но|о|об|в|во|на|по|за|из|из-за|к|с|со|у|для|при|от|до|или|а также|что|как))+$", re.I)


def clip_text(text: Any, limit: int) -> str:
    """Текст для итогового заключения, обрезанный по границе предложения/запятой без «…» и «висячих» предлогов.

    Args:
        text: Исходный текст.
        limit: Максимальная длина.

    Returns:
        str: Строка не длиннее ``limit``; обрыв посреди фразы не оставляет многоточий и союзов в конце.
    """
    value = re.sub(r"\s+", " ", str(text or "")).strip().replace("…", "").strip()
    if len(value) <= limit:
        return _DANGLING.sub("", value).rstrip(" ,;:—-")
    cut = value[:limit]
    for sep in (". ", "; ", ", ", " "):
        pos = cut.rfind(sep)
        if pos >= limit * 0.5:
            cut = cut[:pos]
            break
    return _DANGLING.sub("", cut).rstrip(" ,;:—-")


def lower_first(text: str) -> str:
    """Первая буква строчная (для фраз после тире), если слово не аббревиатура."""
    return text[:1].lower() + text[1:] if text and not text[:2].isupper() else text


def close_truncated_json(raw: str) -> str:
    """Чинит обрезанный JSON без внешних зависимостей.

    Ответ модели обрывается по лимиту токенов посреди строки или пары «ключ: значение». Сначала пробуется
    дописать кавычку и закрывающие скобки; если так не разбирается, JSON обрезается по последней
    «безопасной» точке (после законченного значения) и закрывается.

    Args:
        raw: Обрезанный JSON.

    Returns:
        str: Текст, который разбирается ``json.loads`` (иначе ``ValueError`` при разборе).
    """
    text = raw or ""
    stack, in_str, esc, snapshots = [], False, False, []
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
            snapshots.append((i + 1, "".join(reversed(stack))))
        elif ch == ",":
            snapshots.append((i, "".join(reversed(stack))))
    candidates = [text + ('"' if in_str else "") + "".join(reversed(stack))]
    candidates += [text[:pos] + closers for pos, closers in reversed(snapshots)]
    for candidate in candidates:
        try:
            json.loads(candidate)
            return candidate
        except ValueError:
            continue
    return candidates[0]


def parse_block(raw: str) -> dict:
    """Разбирает ответ модели по блоку; обрезанный/«разогнавшийся» JSON чинится ``json_repair``.

    Args:
        raw: Сырой ответ модели.

    Returns:
        dict: Объект ответа.

    Raises:
        ValueError: Ответ не удалось превратить в объект.
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        try:
            data = json.loads(close_truncated_json(raw))
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"ответ модели не разобран: {type(e).__name__}") from e
    if not isinstance(data, dict):
        raise ValueError("ответ модели не объект")
    return unwrap_block(data)


def unwrap_block(data: dict) -> dict:
    """Снимает обёртку ``{"field3": {...}}`` / ``{"field4": {...}}``, которую модель добавляет по названию блока.

    Args:
        data: Разобранный ответ модели.

    Returns:
        dict: Содержимое блока (или исходный объект, если обёртки нет).
    """
    for wrapper in ("field3", "field4", "блок", "result"):
        inner = data.get(wrapper)
        if isinstance(inner, dict) and len(data) == 1:
            return inner
    return data


def _unique(items: Sequence[Any], key=lambda x: x) -> List[Any]:
    """Элементы без повторов (модель в «разгоне» повторяет один и тот же пункт)."""
    seen, out = set(), []
    for item in items:
        k = re.sub(r"\W+", " ", str(key(item))).strip().casefold()[:80]
        if k and k not in seen:
            seen.add(k)
            out.append(item)
    return out


def clean_notice(value: Any) -> str:
    """Суть замечаний к извещению без повтора вводной фразы и без оборванного хвоста «…» (модель дублирует «Информация… за исключением»)."""
    text = clip_text(value, 500)
    text = re.sub(r"^\s*Информация,? представленная в извещении[^,]*?(?:№\s*\d+)?[^,]*,?\s*соответствует требованиям законодательства,?\s*(?:за исключением:?)?\s*",
                  "", text, flags=re.I)
    text = re.sub(r"\s*(?:\.{3}|…)\.?$", "", text).strip()
    return lower_first(_DANGLING.sub("", text).rstrip(" ,;:"))


def clean_numbers(value: Any) -> str:
    """Номера критериев из ответа модели: только полные номера вида ``2.2.7.1`` (обрезанные «1.3…» отбрасываются)."""
    text = re.sub(r"[\d.]*(?:\.{3}|…).*$", "", str(value or ""))      # оборванный номер перед «…» тоже отбрасывается
    nums = [n for n in re.findall(r"\d+(?:\.\d+)+", text)]
    return ", ".join(nums[:8])


def prompt_for(prompt: str, key: str) -> str:
    """Промпт для одного блока: общая часть + раздел только этого блока (модель путала формат ``field3`` и ``field4``)."""
    parts = re.split(r"(?m)^(?=# )", prompt)
    own, other = ("«ВЫВОД»", "«ЗАКЛЮЧЕНИЕ»") if key == "field3" else ("«ЗАКЛЮЧЕНИЕ»", "«ВЫВОД»")
    kept = [p for p in parts if not (p.startswith("# БЛОК") and other in p.split("\n", 1)[0])]
    text = "".join(kept)
    lines = [ln for ln in text.split("\n")
             if not (ln.startswith("Для «") and ("(%s)" % key) not in ln)]
    return "\n".join(lines) + f"\n\nВерни СТРОГО формат блока {key}: плоский JSON без обёртки."


def render_block(key: str, parsed: dict, remarks: Sequence[dict], name: Optional[str], number: Optional[str],
                 procedure: Optional[str]) -> Optional[str]:
    """Собирает текст блока из структурированного ответа модели.

    * ``field3`` — абзац об извещении и строки ``<номера>. <суть>`` под «Выявлены несоответствия и недостатки по критериям:».
    * ``field4`` — «образец 1» (до :data:`MANY_REMARKS` замечаний: «…соответствуют, за исключением… Заказчику рекомендуется: …»)
      или «образец 2» (много замечаний: «…имеют недостатки… а именно: … Следует отметить… Заказчику рекомендуется: …»).

    Args:
        key: ``field3`` или ``field4``.
        parsed: Ответ модели по схеме :data:`BLOCK_SCHEMAS`.
        remarks: Проверенные замечания.
        name: Наименование объекта закупки.
        number: Реестровый номер закупки.
        procedure: Способ определения поставщика в родительном падеже.

    Returns:
        Optional[str]: Текст блока; ``None``, если ответ модели пуст.
    """
    num = f" № {number}" if number else ""
    if key == "field3":
        notice = clean_notice(parsed.get("notice"))
        items = _unique([i for i in parsed.get("items") or [] if isinstance(i, dict) and i.get("text")],
                        key=lambda i: i["text"])[:12]
        notice_remarks = [r for r in remarks if (r.get("number") or "").startswith("1.")]
        if notice_remarks and not notice:
            notice = "; ".join(_title(r["criterion"])[:90] for r in notice_remarks[:6])     # модель пропустила раздел 1
        other_remarks = [r for r in remarks if r not in notice_remarks]
        if (other_remarks and not items) or (remarks and not notice and not items):
            return None                              # пустой ответ при наличии замечаний — не принимаем
        head = f"Информация, представленная в извещении{num}, соответствует требованиям законодательства"
        head += f", за исключением: {notice.rstrip('.')}." if notice else "."
        if not items:
            return head
        lines = [f"{clean_numbers(i.get('numbers'))}. {clip_text(i['text'], 400)}".strip(". ") for i in items]
        return head + "\nВыявлены несоответствия и недостатки по критериям:\n" + "\n".join(lines)
    recs = _unique([clip_text(r, 260).rstrip(".;") for r in parsed.get("recommendations") or [] if str(r).strip()])[:6]
    areas = _unique([a for a in parsed.get("areas") or [] if isinstance(a, dict) and a.get("summary")],
                    key=lambda a: a["summary"])[:6]
    if not recs:
        return None
    subject = f"закупки{num}" + (f" на {name.strip().rstrip('.')}" if name else "")
    kind = f"{procedure} для {subject}" if procedure else subject
    todo = "Заказчику рекомендуется:\n" + ";\n".join(f"- {r}" for r in recs) + "."
    if len(remarks) < MANY_REMARKS or not areas:
        return (f"Извещение и документация о проведении {kind} соответствуют требованиям законодательства, "
                f"за исключением указанных несоответствий и недостатков.\n{todo}")
    bullets = ",\n".join(f"- {clip_text(a.get('area'), 80)} — {lower_first(clip_text(a['summary'], 300).rstrip('.'))}" for a in areas) + "."
    note = lower_first(clip_text(parsed.get("note"), 400))
    return (f"Извещение о проведении {kind} и электронные документы имеют недостатки и несоответствия требованиям "
            f"законодательства РФ, а именно:\n{bullets}\n\n" + (f"Следует отметить, что {note.rstrip('.')}.\n\n" if note else "") + todo)


LAW_44FZ = ("Федерального закона от 05.04.2013 № 44-ФЗ «О контрактной системе в сфере закупок товаров, работ, услуг "
            "для обеспечения государственных и муниципальных нужд»")
PROCEDURES = (("competition", "открытого конкурса в электронной форме"), ("auction", "электронного аукциона"),
              ("quotation", "запроса котировок в электронной форме"), ("single", "закупки у единственного поставщика"))


def procedure_phrase(form_code: Optional[str]) -> Optional[str]:
    """Способ определения поставщика в родительном падеже по коду формы (``44fz_competition_obj6`` → «открытого конкурса…»)."""
    for marker, phrase in PROCEDURES:
        if marker in (form_code or ""):
            return phrase
    return None


def _title(label: str) -> str:
    """Название критерия без номера."""
    return re.sub(r"^\s*\d+(?:\.\d+)*\.?\s*", "", label or "").strip().rstrip(" .")


def fallback_block(key: str, remarks: Sequence[dict], stats: Dict[str, int], name: Optional[str] = None,
                   number: Optional[str] = None, procedure: Optional[str] = None) -> str:
    """Детерминированный текст блока III/IV по образцу экспертного заключения (если LLM недоступна).

    * ``field3`` («III. Вывод»): абзац об извещении («… соответствует требованиям законодательства[, за исключением: …]»)
      и перечень «Выявлены несоответствия и недостатки по критериям:» со строками ``номер «название». суть``.
    * ``field4`` («IV. Заключение»): «Извещение и документация о проведении <способ> для закупки № <номер> на <предмет>
      соответствуют требованиям законодательства, за исключением указанных несоответствий и недостатков.
      Заказчику рекомендуется: - …»; без замечаний — «… соответствуют …, целесообразно оформить документацию …».
      Самостоятелен, без ссылок на другие блоки.

    Args:
        key: ``field3`` или ``field4``.
        remarks: Проверенные замечания (``number``, ``criterion``, ``comment``).
        stats: Счётчики (``checked``, ``remarks``).
        name: Наименование объекта закупки.
        number: Реестровый номер закупки (шифр).
        procedure: Способ определения поставщика в родительном падеже.

    Returns:
        str: Текст блока.
    """
    num = f" № {number}" if number else ""
    if key == "field3":
        notice = [r for r in remarks if (r.get("number") or "").startswith("1.")]
        other = [r for r in remarks if r not in notice]
        if notice:
            items = "; ".join(((r.get("comment") or "").strip().rstrip(".") or _title(r["criterion"])) for r in notice)
            head = f"Информация, представленная в извещении{num}, соответствует требованиям законодательства, за исключением: {items}."
        else:
            head = f"Информация, представленная в извещении{num}, соответствует требованиям законодательства."
        if not other:
            return head + ("" if notice else " Документация о проведении закупки соответствует требованиям действующего законодательства Российской Федерации.")
        lines = [f"{r.get('number') or ''} «{_title(r['criterion'])}». {(r.get('comment') or '').strip()}".strip() for r in other]
        return head + "\nВыявлены несоответствия и недостатки по критериям:\n" + "\n".join(lines)
    subject = f"Извещение и документация о проведении {procedure} для закупки{num}" if procedure else f"Извещение и документация о проведении закупки{num}"
    if name:
        subject += f" на {name.strip().rstrip('.')}"
    if not remarks:
        return (f"{subject} соответствуют требованиям законодательства. На основании представленной документации "
                f"целесообразно оформить документацию и осуществить закупку в соответствии с требованиями {LAW_44FZ}.")
    groups: Dict[str, List[str]] = {}
    for r in remarks:
        num = r.get("number") or _title(r["criterion"])
        groups.setdefault(num.split(".")[0], []).append(num)
    where = {"1": "в извещении о закупке", "2": "в описании объекта закупки, обосновании НМЦК и проекте контракта",
             "3": "в документации о закупке", "4": "в части требований технических регламентов и стандартов"}
    todo = ";\n".join(f"- учесть и устранить замечания {where.get(g, 'по документации')} (критерии {', '.join(sorted(set(n)))})"
                      for g, n in sorted(groups.items()))
    return (f"{subject} соответствуют требованиям законодательства, за исключением указанных несоответствий и недостатков.\n"
            f"Заказчику рекомендуется:\n{todo}.")


async def write_blocks(fields: Sequence[dict], data: Dict[str, Any], trace: Dict[str, Any],
                       llm_call: Optional[LlmCall], form_code: Optional[str] = None) -> Dict[str, Any]:
    """Заполняет ``field3`` и ``field4`` (если они есть в форме): LLM видит только проверенные результаты.

    При отсутствии замечаний LLM не вызывается. Сбой или пустой ответ модели заменяется
    детерминированным текстом; это отмечается в ``trace``.

    Args:
        fields: Поля формы.
        data: ``data`` (изменяется на месте).
        trace: ``trace`` (изменяется на месте).
        llm_call: Функция ``(messages, schema) -> str`` или ``None`` (тогда используется шаблон).
        form_code: Код формы — по нему определяется способ закупки для заключения.

    Returns:
        dict: Счётчики ``{"checked", "remarks"}``, использованные при формулировке.
    """
    remarks = collect_remarks(fields, data, trace)
    checked = sum(1 for f in fields if f["value_kind"] in RESULT_KINDS and data.get(f["field_key"]) is not None)
    stats = {"checked": checked, "remarks": len(remarks)}
    full_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    procedure = procedure_phrase(form_code)
    for key in ("field3", "field4"):
        if key not in data:
            continue
        text, how, llm_error = None, "template", None
        if llm_call is not None and remarks:
            compact = [{"номер": r.get("number"), "критерий": _title(r["criterion"])[:140],
                        "суть": _clip(r.get("comment"), 260), "документ": r.get("document")} for r in remarks[:24]]
            payload = {"блок": BLOCK_TITLES[key], "объект закупки": data.get("name"), "номер закупки": data.get("code"),
                       "способ определения поставщика": procedure, "счётчики": stats, "замечания": compact,
                       "формат ответа": "плоский JSON с ключами " + ", ".join(BLOCK_SCHEMAS[key]["properties"]) + " (без обёртки)"}
            messages = [{"role": "system", "content": prompt_for(full_prompt, key)},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
            for attempt in range(2):          # обрезанный/невалидный ответ — одна повторная попытка
                try:
                    raw = await llm_call(messages, BLOCK_SCHEMAS[key])
                    parsed = parse_block(raw)
                    candidate = ((parsed.get("text") or "").strip() if parsed.get("text")
                                 else render_block(key, parsed, remarks, data.get("name"), data.get("code"), procedure))
                    if candidate:
                        text, how = candidate, "llm"
                        break
                    llm_error = f"пустой ответ модели: {_clip(raw, 200)}"
                except Exception as e:  # noqa: BLE001 — блок не должен ронять сборку
                    llm_error = f"{type(e).__name__}: {e}"[:300]
                    logger.warning(f"knowledge_store: блок {key}, попытка {attempt + 1}: {llm_error}")
        data[key] = defragment(text or fallback_block(key, remarks, stats, data.get("name"), data.get("code"), procedure))
        trace[key] = {"status": "generated", "method": how, "based_on": [r["field_key"] for r in remarks]}
        if how == "template" and llm_error:
            trace[key]["llm_error"] = llm_error
    return stats


FUNDING_SCHEMA = {
    "type": "object",
    "properties": {
        "funding": {"type": ["string", "null"], "maxLength": 200},
        "funding_quote": {"type": ["string", "null"], "maxLength": 300},
        "advance": {"type": ["number", "null"]},
        "advance_unit": {"type": ["string", "null"], "enum": ["руб.", "%", None]},
        "advance_quote": {"type": ["string", "null"], "maxLength": 300},
    },
    "required": ["funding", "funding_quote", "advance", "advance_unit", "advance_quote"],
    "additionalProperties": False,
}
FUNDING_PROMPT = (
    "Ты — эксперт по закупкам (44-ФЗ). По фрагментам документов закупки определи: 1) источник (способ) финансирования "
    "в формулировке эксперта, например «за счет средств федерального бюджета» или «за счет средств бюджетных учреждений»; "
    "2) размер аванса: число и единицу («руб.» или «%»). Используй ТОЛЬКО переданный текст. Если сведений нет, "
    "или в поле размера аванса значение не указано, верни null. Для каждого значения приведи ДОСЛОВНУЮ цитату "
    "(до 300 символов) из фрагмента, подтверждающую его. Если аванс не предусмотрен, верни null. Только JSON по схеме.")


_FUNDING_ALIASES = {
    "funding": ("источник_финансирования", "источник финансирования", "источник", "финансирование"),
    "funding_quote": ("цитата_источника", "цитата_финансирования", "источник_цитата"),
    "advance": ("размер_аванса", "аванс"),
    "advance_unit": ("единица_аванса", "единица", "unit"),
    "advance_quote": ("цитата_аванса", "аванс_цитата"),
}


def _funding_aliases(parsed: dict) -> dict:
    """Приводит имена полей ответа модели к схеме :data:`FUNDING_SCHEMA`.

    Бэкенд не гарантирует соблюдение схемы: модель отвечает, например, ``{"источник_финансирования": …,
    "размер_аванса": null}``. Известные синонимы переименовываются; значение по каноническому имени не затирается.
    """
    out = dict(parsed or {})
    lowered = {str(k).casefold(): v for k, v in out.items()}
    for canon, names in _FUNDING_ALIASES.items():
        if out.get(canon) is None:
            for name in names:
                if lowered.get(name) is not None:
                    out[canon] = lowered[name]
                    break
    return out


def funding_from_print_form(documents: Dict[int, dict]) -> Optional[Tuple[str, str]]:
    """Источник финансирования по печатной форме: строка «Закупка за счет …» со значением «Да».

    Returns:
        Optional[Tuple[str, str]]: ``(формулировка, цитата)`` или ``None``. Формулировка — это название отмеченной
        строки (например, «Закупка за счет собственных средств организации»), как её пишут эксперты.
    """
    for doc in documents.values():
        lines = [ln.strip() for ln in (doc.get("text") or "").splitlines() if ln.strip()]
        for i, line in enumerate(lines[:-1]):
            if re.match(r"закупка за счет\s", line, re.IGNORECASE) and lines[i + 1].casefold() == "да":
                return line, f"{line}\n{lines[i + 1]}"
    return None


def keyword_windows(documents: Dict[int, dict], pattern: str, radius: int = 350, limit: int = 4) -> List[str]:
    """Окна текста вокруг ключевых слов в документах (сначала извещение) — вход для точечного извлечения."""
    out: List[str] = []
    docs = sorted(documents.values(), key=lambda d: d.get("doc_code") != "docIzvejenieFiles")
    for doc in docs:
        text = doc.get("text") or ""
        for m in re.finditer(pattern, text, flags=re.I):
            lo, hi = max(0, m.start() - 80), min(len(text), m.end() + radius)
            window = text[lo:hi]
            if all(window[:60] not in w for w in out):
                out.append(window)
            if len(out) >= limit:
                return out
    return out


async def fill_funding_advance(fields: Sequence[dict], data: Dict[str, Any], trace: Dict[str, Any],
                               documents: Dict[int, dict], procurement: Optional[dict],
                               llm_call: Optional[LlmCall]) -> None:
    """Заполняет способ финансирования (``field1_3``) и аванс (``field1_2``/``field1_2_unit``), если они ещё пусты.

    Порядок источников: колонка ``funding`` паспорта закупки → точечное извлечение моделью по фрагментам
    документов, где встречаются «источник финансирования» и «аванс». Значение принимается, только если
    приведённая моделью цитата дословно найдена в тексте документов; иначе поле остаётся эксперту.

    Args:
        fields: Поля формы.
        data: ``data`` (изменяется на месте).
        trace: ``trace`` (изменяется на месте).
        documents: ``document_id`` → описание документа (``filename``, ``doc_code``, ``text``).
        procurement: Паспорт закупки (``funding``).
        llm_call: Функция вызова LLM или ``None`` (тогда только паспорт).
    """
    keys = {f["field_key"] for f in fields}
    funding = ((procurement or {}).get("funding") or "").strip()
    if "field1_3" in keys and data.get("field1_3") is None and funding:
        data["field1_3"] = funding
        trace["field1_3"] = {"status": "from_passport", "source": "pe_procurements"}
    need_funding = "field1_3" in keys and data.get("field1_3") is None
    need_advance = "field1_2" in keys and data.get("field1_2") is None
    if llm_call is None or not (need_funding or need_advance):
        return
    windows = (keyword_windows(documents, r"источник\w*\s+финансирования|за счет средств|за счёт средств") if need_funding else []) \
        + (keyword_windows(documents, r"размер\w*\s+аванс|авансов\w+\s+платеж|аванс") if need_advance else [])

    def fail(reason: str) -> None:
        """Причина, по которой поле осталось пустым, — в trace (иначе её не видно)."""
        for key, needed in (("field1_3", need_funding), ("field1_2", need_advance)):
            if needed and data.get(key) is None:
                trace[key] = {"status": "no_fact", "note": reason}

    if not windows:
        return fail("в документах не найдено мест со словами «источник финансирования» / «аванс»")
    messages = [{"role": "system", "content": FUNDING_PROMPT},
                {"role": "user", "content": "\n\n".join(f"[{i}] {w}" for i, w in enumerate(windows, 1))}]
    try:
        raw = await llm_call(messages, FUNDING_SCHEMA)
        parsed = _funding_aliases(parse_block(raw))
    except Exception as e:  # noqa: BLE001 — общие сведения не должны ронять сборку
        logger.warning(f"knowledge_store: финансирование/аванс не извлечены: {type(e).__name__}: {e}")
        return fail(f"ошибка модели: {type(e).__name__}: {e}"[:300])

    def proven(quote: Any) -> Optional[str]:
        """Цитата, найденная в тексте документов (или ``None``)."""
        for doc in documents.values():
            found = facts_mod.find_quote(str(quote or ""), doc.get("text") or "") if quote else None
            if found:
                return found[:300]
        return None

    value = (parsed.get("funding") or "").strip()
    quote = proven(parsed.get("funding_quote"))
    if need_funding and value and quote:
        data["field1_3"] = value
        trace["field1_3"] = {"status": "verified", "source": "llm_extraction", "quote": quote}
    number, unit = _number(parsed.get("advance")), parsed.get("advance_unit")
    advance_quote = proven(parsed.get("advance_quote"))
    quote = advance_quote
    if need_advance and number is not None and unit and quote:
        data["field1_2"] = number
        trace["field1_2"] = {"status": "verified", "source": "llm_extraction", "quote": quote}
        if "field1_2_unit" in keys and data.get("field1_2_unit") is None:
            data["field1_2_unit"] = unit
            trace["field1_2_unit"] = {"status": "derived", "rule": "единица из найденной формулировки аванса"}
    if need_funding and data.get("field1_3") is None:
        found = funding_from_print_form(documents)
        if found:
            data["field1_3"] = found[0]
            trace["field1_3"] = {"status": "verified", "source": "print_form", "quote": found[1],
                                 "note": "источник финансирования по отмеченной строке печатной формы"}
    answer = f"ответ модели: {_clip(raw, 300)}; окон текста: {len(windows)}"
    if need_advance and data.get("field1_2") is None and data.get("field2_1_22") == 2:
        trace["field1_2"] = {"status": "derived", "note": "аванс не предусмотрен (критерий 1.22 = «2»), размер не указывается"}
        need_advance = False
    if need_funding and data.get("field1_3") is None:
        trace["field1_3"] = {"status": "no_fact", "note": "значение не принято (нет значения или цитата не найдена в тексте); " + answer}
    if need_advance and data.get("field1_2") is None:
        trace["field1_2"] = {"status": "no_fact", "note": "аванс не принят (нет числа/единицы или цитата не найдена в тексте); " + answer}


def validate(data: Dict[str, Any], fields: Sequence[dict]) -> List[str]:
    """Проверяет, что ``data`` соответствует форме: только её ключи и допустимые типы значений.

    Args:
        data: Собранный ``data``.
        fields: Поля формы.

    Returns:
        list[str]: Найденные проблемы (пусто, если всё в порядке).
    """
    problems = []
    kinds = {f["field_key"]: f["value_kind"] for f in fields}
    for key in data:
        if key not in kinds:
            problems.append(f"ключ вне формы: {key}")
    for key, kind in kinds.items():
        if key not in data:
            problems.append(f"нет ключа формы: {key}")
            continue
        value = data[key]
        if value is None:
            continue
        if kind in RESULT_KINDS and value not in (0, 1, 2):
            problems.append(f"{key}: недопустимое значение {value!r}")
        if kind == forms.KIND_SECTION and value not in (0, 1):
            problems.append(f"{key}: недопустимый итог подраздела {value!r}")
        if kind == forms.KIND_TEXT and not isinstance(value, str):
            problems.append(f"{key}: ожидается текст")
    return problems


async def precheck(conn, expertise_id: int) -> Dict[str, Any]:
    """Проверяет, что сводное ЭЗ можно строить: есть документы, тексты не очищены, форма поддерживается.

    Args:
        conn: Соединение ``asyncpg``.
        expertise_id: ID экспертизы.

    Returns:
        dict: ``{"form_code", "passport", "documents"}``.

    Raises:
        NoDataError: Документов в хранилище нет («сначала /evaluate-documents»).
        TextPurgedError: Все тексты очищены политикой хранения («перезапустите /evaluate-documents»).
        UnsupportedFormError: Для экспертизы нет формы в справочнике.
    """
    docs = await repo.get_documents_brief(conn, expertise_id)
    if not docs:
        raise NoDataError("По экспертизе нет данных в хранилище знаний — сначала выполните /evaluate-documents")
    if not any(d["has_text"] for d in docs):
        raise TextPurgedError("Тексты документов удалены по сроку хранения — перезапустите /evaluate-documents")
    passport = await repo.get_procurement(conn, expertise_id)
    if not passport:
        raise NoDataError("Нет паспорта закупки — сначала выполните /evaluate-documents")
    code = forms.get_form_code(passport["law"], passport["check_type2"], passport["object_code"],
                               available=await repo.list_form_codes(conn))
    if not code:
        raise UnsupportedFormError(
            f"Форма заключения не поддерживается ({passport['law']}, checkType2={passport['check_type2']}, "
            f"объект {passport['object_code']})")
    await repo.set_form_code(conn, expertise_id, code)
    return {"form_code": code, "passport": passport, "documents": docs}


async def generate_summary(conn, expertise_id: int, llm_call: Optional[LlmCall],
                           build_facts: Optional[FactsBuilder] = None, rebuild_facts: bool = False) -> Dict[str, Any]:
    """Собирает и сохраняет сводное ЭЗ экспертизы.

    Args:
        conn: Соединение ``asyncpg``.
        expertise_id: ID экспертизы.
        llm_call: Функция вызова LLM для блоков III–IV (``None`` — только шаблонные тексты).
        build_facts: Корутина-фабрика построения фактов; вызывается, если фактов ещё нет (или ``rebuild_facts``).
        rebuild_facts: Пересчитать факты перед сборкой, даже если они уже есть.

    Returns:
        dict: ``summary_id``, ``form_code``, ``status`` (``draft`` / ``needs_review``), ``data``
        (ровно ключи формы), ``trace`` (доказательства отдельно), ``stats``, ``problems``.

    Raises:
        SummaryError: См. :func:`precheck`; :class:`NoFactsError`, если фактов нет и построить их нечем.
    """
    ready = await precheck(conn, expertise_id)
    code = ready["form_code"]
    # факты раздела 1 из XML/печатной формы дёшевы и детерминированы — обновляются при каждой сборке
    eis_stats = await facts_mod.extract_facts(conn, expertise_id, code, None)
    fact_rows = [dict(r) for r in await repo.get_facts(conn, expertise_id)]
    has_llm = any(r["source"] == facts_mod.SOURCE_LLM for r in fact_rows)
    if (not has_llm or rebuild_facts) and build_facts is not None:
        await build_facts()
        fact_rows = [dict(r) for r in await repo.get_facts(conn, expertise_id)]
    if not fact_rows:
        raise NoFactsError("Факты по экспертизе ещё не построены — повторите запрос позже")

    fields = [dict(r) for r in await repo.get_form_fields(conn, code)]
    documents = {d["id"]: {"filename": d["filename"], "doc_code": d["doc_code"], "text": d["text_full"],
                           "extraction": d.get("extraction")}
                 for d in ready["documents"]}
    fact_rows = [r for r in fact_rows if r["fact_key"] not in facts_mod.ASSESSED_KEYS]   # их оценивает модель по документам
    decided = {r["fact_key"] for r in fact_rows if normalize_result((_as_dict(r.get("value")) or {}).get("value")) is not None}
    preset = await assessment.compute_assessed(fields, documents, llm_call, skip=decided)
    data, trace = assemble(fields, fact_rows, documents, ready["passport"], preset)
    await fill_funding_advance(fields, data, trace, documents, ready["passport"], llm_call)
    stats = await write_blocks(fields, data, trace, llm_call, code)
    problems = validate(data, fields)
    if problems:
        logger.error(f"knowledge_store: сводное ЭЗ {expertise_id} не соответствует форме: {problems[:5]}")
    unresolved = [f["field_key"] for f in fields
                  if f["value_kind"] in RESULT_KINDS and data.get(f["field_key"]) is None]
    proposed = [k for k, v in trace.items() if isinstance(v, dict) and v.get("status") == "proposed"]
    status = "needs_review" if unresolved or proposed or problems else "draft"
    stats.update({"fields": len(fields), "unresolved": len(unresolved), "proposed": len(proposed),
                  "eis_xml": eis_stats.get("eis_xml", 0), "eis_print_form": eis_stats.get("eis_print_form", 0)})
    trace["_summary"] = {"unresolved": unresolved, "proposed": proposed, "problems": problems}
    async with conn.transaction():
        summary_id = await repo.insert_summary(conn, expertise_id, code, data, trace, status)
        await repo.touch_expiry(conn, expertise_id, Config.PE_TEXT_RETENTION_DAYS)
    logger.info(f"knowledge_store: сводное ЭЗ {expertise_id} ({code}): {status}, {stats}")
    return {"summary_id": summary_id, "form_code": code, "status": status, "data": data, "trace": trace,
            "stats": stats, "problems": problems}