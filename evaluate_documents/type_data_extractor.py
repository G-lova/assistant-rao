import asyncio
import json
import re
from json_repair import repair_json
from string import Template
from typing import Any, Dict, List

from configs.logger import get_logger
from configs.rate_limiter import TokenBucket
from configs.retry_utils import LLM_RETRY_CONFIG, async_retry
from configs.utils import extract_json_objects


logger = get_logger(__name__)

DOCUMENT_TYPE_MAPPING = {
    'docAcceptInafPostavFiles': 'Документация, подтверждающая невозможность (нецелесообразность) использования иных способов определения поставщика (исполнителя, подрядчика)',
    'docActPriemTovFiles': 'Акт о приемке товара',
    'docAssetSelOrgFiles': 'Положение о закупках организации',
    'docCargoTaxFiles': 'Товарная накладная',
    'docCertValidFiles': 'Сертификаты соответствия',
    'docContractDoWorkFiles': 'Контракт на выполнение работ (оказание услуг)',
    'docContractNIRFiles': 'Контракт на выполнение НИР (или НИОКР)',
    'docContractPostTovarFiles': 'Контракт на поставку товара',
    'docDocPriemActSdachFiles': 'Документ о приемке и/или акт сдачи-приемки работ (услуг)',
    'docDopConsentContractFiles': 'Дополнительные соглашения к контракту',
    'docDopMaterialsFiles': 'Дополнительные материалы',
    'docExpertReportFiles': 'Экспертиза результатов исполнения контракта, проведенная членами приемочной комиссии или силами привлеченной экспертной организации',
    'docIzvejenieFiles': 'Извещение',
    'docMaterialValidNMCKFiles': 'Материалы, подтверждающие Обоснование н(м)цк',
    'docOpusObjectZacupFiles': 'Описание объекта закупки',
    'docObosnNMCKFiles': 'Обоснование н(м)цк',
    'docPhotoCargoFiles': 'Фото товара',
    'docPhotoFinishWorkFiles': 'Фото результатов выполнения работ (оказания услуг)',
    'docPorViewOcenkFiles': 'Порядок рассмотрения и оценки заявок на конкурс',
    'docProjContractFiles': 'Проект контракта',
    'docPriemTovSchetFiles': 'Документ о приемке товара',
    'docReportDoNIRFiles': 'Отчет о выполнении НИР, а также документы, подтверждающие исполнение требований к выполнению НИР',
    'docTechDocFiles': 'Техническая документация, паспорт товара и пр.',
    'docTrebContentRequestFiles': 'Требования к содержанию заявки на конкурс и инструкция по ее заполнению',
    'docValidAllIfFiles': 'Документы, подтверждающие исполнение всех условий, предусмотренных контрактом',
    'docValidCopyriteFiles': 'Документы, подтверждающие передачу авторских прав на результаты интеллектуальной собственности',
    'docValidCountyFiles': 'Документы, подтверждающие страну происхождение товара',
    'docValidGarantFiles': 'Документы, подтверждающие гарантийные обязательства ',
    'docVziskPenyFiles': 'Документы по взысканию пени и штрафов',
    'unknown': 'Неизвестный документ'
}

ALLOWED_DOC_TYPES = list(DOCUMENT_TYPE_MAPPING.keys())
ALLOWED_DOC_TYPES.append('linkDocs')


DOCUMENT_TYPE_KEYWORDS = {
    "docAssetSelOrgFiles": ["положение о закупках"],
    "docCargoTaxFiles": ["товарная накладная", "накладная торг-12", "торг-12"],
    "docCertValidFiles": ["сертификат соответствия", "декларация соответствия"],
    "docContractPostTovarFiles": ["контракт на поставку", "на поставку", "договор поставки", "поставка товара", "контракт"],
    "docContractDoWorkFiles": ["контракт на выполнение работ", "договор подряда", "оказание услуг", "выполнение работ", "контракт"],
    'docContractNIRFiles': ["контракт", "на выполнение НИР", "на выполнение НИОКР", "НИОКР", "НИР", "научно-исследовательских", "договор"],
    "docDocPriemActSdachFiles": ["акт сдачи-приемки", "акт выполненных работ", "акт оказанных услуг"],
    "docDopConsentContractFiles": ["дополнительное соглашение"],
    "docIzvejenieFiles": ["извещение", "извещение о проведении", "уведомление о размещении"],
    "docMaterialValidNMCKFiles": ["коммерческое предложение", "анализ рынка", "ценовая информация"],
    "docObosnNMCKFiles": ["обоснование нмцк", "обоснование н(м)цк", "обоснование начальной", "обоснование максимальной цены"],
    "docOpusObjectZacupFiles": ["описание объекта закупки", "техническое задание", "тз"],
    "docPhotoCargoFiles": ["фото товара"],
    "docPhotoFinishWorkFiles": ["фото выполненных работ", "фото результата работ"],
    "docPriemTovSchetFiles": ["документ о приемке", "акт приемки", "универсальный передаточный документ", "упд"],
    "docProjContractFiles": ["контракт", "договор"],
    "docPorViewOcenkFiles": ["порядок рассмотрения и оценки"],
    "docTechDocFiles": ["паспорт изделия", "паспорт товара", "техническая документация", "руководство по эксплуатации"],
    "docTrebContentRequestFiles": ["требования к содержанию", "инструкция по заполнению заявки"],
    "docValidGarantFiles": ["гарантийное письмо", "гарантийные обязательства"]
}


class TypeDataExtractor:
    """
    Извлекает структурированные данные и оценивает читаемость документов с помощью LLM.

    Класс предназначен для анализа одного или нескольких текстовых фрагментов (чанков) документа.
    Для каждого чанка вызывается крупная языковая модель (LLM) с заданной JSON-схемой,
    чтобы извлечь ключевые метаданные (номер контракта, даты, суммы, организации и др.)
    и оценить качество текста (читаемость, язык, наличие артефактов OCR).

    Поддерживает параллельную обработку чанков, объединение результатов и устойчивость
    к ошибкам парсинга или таймаутам.
    """
    def __init__(self, llm_client, model):
        """
        Инициализирует экстрактор данных с клиентом LLM и моделью.

        Загружает:
            - промпт для извлечения данных из файла `prompts/data_extractor_prompt.txt`,
            - JSON-схему из `schemas/data_extractor_schema.json`, в которую подставляется
              список допустимых типов документов (`ALLOWED_DOC_TYPES`).

        Args:
            llm_client: Экземпляр клиента LLM (совместимого с OpenAI API),
                        поддерживающего метод `chat.completions.create`.
            model (str): Название модели LLM (например, "gpt-4o", "llama3-70b" и т.д.).
        """
        self.client = llm_client
        self.model = model


        self.semaphore = asyncio.Semaphore(5)
        self.rate_limiter = TokenBucket(rate=1.5)  # 1.5 запроса в секунду

        with open("prompts/type_data_extractor_prompt.txt") as f:
            self.type_data_extractor_prompt = f.read()

        with open("schemas/type_data_extractor_schema.json") as f:
            self.TYPE_DATA_EXTRACTOR_SCHEMA = f.read()
            

    async def extract_data_from_document(self, chunks: List[str], document_name: str, doc_type: str, expertise_object: int) -> Dict[str, Any]:
        """
        Анализирует документ, разбитый на чанки, и возвращает объединённые структурированные данные.

        Метод:
            1. Запускает параллельный анализ всех чанков через `extract_data_from_chunk`.
            2. Игнорирует чанки, вызвавшие исключения, и логирует ошибки.
            3. Объединяет успешные результаты с помощью `merge_chunk_results`.
            4. При полной неудаче возвращает резервный ответ с пометкой об ошибке.

        Args:
            chunks (List[str]): Список текстовых фрагментов (чанков) исходного документа.
            document_name (str): Имя документа (для логирования и идентификации).

        Returns:
            str: JSON-строка с объединёнными результатами анализа в формате:
                {
                    "type_compliance": { ... },
                    "readability": { ... },
                    "raw_data": { ... }
                }
                Если ни один чанк не проанализирован успешно, возвращается JSON
                с полем `readability.status = "неудовлетворительно"` и сообщением об ошибке.
        """
        # Параллельный анализ всех чанков
        tasks = []
        for i, chunk in enumerate(chunks):
            task_name = f"{document_name} (блок {i+1})" if len(
                chunks) > 1 else document_name
            task = self.extract_data_from_chunk(
                content=chunk,
                document_name=task_name,
                doc_type=doc_type
            )
            tasks.append(task)
            
        # Запускаем все задачи параллельно
        async with self.semaphore:
            results = await asyncio.gather(*tasks, return_exceptions=True)

        # Обрабатываем результаты
        successful_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Ошибка при анализе блока {i+1}: {result}")
                continue
            if result != None:
                successful_results.append(result)

        if not successful_results:
            fallback = {
                "type_compliance": {"status": "deny", "issues": ["Не удалось определить тип: документ пустой"], "detected_type": "unknown"},
                "readability": {"status": "deny", "issues": ["Ошибка анализа документа"]},
                "raw_data": {}
            }
            return fallback

        # Объединяем результаты
        try:
            logger.info(f"Объединение извлеченных данных по чанкам для {document_name}")
            final_result = await self.merge_chunk_results(successful_results, doc_type, expertise_object, document_name)
            logger.info(f"Объединение извлеченных данных по чанкам для {document_name} прошло успешно: {final_result}")

            # logger.info(f"Оптимизация объединенных данных для {document_name}")
            # detected_type = final_result.get("type_compliance", {}).get("detected_type", doc_type)
            # opt_result = await self.extract_data_from_chunk(json.dumps(final_result), document_name, detected_type)
            # logger.info(f"opt_result: {opt_result}")

            # return final_result if not opt_result else opt_result
            return final_result
        except Exception as e:
            logger.error(f"Ошибка объединения извлеченных данных по чанкам: {e}")
            return {
                "type_compliance": {"status": "deny", "issues": ["Ошибка объединения извлеченных данных по чанкам"], "detected_type": "unknown"},
                "readability": {"status": "deny", "issues": ["Ошибка анализа документа"]},
                "raw_data": {}
            }
    

    @async_retry(LLM_RETRY_CONFIG)
    async def extract_data_from_chunk(self, content: str, document_name: str, doc_type: str) -> Dict[str, Any]:
        """
        Анализирует отдельный текстовый чанк с помощью LLM и возвращает структурированные данные.

        Метод:
            - Обрезает контент до 15 000 символов, если он слишком длинный.
            - Вызывает LLM с guided JSON для получения предсказуемого формата.
            - Поддерживает таймаут (300 секунд) на выполнение запроса.
            - При неудаче пытается извлечь JSON-структуру из неформатированного ответа.

        Args:
            content (str): Текст чанка для анализа.
            document_name (str): Имя или идентификатор чанка (для логирования).

        Returns:
            Optional[Dict[str, Any]]: Словарь с полями `readability` и `raw_data`,
                                      соответствующий схеме `DATA_EXTRACTOR_SCHEMA`,
                                      или `None` в случае неустранимой ошибки.
        """
        # Ждём «разрешения» от глобального лимитера ПЕРЕД запросом
        await self.rate_limiter.acquire()
        
        try:
            logger.info(f"Анализ чанка '{document_name}'")
            logger.info(f"Длина контента: {len(content)} символов")

            # Загружаем промпт
            prompt = Template(self.type_data_extractor_prompt).safe_substitute(
                mapping=json.dumps(DOCUMENT_TYPE_MAPPING, ensure_ascii=False, indent=2)
            )

            img_result = ''
            content_splitted = content.split('"')
            for i, item in enumerate(content_splitted):
                if item in ['image_description'] and i + 2 <= len(content_splitted) - 1:
                    img_result = content_splitted[i + 2]


            schema = self.TYPE_DATA_EXTRACTOR_SCHEMA.replace(
                '"ALLOWED_DOC_TYPES"', json.dumps(ALLOWED_DOC_TYPES)).replace(
                '"img_description"', f'"{img_result}"')

            # # Ограничиваем размер контента для предотвращения ошибок
            # if len(content) > 15000:
            #     content = content[:15000] + "... [контент обрезан]"
            #     logger.info(f"Контент обрезан до {len(content)} символов")

            # Вызов модели с guided_json и таймаутом
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system",
                            "content": prompt},
                        {"role": "user", "content": f"""
                            Определи тип этого документа. Соответствует ли он {doc_type if doc_type in ALLOWED_DOC_TYPES else "docDopMaterialsFiles"}: ({'Ссылка на ЕИС' if doc_type == 'linkDocs' else DOCUMENT_TYPE_MAPPING.get(doc_type, "Дополнительные материалы")})? 
                            Проанализируй текст, оцени его читаемость, извлеки данные:\n\n{content}
                        """}
                    ],
                    extra_body={
                        "guided_json": json.loads(schema)},
                    max_tokens=3000,
                    temperature=0.1
                )
            except asyncio.TimeoutError:
                logger.error(f"Таймаут при анализе {document_name}")
                return None

            raw_response = response.choices[0].message.content.strip()
            logger.info(f"Получен ответ длиной {len(raw_response)} символов для чанка '{document_name}'")
            logger.info(raw_response)

            # Парсим JSON
            try:
                result = json.loads(raw_response)
                logger.info("Удалось распарсить JSON.")
                if isinstance(result, list):
                    result = self.merge_json_objects(result)
                return result
            
            except json.JSONDecodeError as e:
                logger.warning(f"Первая попытка парсинга JSON не удалась: {e}")

            # Поиск JSON структур вручную
            try:
                result = json.loads(repair_json(raw_response))

                if not result:
                    logger.error("JSON структуры не найдены.")
                    return None
                
                logger.info("Удалось распарсить JSON из извлечённого фрагмента вручную.")
                if isinstance(result, list):
                    result = self.merge_json_objects(result)
                return result
            
            except json.JSONDecodeError:
                logger.error("Не удалось распарсить ни одну JSON структуру.")
                return None

        except Exception as e:
            logger.error(f"Ошибка при анализе {document_name}: {str(e)}")
            return None

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
        if not new_evidence:
            return current_evidence
        if not current_evidence:
            return new_evidence
        
        conf_current = float(current_evidence.get("confidence", 0) or 0)
        conf_new = float(new_evidence.get("confidence", 0) or 0)
        
        return new_evidence if conf_new > conf_current else current_evidence


    async def merge_chunk_results(self, chunk_results: List[Dict[str, Any]], doc_type: str, expertise_object: int, document_name: str) -> Dict[str, Any]:
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
        type_compliance = chunk_results[0].get("type_compliance", {})

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
        for chunk in chunk_results:

            # --- RADABILITY ---
            readability = chunk.get("readability", {})

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


            # summary
            logger.info(f"Объединение summary")
            if raw_data.get("summary", ""):
                summary.append(raw_data.get("summary", ""))


            # document_name, contract number, nmck_method": если найден в chunk — сохраняем первый
            logger.info(f"Объединение document_name")
            dn = raw_data.get("document_name", {})
            if dn.get("value", "") and not merged["raw_data"]["document_name"]["value"]:
                merged["raw_data"]["document_name"]["value"] = dn["value"]
                merged["raw_data"]["document_name"]["confidence"] = dn.get("confidence", None)
                    
            logger.info(f"Объединение contract_number")
            cn = raw_data.get("contract_number", {})
            if cn.get("value", "") and not merged["raw_data"]["contract_number"]["value"]:
                merged["raw_data"]["contract_number"]["value"] = cn["value"]
                merged["raw_data"]["contract_number"]["evidence"] = cn.get("evidence", None)

            logger.info(f"Объединение nmck")
            nmck = raw_data.get("nmck_method", {})
            if nmck and nmck.get("method", ""):
                if not merged["raw_data"]["nmck_method"]["method"]:
                    merged["raw_data"]["nmck_method"]["method"] = nmck.get("method", "")
                if not merged["raw_data"]["nmck_method"]["assessment"]:
                    merged["raw_data"]["nmck_method"]["assessment"] = nmck.get("assessment", "")
                merged["raw_data"]["nmck_method"]["evidence"] = self._merge_evidence(
                    merged["raw_data"]["nmck_method"]["evidence"], nmck.get("evidence")
                )

            # даты, суммы, нормативные акты, отчетные документы, прочие данные
            logger.info(f"Объединение dates, reporting_documentation, other_data")
            for key in ["dates", "reporting_documentation", "other_data"]:
                raw_list = raw_data.get(key, [])
                if isinstance(raw_list, list):
                    merged["raw_data"][key].extend(raw_list)
            
            logger.info(f"Объединение finances")
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
            logger.info(f"Объединение law_references")
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
            logger.info(f"Объединение organizations")
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
            logger.info(f"Объединение procurement_subject")
            ps = raw_data.get("procurement_subject", {})
            if isinstance(ps, dict) and ps.get("description", "") and ps.get("evidence", "") and merged["raw_data"]["procurement_subject"]["description"]:
                    merged["raw_data"]["procurement_subject"]["description"] = ps["description"]
                    merged["raw_data"]["procurement_subject"]["evidence"] = ps.get("evidence")

            logger.info(f"Объединение items")
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
            logger.info(f"Объединение execution_location")
            el = raw_data.get("execution_location", {})
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
        if summary_string:
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system",
                            "content": "Сделай единое summary документа. 3-10 предложений."},
                        {"role": "user", "content": f"Вот summary каждого чанка:\n\n{summary_string}"}
                    ],
                    max_tokens=3000,
                    temperature=0.1
                )

                raw_response = response.choices[0].message.content.strip()
                logger.info(f"Объединение summary для '{document_name}'")
                logger.info(raw_response)

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
        if not document_name:
            return "unknown"

        name = document_name.lower()
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