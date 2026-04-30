import asyncio
import json
from configs.rate_limiter import TokenBucket
from configs.retry_utils import LLM_RETRY_CONFIG, async_retry
from json_repair import repair_json
from string import Template

from configs.logger import get_logger
from typing import Any, Dict, List

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
            final_result = self.merge_chunk_results(successful_results, doc_type, expertise_object)
            logger.info(f"Объединение извлеченных данных по чанкам для {document_name} прошло успешно")

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
                            Определи тип этого документа. Соответствует ли он {doc_type if doc_type in DOCUMENT_TYPE_MAPPING.keys() else "docDopMaterialsFiles"}: ({DOCUMENT_TYPE_MAPPING.get(doc_type, "Дополнительные материалы")})? 
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


    def merge_chunk_results(self, chunk_results: List[Dict[str, Any]], doc_type: str, expertise_object: int) -> Dict[str, Any]:
        """
        Объединяет результаты анализа нескольких чанков в единый структурированный ответ.

        Логика объединения:
            - **Читаемость**: усредняется оценка, определяется доминирующий язык,
              собираются все замечания (например, «размытый текст», «артефакты OCR»).
            - **Сырые данные**:
                - `contract_number` — берётся первый непустой.
                - Списки (`dates`, `amounts`, `law_references`, `organizations`, `items`) — объединяются.
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
        if len(chunk_results) == 1:
            return chunk_results[0]
        
        
        # Базовый шаблон итоговой структуры
        merged = {
            "type_compliance": {
                "status": "deny",
                "detected_type": "unknown",
                "issues": [f"Ошибка при определении типа документа"]
            },
            "readability": {
                "status": "deny",
                "image_description": "",
                "issues": [],
                "readability_score": 0.0,
                "main_language": None,
                "language_confidence": 0.0
            },
            "raw_data": {
                "document_name": "",
                "contract_number": "",
                "dates": [],
                "amounts": [],
                "nmck_method": {
                    "method": "",
                    "assessment": ""
                },
                "organizations": [],
                "law_references": [],
                "procurement_subject": {
                    "description": "",
                    "items": []
                },
                "planned_timeline": {},
                "reporting_documentation": {},
                "execution_location": {},
                "other_data": []
            }
        }

        # --- TYPE_COMPLIANCE ---

        type_compliance = chunk_results[0].get("type_compliance", {
            "status": "deny",
            "detected_type": "unknown",
            "issues": [f"Ожидался {DOCUMENT_TYPE_MAPPING.get(doc_type, 'Дополнительные материалы')}, но в документе 'Неизвестный документ'"]
        })
    
        # 🔧 Нормализация: если пришла строка вместо объекта — конвертируем
        if isinstance(type_compliance, str):
            logger.warning(f"Нормализация type_compliance: строка '{type_compliance}' → объект")
            type_compliance = {
                "status": "allow" if (type_compliance in ALLOWED_DOC_TYPES) and (type_compliance == doc_type) else "deny",
                "detected_type": type_compliance if type_compliance in ALLOWED_DOC_TYPES else "unknown",
                "issues": [] if (type_compliance in ALLOWED_DOC_TYPES) and (type_compliance == doc_type) else [
                    f"Ожидался {DOCUMENT_TYPE_MAPPING.get(doc_type, 'Дополнительные материалы')}, но в документе {DOCUMENT_TYPE_MAPPING.get(type_compliance, 'Неизвестный документ')}"
                ]
            }
        elif not isinstance(type_compliance, dict):
            logger.warning(f"Некорректный тип type_compliance: {type(type_compliance)}")
            type_compliance = {
                "status": "deny",
                "detected_type": "unknown",
                "issues": [f"Ожидался {DOCUMENT_TYPE_MAPPING.get(doc_type, 'Дополнительные материалы')}, но в документе 'Неизвестный документ'"]
            }

        if type_compliance:
            doc_code = type_compliance.get('detected_type', 'unknown')
            if ((doc_type == 'docProjContractFiles') and (doc_code in {'docProjContractFiles', 'docContractDoWorkFiles', 'docContractNIRFiles', 'docContractPostTovarFiles'})) or (
                (doc_type == 'docDopMaterialsFiles') and (doc_code != 'unknown')):
                merged["type_compliance"] = {
                    "status": "allow",
                    "detected_type": doc_type,
                    "issues": []
                }
            elif doc_type == 'linkDocs' and expertise_object in (3,5,6) and doc_code in {'docProjContractFiles', 'docContractDoWorkFiles', 'docContractNIRFiles', 'docContractPostTovarFiles'}:
                merged['type_compliance'] = {
                    "status": "allow",
                    "detected_type": 'docProjContractFiles',
                    "issues": []
                }
            elif (doc_type == 'linkDocs') and (doc_code != 'unknown'):
                merged['type_compliance'] = {
                    "status": "allow",
                    "detected_type": doc_code if doc_code in DOCUMENT_TYPE_MAPPING.keys() else "docDopMaterialsFiles",
                    "issues": []
                }
            else:
                merged["type_compliance"] = type_compliance

        readability_scores = []
        languages = {}
        language_conf_values = []

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

            # document_name, contract number, nmck_method": если найден в chunk — сохраняем первый
            for key in ["document_name", "contract_number"]:
                if not merged["raw_data"][key]:
                    val = raw_data.get(key)
                    if val:
                        merged["raw_data"][key] = val

            nmck = raw_data.get("nmck_method", {})
            if nmck:
                if not merged["raw_data"]["nmck_method"]["method"]:
                    merged["raw_data"]["nmck_method"]["method"] = nmck.get("method", "")
                if not merged["raw_data"]["nmck_method"]["assessment"]:
                    merged["raw_data"]["nmck_method"]["assessment"] = nmck.get("assessment", "")

            # даты, суммы, упоминания законов, прочие данные
            for key in ["dates", "amounts", "law_references", "other_data", "organizations"]:
                merged["raw_data"][key].extend(raw_data.get(key, []))

            # организации
            # for item in merged["raw_data"]["organizations"]:
            #     if item["name"] != '':
            #         merged["raw_data"]["organizations"].extend(raw_data.get("organizations", []))

            # предмет закупки
            procurement = raw_data.get("procurement_subject", {})
            if procurement:
                # description — собираем непустые варианты
                desc = procurement.get("description")
                if desc:
                    # Чтобы не склеивать мусор, только уникальные + не пустые
                    if merged["raw_data"]["procurement_subject"]["description"]:
                        merged["raw_data"]["procurement_subject"]["description"] += "\n" + desc
                    else:
                        merged["raw_data"]["procurement_subject"]["description"] = desc

                # items
                merged["raw_data"]["procurement_subject"]["items"].extend(
                    procurement.get("items", [])
                )

            # сроки, отчетность, место выполнения
            for key in ["planned_timeline", "reporting_documentation", "execution_location"]:
                pt = raw_data.get(key, {})
                if pt:
                    merged["raw_data"][key].update({k: v for k, v in pt.items() if v})

        # ---- ПОСЛЕ ОБХОДА ВСЕХ ЧАНКОВ ----

        # агрегированный readability_score
        if readability_scores:
            merged["readability"]["readability_score"] = round(sum(readability_scores) / len(readability_scores), 2)

        if merged["readability"]["readability_score"] >= 0.7:
            merged["readability"]["status"] = "allow"
        else:
            merged["readability"]["status"] = "deny"

        # most frequent language
        if languages:
            merged["readability"]["main_language"] = max(languages.items(), key=lambda x: x[1])[0]
        else:
            merged["readability"]["main_language"] = "ru"  # fallback

        # средняя уверенность языка
        if language_conf_values:
            merged["readability"]["language_confidence"] = round(sum(language_conf_values) / len(language_conf_values), 2)
        else:
            merged["readability"]["language_confidence"] = 0.0

        # убрать дубликаты в строковых списках
        merged["raw_data"]["law_references"] = list(set(merged["raw_data"]["law_references"]))
        merged["raw_data"]["other_data"] = list(set(merged["raw_data"]["other_data"]))

        # коррекция названия для проекта контракта
        if doc_code == merged["type_compliance"]["detected_type"] == "docProjContractFiles":
            merged["raw_data"]["document_name"] = "Проект контракта"

        return merged