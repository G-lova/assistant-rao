"""Разбор документа для ``/evaluate-documents``: тип → извлечение по профилю типа → объединение → проверка.

Раньше одна модель за один запрос на каждый блок текста и определяла тип, и оценивала читаемость, и извлекала все
14 полей схемы; тип брался из первого блока. Теперь шаги разделены:

1. :class:`TypeDetector` — тип документа по его началу и имени файла (один короткий запрос на документ);
2. по типу выбирается профиль полей (:mod:`evaluate_documents.doc_profiles`): у акта не спрашивают метод НМЦК,
   у декларации — сроки отчётных документов;
3. :class:`DataExtractor` извлекает поля профиля по каждому блоку (блоки размечены страницами);
4. платёжные поручения и XML площадок разбираются кодом (:mod:`evaluate_documents.rule_extractors`): платёжка,
   разобранная полностью, модели для извлечения не отправляется;
5. результаты блоков объединяются прежним образом, дубли убираются, значения сверяются с текстом.

Формат результата прежний: ``type_compliance`` (``status``, ``detected_type``, ``issues``), ``readability``,
``raw_data`` со всеми 14 полями (поля вне профиля — пустые значения шаблона).
"""
import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from configs import settings
from configs.logger import brief, get_logger
from evaluate_documents import rule_extractors
from evaluate_documents.data_extractor import DataExtractor
from evaluate_documents.doc_profiles import (
    PAYMENT_RE, PROFILE_TITLES, annotate_pages, describe_raw_data, format_raw_data_for_log, profile_fields,
    profile_for, profile_hint,
)
from evaluate_documents.evidence_check import drop_nulls, verify_raw_data
from evaluate_documents.merge_utils import as_evidence, as_text, chunk_counts, dedupe_raw_data
from evaluate_documents.type_detector import (  # noqa: F401 — реэкспорт: импортируются из этого модуля
    ALLOWED_DOC_TYPES, DOCUMENT_TYPE_KEYWORDS, DOCUMENT_TYPE_MAPPING, TypeDetector,
)

logger = get_logger(__name__)

# Читаемость платёжного поручения, разобранного кодом (модель текст не оценивала)
CODE_READABILITY = {"status": "allow", "image_description": "", "issues": [], "readability_score": 0.9,
                    "main_language": "ru", "language_confidence": 1.0}


def _as_obj(value, key: str) -> dict:
    """Поле ответа модели как объект.

    Модель иногда отдаёт строку вместо объекта (``"document_name": "Контракт"`` вместо
    ``{"value": "Контракт", "confidence": ...}``), особенно в обрезанном и восстановленном JSON — объединение блоков
    падало с «'str' object has no attribute 'get'» и терялись данные всего документа (прогон 02.07, 23.10.2026).

    Args:
        value: Значение поля из ответа.
        key: Ключ, под которым строка кладётся в объект (``value`` / ``method``).

    Returns:
        dict: Объект поля (пустой, если значения нет).
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, int, float)) and value != "":
        return {key: value}
    return {}



class TypeDataExtractor:
    """Разбор документа: тип (:class:`TypeDetector`) → поля профиля (:class:`DataExtractor`) → объединение.

    Интерфейс прежний (``extract_data_from_document(chunks, document_name, doc_type, expertise_object)``), им
    пользуются пайплайн ``/evaluate-documents`` и риск-мониторинг.
    """

    def __init__(self, llm_client, model):
        """Создаёт разборщик.

        Args:
            llm_client: Клиент LLM (OpenAI-совместимый, ``chat.completions.create``).
            model (str): Название модели.
        """
        self.client = llm_client
        self.model = model
        self.semaphore = asyncio.Semaphore(3)   # одновременных запросов по блокам одного документа
        self.type_detector = TypeDetector(llm_client, model)
        self.data_extractor = DataExtractor(llm_client, model)

    async def extract_data_from_document(self, chunks: List[str], document_name: str, doc_type: str,
                                         expertise_object: int) -> Dict[str, Any]:
        """Определяет тип документа, извлекает данные и оценивает читаемость.

        Args:
            chunks: Блоки текста документа.
            document_name: Имя файла.
            doc_type: Ожидаемый тип (поле экспертизы, куда загружен файл).
            expertise_object: Объект экспертизы.

        Returns:
            dict: ``type_compliance``, ``readability``, ``raw_data`` (формат прежний). Если ни один блок не
            разобран — ``deny`` с причиной.
        """
        started = time.monotonic()
        chunks = annotate_pages(chunks)
        text = "\n".join(chunks)

        # платёжное поручение — кодом (форма стабильная, модель ошибалась в суммах)
        payment = None
        if PAYMENT_RE.search(document_name or "") or PAYMENT_RE.search(text[:1500]):
            payment = rule_extractors.parse_payment_order(text, document_name)
            if payment:
                logger.info(f"Документ '{document_name}': платёжное поручение разобрано кодом "
                            f"({'полностью' if payment['complete'] else 'частично — дополнит модель'}):"
                            f"{format_raw_data_for_log(payment['raw_data'])}")
            else:
                logger.info(f"Документ '{document_name}': похоже на платёжку, но форма не распознана — разбор моделью")

        async with self.semaphore:   # запрос к модели — под общим лимитом пайплайна
            detection = await self.type_detector.detect(chunks, document_name)
        detected_type = detection.get("detected_type")
        profile = "payment" if payment else profile_for(detected_type, document_name, text[:3000])
        logger.info(f"Документ '{document_name}': поле экспертизы {doc_type}, тип {detected_type}, профиль {profile} "
                    f"({PROFILE_TITLES.get(profile, '')}): поля {', '.join(profile_fields(profile))}; "
                    f"блоков {len(chunks)}, текст {len(text)} символов")

        if payment and payment["complete"]:
            source = "код"
            chunk_results = [{"readability": dict(CODE_READABILITY), "raw_data": payment["raw_data"]}]
        else:
            source = "модель"
            hint = profile_hint(profile, detected_type, DOCUMENT_TYPE_MAPPING.get(detected_type or "", ""))
            chunk_results = await self._extract_chunks(chunks, document_name, profile, hint)

        if not chunk_results:
            logger.error(f"Документ '{document_name}': данные не извлечены ни из одного блока ({len(chunks)})")
            return {
                "type_compliance": {"status": "deny", "issues": ["Не удалось определить тип: документ пустой"], "detected_type": "unknown"},
                "readability": {"status": "deny", "issues": ["Ошибка анализа документа"]},
                "raw_data": {}
            }

        try:
            merged = await self.merge_chunk_results(chunk_results, doc_type, expertise_object, document_name,
                                                    detected_type=detected_type)
        except Exception as e:
            logger.error(f"Документ '{document_name}': ошибка объединения данных блоков: {type(e).__name__}: {e}",
                         exc_info=True)
            return {
                "type_compliance": {"status": "deny", "issues": ["Ошибка объединения извлеченных данных по чанкам"], "detected_type": "unknown"},
                "readability": {"status": "deny", "issues": ["Ошибка анализа документа"]},
                "raw_data": {}
            }

        raw = merged["raw_data"]
        if payment and not payment["complete"]:
            rule_extractors.overlay(raw, payment["raw_data"])
            source += " + код (платёжка)"
        xml = rule_extractors.parse_flat_xml(text) if rule_extractors.looks_like_flat_xml(text) else None
        if xml:
            logger.info(f"Документ '{document_name}': XML площадки, значения тегов:{format_raw_data_for_log(xml)}")
            rule_extractors.overlay(raw, xml)
            source += " + XML"
        removed = dedupe_raw_data(raw)
        if removed:
            logger.info(f"Документ '{document_name}': удалено дублей после объединения блоков: {removed}")

        # значения, которых нет в тексте документа, отбрасываются (суммы, даты, номер, ИНН/КПП/ОГРН, метод НМЦК)
        merged["raw_data"], dropped = verify_raw_data(raw, text)
        if dropped:
            logger.warning(f"Документ '{document_name}': нет в тексте документа, отброшено {len(dropped)}:\n    "
                           + "\n    ".join(dropped))

        tc = merged.get("type_compliance", {})
        logger.info(
            f"Документ '{document_name}' ({doc_type}): тип {detected_type} [{detection.get('source')}] → "
            f"{tc.get('status')}, профиль {profile}, блоков {len(chunks)}, извлечение: {source}, "
            f"читаемость {merged.get('readability', {}).get('readability_score')}; "
            f"{describe_raw_data(merged['raw_data'])}; дублей {removed}, отброшено {len(dropped)}; "
            f"{time.monotonic() - started:.1f} с")
        logger.info(f"Документ '{document_name}': извлечённые значения:{format_raw_data_for_log(merged['raw_data'])}")
        logger.info(f"Документ '{document_name}': соответствие типу {tc}; читаемость {merged.get('readability')}")
        logger.info(f"Документ '{document_name}': результат {brief(merged)}")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"Документ '{document_name}': результат целиком {json.dumps(merged, ensure_ascii=False, default=str)}")
        return merged

    async def _extract_chunks(self, chunks: List[str], document_name: str, profile: str,
                              hint: str) -> List[Dict[str, Any]]:
        """Извлечение по блокам через :class:`DataExtractor` (не больше ``self.semaphore`` запросов сразу).

        Args:
            chunks: Блоки текста (с метками страниц).
            document_name: Имя файла.
            profile: Профиль полей.
            hint: Подсказка о типе документа.

        Returns:
            list[dict]: Ответы по блокам (``readability``, ``raw_data``) без ``null``; блоки с ошибкой пропущены.
        """
        async def limited(i, chunk):
            """Запрос по одному блоку, заняв место в семафоре."""
            async with self.semaphore:
                name = f"{document_name} (блок {i + 1}/{len(chunks)})" if len(chunks) > 1 else document_name
                return await self.data_extractor.extract_data_from_chunk(content=chunk, filename=name,
                                                                         profile=profile, hint=hint)

        results = await asyncio.gather(*(limited(i, c) for i, c in enumerate(chunks)), return_exceptions=True)
        out = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Документ '{document_name}': ошибка в блоке {i + 1}: {type(result).__name__}: {result}")
                continue
            if isinstance(result, list):   # блок не поместился в контекст и разобран по частям
                out.extend(drop_nulls(r) for r in result if r)
            elif result is not None:
                out.append(drop_nulls(result))
        if len(out) < len(chunks):
            logger.warning(f"Документ '{document_name}': разобрано блоков {len(out)} из {len(chunks)}")
        return out

    def merge_json_objects(self, json_strings) -> dict:
        """Объединяет список JSON-строк в один словарь."""
        result = {}
        for js in json_strings:
            try:
                obj = json.loads(js) if isinstance(js, str) else js
                result.update(obj)  # поверхностное слияние
            except json.JSONDecodeError:
                continue
        return result


    def _merge_evidence(self, current_evidence: dict, new_evidence: dict) -> dict:
        """
        Вспомогательный метод для объединения evidence.
        Выбирает evidence с наибольшим confidence.
        """
        current_evidence, new_evidence = as_evidence(current_evidence), as_evidence(new_evidence)
        if not new_evidence:
            return current_evidence
        if not current_evidence:
            return new_evidence
        
        conf_current = float(current_evidence.get("confidence", 0) or 0)
        conf_new = float(new_evidence.get("confidence", 0) or 0)
        
        return new_evidence if conf_new > conf_current else current_evidence


    async def merge_chunk_results(self, chunk_results: List[Dict[str, Any]], doc_type: str, expertise_object: int,
                                  document_name: str, detected_type: Optional[str] = None) -> Dict[str, Any]:
        """
        Объединяет результаты анализа нескольких чанков в единый структурированный ответ.

        Логика объединения:
            - **Читаемость**: усредняется оценка, определяется доминирующий язык,
              собираются все замечания (например, «размытый текст», «артефакты OCR»).
            - **Сырые данные**:
                - `contract_number` — берётся первый непустой.
                - Списки (`dates`, `finances`, `law_references`, `organizations`, `items`) — объединяются.
                - Словари (`planned_timeline`, `reporting_documentation`, `execution_location`) — обновляются,
                  перезаписывая пустые значения непустыми.
                - `procurement_subject.description` — конкатенируется с разделителем `\n`.

        Args:
            chunk_results (List[Dict[str, Any]]): Список словарей, возвращённых LLM
                                                для каждого чанка.
            doc_type (str): Ожидаемый тип (код поля экспертизы, куда загружен файл).
            expertise_object (int): Объект экспертизы (для правил соответствия ссылки на ЕИС).
            document_name (str): Имя файла (для журнала).
            detected_type (str): Тип, определённый :class:`TypeDetector` по началу документа. Если не передан —
                как раньше, из ``type_compliance`` первого блока.

        Returns:
            Dict[str, Any]: Единый словарь в том же формате, но с агрегированными данными
                            по всем чанкам.
        """
        # Базовый шаблон итоговой структуры
        merged = {
            "type_compliance": {},
            "readability": {
                "status": "deny",
                "image_description": "",
                "issues": [],
                "readability_score": 0.0,
                "main_language": None
            },
            "raw_data": {
                "document_name": {"value": None, "confidence": 0.0},
                "summary": "",
                "contract_number": {"value": None, "evidence": None},
                "dates": [],
                "finances": [],
                "nmck_method": {"method": "", "assessment": "", "evidence": None},
                "organizations": [],
                "law_references": [],
                "procurement_subject": {"description": "", "evidence": None},
                "items": [],
                "planned_timeline": {"start_date": "", "end_date": "", "total_duration": "", "milestones": [], "evidence": None},
                "reporting_documentation": [],
                "execution_location": {"address": "", "special_conditions": "", "evidence": None},
                "other_data": []
            }
        }

        readability_scores = []
        languages = {}
        language_conf_values = []
        summary = []

        # --- TYPE_COMPLIANCE ---
        first = next((c for c in chunk_results if isinstance(c, dict)), {})
        type_compliance = {"detected_type": detected_type} if detected_type else first.get("type_compliance", {})
        detected_type = ""

        if type_compliance:
    
            # 🔧 Нормализация: если пришла строка вместо объекта — конвертируем
            if isinstance(type_compliance, str):
                logger.warning(f"Нормализация type_compliance: строка '{type_compliance}' → объект")
                detected_type = type_compliance

            if isinstance(type_compliance, dict):
                detected_type = type_compliance.get("detected_type", "")
                
            if detected_type not in ("unknown", ""):
                merged["type_compliance"]["detected_type"] = detected_type


        # ---- МЕРДЖ ЧАНКОВ ----
        for chunk_no, chunk in enumerate(chunk_results, 1):
            if not isinstance(chunk, dict):
                continue
            chunk_label = f"'{document_name}', блок {chunk_no}/{len(chunk_results)}"
            chunk_raw = chunk.get("raw_data") if isinstance(chunk.get("raw_data"), dict) else {}

            # --- RADABILITY ---
            readability = _as_obj(chunk.get("readability"), "status")

            if not merged["readability"]["image_description"]:
                img = readability.get("image_description")
                if img:
                    merged["readability"]["image_description"] = img

            # Проблемы OCR
            readability_issues = readability.get("issues", [])
            if readability_issues and isinstance(readability_issues, str):
                merged["readability"]["issues"].append(readability_issues)
            elif readability_issues and isinstance(readability_issues, list):
                merged["readability"]["issues"].extend(readability_issues)

            # Оценка читаемости
            if "readability_score" in readability:
                readability_scores.append(readability["readability_score"])

            # Язык
            lang = readability.get("main_language")
            conf = readability.get("language_confidence")
            if lang:
                languages.setdefault(lang, 0)
                languages[lang] += 1
            if conf:
                language_conf_values.append(conf)

            # --- RAW DATA ---
            raw_data = chunk.get("raw_data", {})
            if not isinstance(raw_data, dict):
                raw_data = {}


            # summary
            logger.info(f"Объединение summary ({chunk_label}): {chunk_counts(chunk_raw, ('summary',))}")
            if raw_data.get("summary", ""):
                summary.append(as_text(raw_data.get("summary", "")))


            # document_name, contract number, nmck_method": если найден в chunk — сохраняем первый
            logger.info(f"Объединение document_name ({chunk_label}): {chunk_counts(chunk_raw, ('document_name',))}")
            dn = _as_obj(raw_data.get("document_name"), "value")
            if dn.get("value", "") and not merged["raw_data"]["document_name"]["value"]:
                merged["raw_data"]["document_name"]["value"] = dn["value"]
                merged["raw_data"]["document_name"]["confidence"] = dn.get("confidence", None)
                    
            logger.info(f"Объединение contract_number ({chunk_label}): {chunk_counts(chunk_raw, ('contract_number',))}")
            cn = _as_obj(raw_data.get("contract_number"), "value")
            if cn.get("value", "") and not merged["raw_data"]["contract_number"]["value"]:
                merged["raw_data"]["contract_number"]["value"] = cn["value"]
                merged["raw_data"]["contract_number"]["evidence"] = cn.get("evidence", None)

            logger.info(f"Объединение nmck ({chunk_label}): {chunk_counts(chunk_raw, ('nmck_method',))}")
            nmck = _as_obj(raw_data.get("nmck_method"), "method")
            if nmck and nmck.get("method", ""):
                if not merged["raw_data"]["nmck_method"]["method"]:
                    merged["raw_data"]["nmck_method"]["method"] = nmck.get("method", "")
                if not merged["raw_data"]["nmck_method"]["assessment"]:
                    merged["raw_data"]["nmck_method"]["assessment"] = nmck.get("assessment", "")
                merged["raw_data"]["nmck_method"]["evidence"] = self._merge_evidence(
                    merged["raw_data"]["nmck_method"]["evidence"], nmck.get("evidence")
                )

            # даты, суммы, нормативные акты, отчетные документы, прочие данные
            logger.info(f"Объединение dates, reporting_documentation, other_data ({chunk_label}): {chunk_counts(chunk_raw, ('dates', 'reporting_documentation', 'other_data'))}")
            for key in ["dates", "reporting_documentation", "other_data"]:
                raw_list = raw_data.get(key, [])
                if isinstance(raw_list, list):
                    merged["raw_data"][key].extend(raw_list)
            
            logger.info(f"Объединение finances ({chunk_label}): {chunk_counts(chunk_raw, ('finances',))}")
            am = raw_data.get("finances", [])
            if isinstance(am, list):
                for item in am:
                    if not isinstance(item, dict):
                        continue
                    val = item.get("value", None)
                    # Разрешаем только реальные числа > 0
                    if isinstance(val, (int, float)) and val > 0:
                        merged["raw_data"]["finances"].append(item)

            # law_references
            logger.info(f"Объединение law_references ({chunk_label}): {chunk_counts(chunk_raw, ('law_references',))}")
            raw_laws = raw_data.get("law_references", [])
            if isinstance(raw_laws, list):
                for law_item in raw_laws:
                    if not isinstance(law_item, dict):
                        continue
                    
                    law_val = str(law_item.get("value", "")).strip()
                    if not law_val:
                        continue
                    
                    # 1. Нормализация: гарантируем, что evidence — это ВСЕГДА список
                    ev = law_item.get("evidence")
                    if not isinstance(ev, list):
                        law_item["evidence"] = [ev]
                    
                    # 2. Нормализуем название для поиска дубликатов
                    normalized_val = re.sub(r'\s+', ' ', law_val.lower().replace('№', '').replace('n', '').replace('статья', 'ст.'))
                    
                    # 3. Ищем, есть ли уже такой закон в итоговом списке
                    existing_law = next(
                        (x for x in merged["raw_data"]["law_references"] 
                         if re.sub(r'\s+', ' ', str(x.get("value", "")).strip().lower().replace('№', '').replace('n', '').replace('статья', 'ст.')) == normalized_val),
                        None
                    )
                    
                    if existing_law:
                        # Убеждаемся, что у существующего закона evidence тоже список
                        if not isinstance(existing_law.get("evidence"), list):
                            existing_law["evidence"] = [existing_law["evidence"]] if existing_law.get("evidence") else []
                        
                        # Собираем существующие фрагменты, чтобы не добавлять дубликаты
                        existing_fragments = {
                            str(e.get("fragment", "")).strip() 
                            for e in existing_law["evidence"] if isinstance(e, dict)
                        }
                        
                        # Добавляем новые уникальные цитаты
                        for new_ev in law_item["evidence"]:
                            if isinstance(new_ev, dict):
                                frag = str(new_ev.get("fragment", "")).strip()
                                if frag and frag not in existing_fragments:
                                    existing_law["evidence"].append(new_ev)
                                    existing_fragments.add(frag)
                    else:
                        # Если закона еще нет, добавляем его целиком
                        merged["raw_data"]["law_references"].append(law_item)

            # организации
            logger.info(f"Объединение organizations ({chunk_label}): {chunk_counts(chunk_raw, ('organizations',))}")
            organizations = raw_data.get("organizations", [])
            if isinstance(organizations, list):
                for item in organizations:
                    if not isinstance(item, dict):
                        continue
                    
                    # Нормализуем роль и имя для надежного сравнения
                    item_role = str(item.get("role", "")).strip().lower()
                    item_name = str(item.get("name", "")).strip().lower()
                    
                    # Строгая фильтрация: пропускаем, если имя отсутствует или является мусором
                    if not item_name or item_name in {"", "не указано", "null", "none", "н/д", "-"}:
                        continue

                    # Ищем дубликат строго по КОМБИНАЦИИ роли и имени
                    existing_org = next(
                        (org for org in merged["raw_data"]["organizations"] 
                         if str(org.get("role", "")).strip().lower() == item_role 
                         and str(org.get("name", "")).strip().lower() == item_name),
                        None
                    )

                    if existing_org:
                        # Если организация с такой же ролью и именем уже есть, дополняем только пустые поля
                        for key in ["inn", "kpp", "ogrn", "address"]:
                            item_val = str(item.get(key, "")).strip()
                            existing_val = str(existing_org.get(key, "")).strip()
                            
                            # Заполняем, если в новом item значение есть (и не мусор), а в существующем - нет
                            if item_val and item_val.lower() not in {"не указано", "null", "none", "", "-"} and not existing_val:
                                existing_org[key] = item_val
                        
                        # Объединяем evidence — выбираем фрагмент с наибольшим confidence
                        existing_org["evidence"] = self._merge_evidence(
                            existing_org.get("evidence"), 
                            item.get("evidence")
                        )
                    else:
                        # Если такой комбинации роли и имени еще нет, добавляем организацию целиком
                        merged["raw_data"]["organizations"].append(item)


            # предмет закупки
            logger.info(f"Объединение procurement_subject ({chunk_label}): {chunk_counts(chunk_raw, ('procurement_subject',))}")
            ps = raw_data.get("procurement_subject", {})
            if isinstance(ps, dict) and ps.get("description", "") and ps.get("evidence", "") and not merged["raw_data"]["procurement_subject"]["description"]:
                    merged["raw_data"]["procurement_subject"]["description"] = ps["description"]
                    merged["raw_data"]["procurement_subject"]["evidence"] = ps.get("evidence")

            logger.info(f"Объединение items ({chunk_label}): {chunk_counts(chunk_raw, ('items',))}")
            chunk_items = raw_data.get("items", [])
            if isinstance(chunk_items, list):
                for new_item in chunk_items:
                    if not isinstance(new_item, dict):
                        continue
                    
                    item_name = str(new_item.get("name", "")).strip().lower()
                    if not item_name:
                        continue

                    existing_item = next(
                        (item for item in merged["raw_data"]["items"] 
                         if str(item.get("name", "")).strip().lower() == item_name),
                        None
                    )

                    if existing_item:
                        # Дополняем пустые поля
                        for key in ["quantity", "unit", "specifications"]:
                            new_val = new_item.get(key)
                            if new_val and (isinstance(new_val, str) and new_val.strip() or isinstance(new_val, (int, float))):
                                if not existing_item.get(key):
                                    existing_item[key] = new_val
                        
                        # Объединяем evidence
                        existing_item["evidence"] = self._merge_evidence(
                            existing_item.get("evidence"), new_item.get("evidence")
                        )
                    else:
                        merged["raw_data"]["items"].append(new_item)


            # execution_location
            logger.info(f"Объединение execution_location ({chunk_label}): {chunk_counts(chunk_raw, ('execution_location',))}")
            el = _as_obj(raw_data.get("execution_location"), "address")
            if el:
                for k in ["address", "special_conditions"]:
                    if el.get(k) and not merged["raw_data"]["execution_location"].get(k):
                        merged["raw_data"]["execution_location"][k] = el[k]
                merged["raw_data"]["execution_location"]["evidence"] = self._merge_evidence(
                    merged["raw_data"]["execution_location"]["evidence"], el.get("evidence")
                )

        # ---- ПОСЛЕ ОБХОДА ВСЕХ ЧАНКОВ ----

        # --- TYPE_COMPLIANCE ---
        if merged["type_compliance"].get("detected_type", "") not in ALLOWED_DOC_TYPES:
            merged["type_compliance"]["detected_type"] = self.detect_document_type_by_name(merged["raw_data"].get("document_name", {}).get("value", ""))

        doc_code = merged["type_compliance"].get("detected_type")

        if ((doc_type == 'docProjContractFiles') and (doc_code in {'docProjContractFiles', 'docContractDoWorkFiles', 'docContractNIRFiles', 'docContractPostTovarFiles'})) or (
            (doc_type == 'docDopMaterialsFiles') and (doc_code != 'unknown')):
            merged["type_compliance"]["status"] = "allow"
            merged["type_compliance"]["detected_type"] = doc_type
            merged["type_compliance"]["issues"] = []
            
        elif doc_type == 'linkDocs' and expertise_object in (3,5,6) and doc_code in {'docProjContractFiles', 'docContractDoWorkFiles', 'docContractNIRFiles', 'docContractPostTovarFiles'}:
            merged["type_compliance"]["status"] = "allow"
            merged["type_compliance"]["detected_type"] = 'docProjContractFiles'
            merged["type_compliance"]["issues"] = []

        elif (doc_type == 'linkDocs') and (doc_code != 'unknown'):
            merged["type_compliance"]["status"] = "allow"
            merged["type_compliance"]["detected_type"] = doc_code
            merged["type_compliance"]["issues"] = []

        elif doc_code == doc_type:
            merged["type_compliance"]["status"] = "allow"
            merged["type_compliance"]["issues"] = []

        else:
            merged["type_compliance"]["status"] = "deny"
            merged["type_compliance"]["issues"] = [
                f"Ожидался {'Ссылка на ЕИС' if doc_type == 'linkDocs' else DOCUMENT_TYPE_MAPPING.get(doc_type, 'Дополнительные материалы')}, ",
                f"но в документе {'Ссылка на ЕИС' if doc_type == 'linkDocs' else DOCUMENT_TYPE_MAPPING.get(doc_code, 'Неизвестный документ')}"
            ]
            


        # агрегированный readability_score
        if readability_scores:
            # Если оценки есть, считаем среднее как обычно
            merged["readability"]["readability_score"] = round(sum(readability_scores) / len(readability_scores), 2)
            if merged["readability"]["readability_score"] >= 0.7:
                merged["readability"]["status"] = "allow"
            else:
                merged["readability"]["status"] = "deny"
        else:
            # Если оценок НЕТ, проверяем, удалось ли модели вообще что-то извлечь.
            # Если да, значит документ условно читаем (иначе модель не смогла бы извлечь данные).
            has_extracted_data = (
                merged["raw_data"]["document_name"]["value"] is not None or
                len(merged["raw_data"]["dates"]) > 0 or
                len(merged["raw_data"]["finances"]) > 0 or
                len(merged["raw_data"]["organizations"]) > 0 or
                merged["type_compliance"].get("detected_type", "unknown") != "unknown"
            )
            
            if has_extracted_data:
                merged["readability"]["readability_score"] = 0.85  # Дефолтный высокий балл
                merged["readability"]["status"] = "allow"
                merged["readability"]["issues"].append("Модель не предоставила явную оценку читаемости, но данные успешно извлечены.")
            else:
                # Если данных нет вообще, тогда честно ставим deny
                merged["readability"]["status"] = "deny"
                merged["readability"]["issues"].append("Не удалось оценить читаемость, данные не извлечены.")

        # most frequent language
        if languages:
            merged["readability"]["main_language"] = max(languages.items(), key=lambda x: x[1])[0]
        else:
            merged["readability"]["main_language"] = "ru"  # fallback

        # средняя уверенность языка
        if language_conf_values:
            merged["readability"]["language_confidence"] = round(sum(language_conf_values) / len(language_conf_values), 2)


        # коррекция названия для проекта контракта
        if merged["type_compliance"]["detected_type"] == "docProjContractFiles":
            merged["raw_data"]["document_name"]["value"] = "Проект контракта"

        # Объединение summary с помощью LLM
        summary_string = "\n".join(summary)
        if len(summary) == 1:
            # один блок — его summary и есть summary документа, отдельный запрос к модели не нужен
            merged["raw_data"]["summary"] = summary_string
        elif summary_string:
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system",
                            "content": "Сделай единое summary документа. 3-10 предложений."},
                        {"role": "user", "content": f"Вот summary каждого чанка:\n\n{summary_string}"}
                    ],
                    max_tokens=settings.EXTRACTION_MAX_TOKENS,
                    temperature=0
                )

                raw_response = response.choices[0].message.content.strip()
                logger.info(f"Объединение summary для '{document_name}'")
                logger.debug(raw_response)  # полный ответ — только в отладке: журнал рос до 4,7 МБ за прогон (30.10.2026)

                merged["raw_data"]["summary"] = raw_response

            except asyncio.TimeoutError:
                logger.error(f"Таймаут при обединении summary {document_name}")

                merged["raw_data"]["summary"] = summary_string
        
        # Удаление организаций без имени
        merged["raw_data"]["organizations"] = [
            org for org in merged["raw_data"].get("organizations", [])
            if isinstance(org, dict) and (str(org.get("name", "")).strip().lower() not in {"не указано", "none", "null", ""})
        ]
        
        # Удаление законов без доказательств
        merged["raw_data"]["law_references"] = [
            law for law in merged["raw_data"].get("law_references", [])
            if isinstance(law, dict) and law.get("evidence", [])
        ]
        
        # Удаление товаров без количества
        merged["raw_data"]["procurement_subject"]["items"] = [
            item for item in merged["raw_data"].get("procurement_subject", {}).get("items", [])
            if isinstance(item, dict) and item.get("quantity", None)
        ]


        return merged


    def detect_document_type_by_name(self, document_name: str) -> str:
        """
        Определяет тип документа по его названию.
        Возвращает код из DOCUMENT_TYPE_MAPPING.
        """
        if not document_name or not isinstance(document_name, (str, int, float)):
            return "unknown"

        name = str(document_name).lower()
        name = re.sub(r"\s+", " ", name)

        # сначала ищем более длинные совпадения
        candidates = []

        for doc_type, keywords in DOCUMENT_TYPE_KEYWORDS.items():
            for keyword in keywords:
                if keyword in name:
                    candidates.append((len(keyword), doc_type))

        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]

        return "docDopMaterialsFiles"