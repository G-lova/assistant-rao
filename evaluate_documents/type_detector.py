import asyncio
import json
import re
from json_repair import repair_json
from string import Template
from typing import Any, Dict

from configs.logger import get_logger
from configs.rate_limiter import TokenBucket
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
    """
    """
    def __init__(self, llm_client, model):
        """
        """
        self.client = llm_client
        self.model = model


        self.rate_limiter = TokenBucket(rate=1.5)  # 1.5 запроса в секунду

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
            logger.info(f"Определение типа документа '{filename}'")

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
                        {"role": "user", "content": f"""
                            Определи тип этого документа:\n\n{content}
                        """}
                    ],
                    extra_body={
                        "guided_json": json.loads(schema)},
                    max_tokens=3000,
                    temperature=0.1
                )
            except asyncio.TimeoutError:
                logger.error(f"Таймаут при анализе '{filename}'")
                return None

            raw_response = response.choices[0].message.content.strip()
            logger.info(f"Получен ответ длиной {len(raw_response)} символов для чанка '{filename}'")
            logger.info(raw_response)

            # Парсим JSON
            try:
                result = json.loads(raw_response)
                logger.info("Удалось распарсить JSON.")
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