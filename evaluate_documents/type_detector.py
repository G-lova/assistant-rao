import asyncio
import json
import re
import time
from json_repair import repair_json
from string import Template
from typing import Any, Dict, List

from configs.logger import get_logger
from configs.rate_limiter import llm_rate_limiter
from configs.retry_utils import LLM_RETRY_CONFIG, async_retry


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


class TypeDetector:
    """Определяет тип документа (код из ``DOCUMENT_TYPE_MAPPING``) по началу текста и имени файла.

    Первый шаг разбора документа: от типа зависит профиль полей, которые потом извлекает
    :class:`evaluate_documents.data_extractor.DataExtractor`. Если модель не ответила или вернула неизвестный тип,
    тип определяется по ключевым словам в имени файла и начале текста.
    """
    def __init__(self, llm_client, model):
        """Создаёт определитель типа.

        Args:
            llm_client: Клиент LLM (OpenAI-совместимый).
            model: Название модели.
        """
        self.client = llm_client
        self.model = model


        self.rate_limiter = llm_rate_limiter()  # общий на процесс лимит частоты запросов к модели

        with open("prompts/type_detector_prompt.txt") as f:
            self.type_detector_prompt = f.read()

        with open("schemas/type_detector_schema.json") as f:
            self.TYPE_DETECTOR_SCHEMA = f.read()
    

    @async_retry(LLM_RETRY_CONFIG)
    async def detect_document_type(self, content: str, filename: str) -> Dict[str, Any]:
        """
        """
        # Ждём «разрешения» от глобального лимитера ПЕРЕД запросом
        await self.rate_limiter.acquire()
        
        try:
            logger.info(f"Определение типа документа '{filename}': {len(content or '')} символов начала документа")

            # Загружаем промпт и схему
            prompt = Template(self.type_detector_prompt).safe_substitute(
                mapping=json.dumps(DOCUMENT_TYPE_MAPPING, ensure_ascii=False, indent=2)
            )

            schema = self.TYPE_DETECTOR_SCHEMA.replace('"ALLOWED_DOC_TYPES"', json.dumps(ALLOWED_DOC_TYPES))

            # Вызов модели с guided_json и таймаутом
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system",
                            "content": prompt},
                        {"role": "user", "content": f"Имя файла: {filename}\nОпредели тип этого документа:\n\n{content}"}
                    ],
                    extra_body={
                        "guided_json": json.loads(schema)},
                    max_tokens=3000,
                    temperature=0
                )
            except asyncio.TimeoutError:
                logger.error(f"Таймаут при анализе '{filename}'")
                return None

            raw_response = (response.choices[0].message.content or "").strip()
            if not raw_response:
                # модель вернула пустой ответ (content = null, 02.11.2026: prod_multimodal_llm) — не исключение
                logger.warning(f"Пустой ответ модели (определение типа) для '{filename}': "
                               f"finish_reason={getattr(response.choices[0], 'finish_reason', None)}")
                return None
            logger.info(f"Тип '{filename}': ответ модели {len(raw_response)} символов: {raw_response[:1000]}")
            logger.debug(raw_response)  # полный ответ — только в отладке: журнал рос до 4,7 МБ за прогон (30.10.2026)

            # Парсим JSON
            try:
                result = json.loads(raw_response)
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
                return result
            
            except json.JSONDecodeError:
                logger.error("Не удалось распарсить ни одну JSON структуру.")
                return None

        except Exception as e:
            logger.error(f"Ошибка при определении типа документа '{filename}': {str(e)}")
            return None


    async def detect(self, chunks: List[str], filename: str) -> Dict[str, Any]:
        """Тип документа по его началу (не по каждому блоку) и имени файла.

        Модели отдаётся начало документа (``settings.TYPE_DETECT_HEAD_CHARS`` символов первого блока). Если модель не
        ответила или вернула тип не из списка / ``unknown`` — тип по ключевым словам имени файла и начала текста.

        Args:
            chunks: Блоки текста документа.
            filename: Имя файла.

        Returns:
            dict: ``detected_type``, ``confidence``, ``key_indicators``, ``issues`` и ``source``
            (``model`` / ``keywords``).
        """
        from configs import settings

        started = time.monotonic()
        head = (chunks[0] if chunks else "")[: settings.TYPE_DETECT_HEAD_CHARS]
        if len(chunks) > 1 or (chunks and len(chunks[0]) > len(head)):
            head += f"\n\n[… начало документа; всего блоков текста: {len(chunks)}]"
        result = None
        try:
            result = await self.detect_document_type(content=head, filename=filename)
        except Exception as e:  # noqa: BLE001 — ретраи исчерпаны; тип по ключевым словам
            logger.warning(f"Тип '{filename}': модель не ответила ({type(e).__name__}: {e}) — по ключевым словам")
        result = dict(result) if isinstance(result, dict) else {}
        detected = result.get("detected_type")
        if detected in ALLOWED_DOC_TYPES and detected != "unknown":
            result["source"] = "model"
        else:
            by_words = self.detect_document_type_by_name(filename)
            if by_words == "docDopMaterialsFiles":
                by_words = self.detect_document_type_by_name(head[:3000])
            result.update(detected_type=by_words, source="keywords", confidence=result.get("confidence") or 0.3)
        result.setdefault("issues", [])
        logger.info(f"Тип '{filename}': {result['detected_type']} "
                    f"«{DOCUMENT_TYPE_MAPPING.get(result['detected_type'], '')}» (источник: {result['source']}, "
                    f"уверенность {result.get('confidence')}, признаки: {result.get('key_indicators') or '—'}, "
                    f"замечания: {result.get('issues') or '—'}) за {time.monotonic() - started:.1f} с")
        return result


    async def get_type_compliance(self, type_results: Dict[str, Any], doc_type: str, expertise_object: int, document_name: str) -> Dict[str, Any]:
        """
        """
        detected_type = type_results.get("detected_type", "")

        if detected_type not in ALLOWED_DOC_TYPES:
            detected_type = self.detect_document_type_by_name(document_name)

        if ((doc_type == 'docProjContractFiles') and (detected_type in {'docProjContractFiles', 'docContractDoWorkFiles', 'docContractNIRFiles', 'docContractPostTovarFiles'})) or (
            (doc_type == 'docDopMaterialsFiles') and (detected_type != 'unknown')):
            type_results["status"] = "allow"
            type_results["detected_type"] = doc_type
            type_results["issues"] = []
            
        elif doc_type == 'linkDocs' and expertise_object in (3,5,6) and detected_type in {'docProjContractFiles', 'docContractDoWorkFiles', 'docContractNIRFiles', 'docContractPostTovarFiles'}:
            type_results["status"] = "allow"
            type_results["detected_type"] = 'docProjContractFiles'
            type_results["issues"] = []

        elif (doc_type == 'linkDocs') and (detected_type != 'unknown'):
            type_results["status"] = "allow"
            type_results["detected_type"] = detected_type
            type_results["issues"] = []

        elif detected_type == doc_type:
            type_results["status"] = "allow"
            type_results["issues"] = []

        else:
            type_results["status"] = "deny"
            type_results["issues"] = [
                f"Ожидался {'Ссылка на ЕИС' if doc_type == 'linkDocs' else DOCUMENT_TYPE_MAPPING.get(doc_type, 'Дополнительные материалы')}, ",
                f"но в документе {'Ссылка на ЕИС' if doc_type == 'linkDocs' else DOCUMENT_TYPE_MAPPING.get(detected_type, 'Неизвестный документ')}"
            ]

        return type_results


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