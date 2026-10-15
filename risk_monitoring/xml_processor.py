import os
import tempfile
from typing import Any, Dict

import aiofiles
import asyncio
import datetime
import json
import pandas as pd
import xmltodict

from collections import defaultdict
from deepdiff import DeepDiff

from configs.config import Config
from configs.data_fetcher import DataFetcher
from configs.file_reader import FileReader
from configs.http_client_manager import HTTPClientManager
from configs.llm_client import get_llm
from configs.logger import get_logger
from configs.parsing import CloudStorageParser
from configs.utils import load_sql_query_async, split_large_text
from evaluate_documents.consistency_checker import ConsistencyChecker
from evaluate_documents.data_extractor import DataExtractor
from evaluate_documents.type_data_extractor import TypeDataExtractor
from risk_monitoring.doc_risks_detector import DocumentRisksDetector
from risk_monitoring.event_analyzer import EventAnalyzer
from risk_monitoring.file_processor import FileProcessor
from risk_monitoring.send_ai_analysis_service import SendAIAnalysisService
from search_experts.embedding_client import EmbeddingClient


logger = get_logger(__name__)

IGNORED_FIELDS = {
    "id",
    "versionNumber",
    "docNumber",
    "fileSize",
    "publishedContentId",
    "createDate",
    "modifyDate",
    "uploadDate",
    "exportDate",
    "schemaVersion",
    "guid",
    "uuid",
    "versionGUID",
    "cryptoSigns",
    "fileHash",
    "signature",
    "serviceSigns"
}

NS_MAP = {
    "http://zakupki.gov.ru/oos/types/1": None,
    "http://zakupki.gov.ru/oos/base/1": None,
    "http://zakupki.gov.ru/oos/common/1": None,
    "http://zakupki.gov.ru/oos/export/1": None,
    "http://zakupki.gov.ru/oos/TPtypes/1": None,
    "http://zakupki.gov.ru/oos/EPtypes/1": None,
    "http://zakupki.gov.ru/oos/CPtypes/1": None,
    "http://zakupki.gov.ru/oos/KOTypes/1": None,
    "http://zakupki.gov.ru/oos/EATypes/1": None,
    "http://zakupki.gov.ru/oos/pprf615types/1": None,
    "http://zakupki.gov.ru/oos/URTypes/1": None,
    "http://zakupki.gov.ru/oos/SMTypes/1": None,
    "http://zakupki.gov.ru/oos/CETypes/1": None,
    "http://zakupki.gov.ru/oos/control99/1": None,
    "http://zakupki.gov.ru/oos/printform/1": None
}

def normalize_xml(xml_content):
    """
    """
    xml_dict = xmltodict.parse(
        xml_content,
        xml_attribs=False,
        attr_prefix='',
        process_namespaces=True,
        namespaces=NS_MAP
    ).values()
    for item in xml_dict:
        return item[list(item.keys())[-1]]

def remove_keys(obj, ignored):
    """
    """
    if isinstance(obj, dict):
        return {
            k: remove_keys(v, ignored)
            for k, v in obj.items()
            if k not in ignored
        }

    if isinstance(obj, list):
        return [
            remove_keys(v, ignored)
            for v in obj
        ]

    return obj


def compare_xml(old: dict, new: dict):
    """
    """
    diff = DeepDiff(
        old,
        new,
        ignore_order=True,
        exclude_paths=_ignored(),
        verbose_level=2
    )

    grouped = defaultdict(lambda: {
        "old_value": {},
        "new_value": {}
    })

    for path, change in diff.get("values_changed", {}).items():

        # отделяем последнее ['field']
        parent_path, field = path.rsplit("[", 1)

        field = field.rstrip("]")
        field = field.strip("'\"")

        grouped[parent_path]["old_value"][field] = change["old_value"]
        grouped[parent_path]["new_value"][field] = change["new_value"]

    diff["values_changed"] = dict(grouped)
    return diff


def _ignored():
    return {
        f"root['{field}']"
        for field in IGNORED_FIELDS
    }


class XMLProcessor:
    """
    
    """

    def __init__(self, http_manager: HTTPClientManager, environment: str = None):
        """
        
        """
        # Загрузка конфигурации
        self.config = Config()
        client, model = get_llm()
        
        # Инициализация компонентов с конфигурацией
        db_config = self.config.get_database_config(environment)
        embedding_config = self.config.get_embedding_config()
        self.file_storage = db_config.storage_path
        
        self.data_fetcher = DataFetcher(db_config.url, db_config.headers, http_manager)
        self.embedding_client = EmbeddingClient(
            embedding_config.api_url,
            embedding_config.api_key,
            embedding_config.model,
            batch_size=embedding_config.batch_size,
            http_manager=http_manager
        )
        self.parse_semaphore = asyncio.Semaphore(5)
        self.llm_semaphore = asyncio.Semaphore(3)
        self.cloud_parser = CloudStorageParser(http_manager, None)
        self.file_reader = FileReader(client, model)
        self.file_processor = FileProcessor(http_manager, environment)
        self.type_data_extractor = TypeDataExtractor(client, model)
        self.data_extractor =DataExtractor(client, model)
        self.doc_risks_detector = DocumentRisksDetector(client, model)
        self.event_analyzer = EventAnalyzer(client, model)


    # def extract_document_changes(self, xml_diff):
    #     changes = {
    #         "added": [],
    #         "removed": [],
    #         "changed": [],
    #         "unchanged": []
    #     }

    #     if not xml_diff:
    #         return changes

    #     # Изменённые attachment
    #     values_changed = xml_diff.get("values_changed", {})

    #     for path, change in values_changed.items():

    #         if "attachment" not in path:
    #             continue

    #         old_value = change.get("old_value")
    #         new_value = change.get("new_value")

    #         if not isinstance(old_value, dict) or not isinstance(new_value, dict):
    #             continue

    #         old_url = old_value.get("url")
    #         new_url = new_value.get("url")

    #         if old_url or new_url:
    #             changes["changed"].append({
    #                 "previous_url": old_url,
    #                 "current_url": new_url,
    #             })

    #     # Добавленные документы
    #     for key in ["iterable_item_added", "dictionary_item_added"]:
    #         for path, value in xml_diff.get(key, {}).items():

    #             if "attachment" not in path:
    #                 continue

    #             if isinstance(value, dict) and value.get("url"):
    #                 changes["added"].append({
    #                     "previous_url": None,
    #                     "current_url": value["url"],
    #                 })

    #     # Удалённые документы
    #     for key in ["iterable_item_removed", "dictionary_item_removed"]:
    #         for path, value in xml_diff.get(key, {}).items():

    #             if "attachment" not in path:
    #                 continue

    #             if isinstance(value, dict) and value.get("url"):
    #                 changes["removed"].append({
    #                     "previous_url": value["url"],
    #                     "current_url": None,
    #                 })

    #     return changes


    async def process_xml(self, start_date, end_date):
        """
        """
        try:
            # === Получение данных
            df = await self.data_fetcher.fetch_async_expertise_data(
                await load_sql_query_async("get_rm_xml_files.sql"), 
                bindings=[self.file_storage, self.file_storage, start_date, end_date, self.file_storage, self.file_storage]
            )
        
            if df.empty:
                raise Exception("Нет файлов для анализа")

            # === Парсинг ЕИС архивов
            # собираем уникальные ссылки на архивы
            archive_links = set(df["current_archive_storage_path"].dropna())
            archive_links |= set(df["previous_archive_storage_path"].dropna())

            entry_names = set(df["current_entry_name"].dropna()) | set(df["previous_entry_name"].dropna())

            # === Чтение и нормализзация xml, маппинг по имени файла
            text_by_name = self.get_xml_texts(
                archive_links=archive_links,
                entry_names=entry_names
            )

            def fill(row, entry_col, text_col):
                existing = row[text_col]
                if pd.isna(existing) if not isinstance(existing, dict) else False:
                    name = row[entry_col]
                    return text_by_name.get(name, existing)
                return existing

            df["current_xml_text"] = df.apply(lambda r: fill(r, "current_entry_name", "current_xml_text"), axis=1)
            df["previous_xml_text"] = df.apply(lambda r: fill(r, "previous_entry_name", "previous_xml_text"), axis=1)

            # === Получение изменений версий
            df["xml_version_changes"] = df.apply(
                lambda r: compare_xml(r["previous_xml_text"], r["current_xml_text"])
                if not pd.isna(r.get("previous_xml_id")) else None,
                axis=1,
            )

            # === Поиск в тексте xml документов, не привязанных к xml в БД
            # # парсинг json-строк
            # df["current_files"] = df["current_files"].apply(lambda x: json.loads(x))
            # df["previous_files"] = df["previous_files"].apply(lambda x: json.loads(x))

            file_links = []
            for row in df.itertuples():
                # if not row.current_files or not any(row.current_files):
                #     file_links.extend(self.cloud_parser.extract_urls_from_dict(row.current_xml_text))
                # if not row.previous_files or not any(row.previous_files):
                #     file_links.extend(self.cloud_parser.extract_urls_from_dict(row.previous_xml_text))
                    
                file_links.extend(self.cloud_parser.extract_urls_from_dict(row.current_xml_text))
                file_links.extend(self.cloud_parser.extract_urls_from_dict(row.previous_xml_text))

            df_missing_files = await self.data_fetcher.fetch_async_expertise_data(
                    await load_sql_query_async("get_rm_files.sql"), 
                    bindings=[self.file_storage, ",".join(file_links)]
                )

            if not df_missing_files.empty():
                df["current_files"] = df.apply(lambda x: [
                    {
                        "id": row.id,
                        "file_name": row.file_name,
                        "file_path": row.file_path,
                        "source_url": row.source_url,
                        "eis_version": row.eis_version
                    }
                    for row in df_missing_files.itertuples(index=False)
                    if (row.source_url in x["current_xml_text"] and x["current_version"] == row.eis_version)
                ])

                df["previous_files"] = df.apply(lambda x: [
                    {
                        "id": row.id,
                        "file_name": row.file_name,
                        "file_path": row.file_path,
                        "source_url": row.source_url,
                        "eis_version": row.eis_version
                    }
                    for row in df_missing_files.itertuples(index=False)
                    if (row.source_url in x["previous_xml_text"] and x["previous_version"] == row.eis_version)
                ])


            # # === Извлечение данных из xml
            # xml_data = []
            # for row in df.itertuples():
            #     logger.info(f"Извлечение данных из xml {row.current_entry_name}")

            #     xml_chunks = split_large_text(row.current_xml_text, 10000)
            #     if not xml_chunks:
            #         xml_analysis = {
            #             "raw_data": {},
            #             "emdeddings": [],
            #             "similar_event_ids": [],
            #             "risks": [
            #                 # {
            #                 #     "rule_id": "DOC-001",
            #                 #     "title": "Неполнота документа",
            #                 #     "description": "Документ пуст или не содержит читаемого текста"
            #                 # }
            #             ],
            #             "analysed_at": datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
            #         }

            #     xml_extracted_data = await self.data_extractor.extract_data_from_document(
            #         chunks=xml_chunks, 
            #         filename=row.current_entry_name
            #     )
            #     if isinstance(xml_extracted_data, str):
            #         xml_extracted_data = json.loads(xml_extracted_data)

            #     xml_data.append(xml_extracted_data)
            # df["xml_extracted_data"] = xml_data
                

            # === Анализ приложенных документов
            for row in df.itertuples():
                current_files = [file_info for file_info in row.current_files if file_info]

                if not current_files: # or len(current_files) <= 1: # отсеиваем файлы, где только печатные формы
                    continue

                doc_analyze_tasks = []
                for file_info in current_files:
                    if "printForm" in file_info.get("source_url"):
                        doc_analyze_tasks.append(self.file_processor.analyze_doc_text(
                            id=file_info.get("id"),
                            text=row.current_xml_text,
                            source=row.current_entry_name,
                            filename=file_info.get("file_name"),
                            context=row.current_context, # должен быть xml извещения или закупки
                            send_to_external=True
                        ))
                    else:
                        doc_analyze_tasks.append(self.file_processor.process_document(
                            id=file_info.get("id"),
                            url=file_info.get("file_path", file_info.get("source_url")),
                            source=row.current_entry_name,
                            filename=file_info.get("file_name"),
                            contract_id=row.purchase_number or row.contract_number,
                            context=row.current_context, # должен быть xml извещения или закупки
                            send_to_external=True
                        ))
                    

                doc_ai_analysis_results = await asyncio.gather(
                    *(doc_analyze_tasks),
                    return_exceptions=True
                )

                # Обрабатываем результаты
                current_files_with_ai = []
                for file_info, ai_analysis in zip(current_files, doc_ai_analysis_results):

                    if isinstance(ai_analysis, Exception):
                        logger.error(f"Ошибка при анализе документа {file_info}: {ai_analysis}")
                        current_files_with_ai.append(file_info)
                        continue

                    file_info["ai_analysis"] = ai_analysis
                    current_files_with_ai.append(file_info)
                    
                df.loc[row, "current_files"] = current_files_with_ai


                # === Описание события, анализ полноты и согласованости комплекта документов
                event_tasks = []
                for row in df.itertuples():
                    payload = {
                        "event_name": self.get_event_type(row.xml_tag),
                        "event_info": row.current_xml_text.get("commonInfo", row.current_xml_text),
                        "changes": row.xml_version_changes,
                        "documents": [
                            {
                                "doc_type": file_info.get("doc_type").get("detected_type"),
                                "raw_data": {
                                    k: v for k,v in self.data_extractor._strip_evidence(file_info.get("raw_data")) if k != "summary"
                                },
                                # "emdeddings": [],
                                # "similar_event_ids": [],
                                "resume": file_info.get("resume"), 
                                # "risks": file_info.get("risks")
                            } for file_info in row.current_files
                        ]
                    }
                    event_tasks.append(self.event_analyzer.check_event_risks(
                        content=json.dumps(payload, ensure_ascii=False, indent=2),
                        xml_name=row.current_entry_name)
                    )

                with self.llm_semaphore:
                    event_results = await asyncio.gather(*event_tasks)

                df["ai_analysis"] = event_results






            return df

        except Exception as e:
            if str(e) == "Нет файлов для анализа":
                return pd.DataFrame(columns=['expert_id', 'scoring'])
            else:
                raise


    async def get_event_type(
        self, 
        xml_name: str,
        xml_version: str
    ):
        """        
        """
        notification_events = [
            "epNotificationEZK2020",
            "epNotificationEF2020",
            "epNotificationEZT2020",
            "epNotificationEOK2020",
            "fcsNotificationEP",
            "fcsNotification111",
            "pprf615NotificationPO",
            "pprf615NotificationEF",
            "purchaseNotice",
            "purchaseNoticeOK",
            "purchaseNoticeOA",
            "purchaseNoticeAE",
            "purchaseNoticeAE94FZ",
            "purchaseNoticeAESMBO",
            "purchaseNoticeZK",
            "purchaseNoticeZKESMBO",
            "purchaseNoticeZPESMBO",
            "purchaseNoticeEP"
        ]

        events_mapping = {
            # закупки
            "epNotificationCancel": "Отменена закупка",
            "fcsNotificationCancel": "Отменена закупка",
            "pprf615NotificationCancel": "Отменена закупка",
            "epNotificationCancelFailure": "Возобновлена закупка",
            "fcsNotificationCancelFailure":	"Возобновлена закупка",
            "epNoticeApplicationsAbsence": "Отсутствуют заявки по закупке",
            "epNoticeApplicationCancel": "Отзыв заявки участником закупки",
            "EpProtocolEOK2020FirstSections": "Рассмотрены первые части заявок по закупке",
            "pprf615ProtocolEF1": "Рассмотрены первые части заявок по закупке",
            "EpProtocolEOK2020SecondSections": "Рассмотрены вторые части заявок по закупке",
            "pprf615ProtocolEF2": "Рассмотрены вторые части заявок по закупке",
            "epProtocolEZK2020Final": "Определен победитель закупки",
            "epProtocolEZT2020Final": "Определен победитель закупки",
            "epProtocolEF2020Final": "Определен победитель закупки",
            "epProtocolEOK2020Final": "Определен победитель закупки",
            "pprf615ProtocolPO": "Определен победитель закупки",

            "epProtocolEOK3": "Подведены итоги электронного открытого конкурса",
            "epProtocolEOKSingleApp": "Рассмотрена единственная заявка на участие в электронном открытом конкурсе",
            "fcsProtocolEF1": "Рассмотрены первые части заявок на участие в электронном аукционе",
            "fcsProtocolEF2": "Рассмотрены вторые части заявок и проведения электронного аукциона",
            "fcsProtocolEF3": "Подведены итоги электронного аукциона",
            "fcsProtocolEFSingleApp": "Рассмотрена единственная заявка на участие в электронном аукционе",

            "epProtocolCancel": "Отмена определения поставщика по закупке",
            "pprf615ActCancel": "Отмена определения поставщика по закупке",
            "pprf615ProtocolCancel": "Отмена определения поставщика по закупке",
            "epClarificationDocRequest": "Поступил запрос на разъяснение документации по закупке",
            "pprf615ClarificationRequest": "Поступил запрос на разъяснение документации по закупке",
            "epClarificationResultRequest":	"Поступил запрос на разъяснение результатов закупки",
            "epClarificationDoc": "Публикация разъяснений положений документации по закупке",
            "pprf615Clarification": "Публикация разъяснений положений документации по закупке",
            "epClarificationResult": "Публикация разъяснений результатов определения поставщика по закупке",
            "epProtocolEvasion": "Уклонение победителя от заключения контракта по закупке",
            "pprf615ActEvasion": "Уклонение победителя от заключения контракта по закупке",
            "epProtocolDeviation": "Отклонена заявка по закупке",
            "pprf615ActDeviation": "Отклонена заявка по закупке",
            "epProtocolEvDevCancel": "Отменено решение об уклонении или отклонении по закупке",
            "fcsPurchaseDocument": "Закупочная документация (архив документов закупки)",
            "cpContractProject": "Опубликован проект контракта",
            "cpContractProjectChange": "Изменен проект контракта",

            # контракт
            "cpContractSign": "Подписан контракт",
            "cpContractProjectSign": "Подписан контракт",
            "cpContractSignLKP": "Подписан контракт",
            "cpProtocolDisagreements": "Разногласия по контракту",
            "cpNoticeDeviation": "Уклонение от заключения контракта",
            "cpNoticeEvasion": "Победитель уклонился от заключения контракта",
            "cpRefusalConcludeContract": "Отказ от заключения контракта",
            "cpProcedureCancel": "Отменена процедура заключения контракта",
            "pprf615ContractProcedureCancel": "Отменена процедура заключения контракта",
            "cpProcedureCancelFailure": "Возобновлена процедура заключения контракта",
            "cpContractProjectLKP": "Проект контракта в личном кабинете поставщика",
            "cpContractProjectChangeLKP": "Изменение проекта контракта в личном кабинете поставщика",
            "cpProcedureCancelLKP": "Отмена процедуры в личном кабинете поставщика",
            "contractAvailableForElAct": "Контракт зарегистрирован в ЕИС (доступен для электронного актирования)",
            "ON_NSCHFDOPPOK": "Уведомление об операции (счет-фактура/документ)",
            "ON_NKORSCHFDOPPOK": "Уведомление об отказе в принятии уточнения",
            "DP_UVUTOCH": "Документ об уточнении",
            "DP_IZVUCH": "Извещение об уточнении",
            "DP_PDOTPR": "Подтверждение даты отправки",
            "DP_IZVPOL": "Извещение о получении",
            "ON_AKTREZRABZ": "Уведомление об аннулировании результата работы (титул заказчика)",
            "ON_NSCHFDOPPR": "Уведомление об операции (приемка)",
            "ON_NKORSCHFDOPPR": "Уведомление об отказе в операции",
            "DP_PDPOL": "Подтверждение даты получения",
            "DP_PROTZ": "Протокол разногласий (электронный)",
            "DP_UVOBZH": "Уведомление об обжаловании",
            "ON_AKTREZRABP": "Уведомление об аннулировании результата работы (титул поставщика)",
            "DP_KVITIZMSTATUS": "Квитанция о статусе извещения",
            "elActUnstructuredSupplierTitle": "Неструктурированный электронный акт (титул поставщика)",
            "elActUnstructuredCustomerTitle": "Неструктурированный электронный акт (титул заказчика)",
            "contractProcedureUnilateralRefusal": "Решение об одностороннем отказе",
            "contractProcedureUnilateralRefusalCancel": "Отмена решения об одностороннем отказе",
            "claimsCorrespondenceNotice": "Уведомление о претензионной переписке",
            "parContractProcedureUnilateralRefusal": "Параллельное уведомление об одностороннем отказе",
            "parContractProcedureUnilateralRefusalCancel": "Отмена параллельного уведомления",
            "parClaimsCorrespondenceNotice": "Параллельное уведомление о претензионной переписке",

            # жалобы
            "complaint": "Жалоба",
            "closedComplaint": "Закрытая жалоба",
            "complaintWithdraw": "Отзыв жалобы",
            "complaintCancel": "Отмена жалобы",
            "closedComplaintCancel": "Отмена закрытой жалобы",
            "complaintDecision": "Решение по жалобе",
            "tenderSuspension": "Приостановление закупки",
            "complaintTransfer": "Передача жалобы",
            "closedComplaintTransfer": "Передача закрытой жалобы",
            "parElectronicComplaintAccept": "Уведомление о принятии жалобы (электронное)",
            "closedParElectronicComplaintAccept": "Уведомление о принятии закрытой жалобы",
            "parElectronicComplaintRefusal": "Уведомление об отказе в рассмотрении жалобы",
            "closedParElectronicComplaintRefusal": "Уведомление об отказе в рассмотрении закрытой жалобы",
            "complaintVerificationPlan"	"План проверки по жалобе"
            "complaintVerificationResult": "Результат проверки по жалобе",

            "checkPlan": "План проверки",
            "eventPlan": "Плановое мероприятие",
            "eventPlanSuspension": "Приостановление планового мероприятия",
            "unplannedCheck": "Внеплановая проверка",
            "closedUnplannedCheck": "Закрытая внеплановая проверка",
            "unplannedCheckCancel": "Отмена внеплановой проверки",
            "closedUnplannedCheckCancel": "Отмена закрытой внеплановой проверки",
            "unplannedCheckTenderSusp": "Приостановление закупки по внеплановой проверке",
            "closedUnplannedCheckTenderSusp": "Приостановление закупки по закрытой внеплановой проверке",

            "unplannedEvent": "Внеплановое мероприятие",
            "unplannedEventCancel": "Отмена внепланового мероприятия",
            "unplannedEventSuspension": "Приостановление внепланового мероприятия",
            "checkResult": "Результат проверки",
            "closedCheckResult": "Результат закрытой проверки",
            "eventResult": "Результат мероприятия",
            "checkResultCancel": "Отмена результата проверки",
            "closedCheckResultCancel": "Отмена результата закрытой проверки",
            "eventResultCancel": "Отмена результата мероприятия",
            "fcsAuditResult": "Результат аудита в сфере закупок",

            # недобросовестные поставщики
            "unfairSupplier2022": "Сведения о недобросовестном поставщике (с 2022 г.)",
            "unfairSupplierIKZ": "Сведения о недобросовестном поставщике (по ИКЗ)",
            "unfairSupplier2022Exclude": "Исключение сведений о недобросовестном поставщике",
            "pprf615QualifiedContractor": "Сведения о квалифицированном подрядчике (ПП РФ № 615)",
            "pprf615QualifiedContractorExclude": "Исключение из реестра квалифицированных подрядчиков",
            "pprf615QualifiedContractorExcludeCancel": "Отмена исключения из реестра",

            "dishonestSupplier": "Недобросовестный поставщик",
            "dishonestApplication": "Недобросовестная заявка",
            "dishonestApplicationRejectType": "Тип отклонения недобросовестной заявки",
            "dishonestSupplierReject": "Отказ во включении в РНП",
            "dishonestSupplierIncludeType": "Тип включения в РНП",
            "dishonestSupplierExcludeType": "Тип исключения из РНП",


            "control99UniversalExtract": "Универсальная выписка контроля",
            "control99TenderPlan2020Extract": "Выписка из плана-графика",
            "control99NotificationExtract": "Выписка из извещения",
            "control99BeginMessage": "Сообщение о начале проверки",
            "control99RefusalMessage": "Сообщение об отказе",
            "control99NoticeCompliance": "Уведомление о соответствии",
            "control99ProtocolMismatch": "Протокол о несоответствии",
            "control99ProtocolMismatchReductFunds": "Протокол о несоответствии с уменьшением объема средств",
            "fcsCustomerReportContractExecution": "Отчет об исполнении контракта",
            "fcsCustomerReportSmallScaleBusiness": "Отчет о закупках у СМП",
            "fcsCustomerReportBigProjectMonitoring": "Отчет о мониторинге крупного проекта",
            "fcsCustomerReportRusProductsPurchasesVolume": "Отчет об объеме закупок российских товаров",
            "closedCustomerReportRusProductsPurchasesVolume": "Закрытый отчет об объеме закупок российских товаров",
            "fcsCustomerReportSingleContractor": "Отчет о закупках у единственного поставщика",
            "fcsCustomerReportSingleContractorInvalid": "Отчет о недействительных закупках у ЕП",
            "fcsCustomerReportContractExecutionInvalid": "Отчет о недействительном исполнении контракта",
            "fcsCustomerReportSmallScaleBusinessInvalid": "Отчет о недействительных закупках у СМП",
            "fcsCustomerReportBigProjectMonitoringInvalid": "Отчет о недействительном мониторинге крупного проекта",
            "fcsCustomerReportRusProductsPurchasesVolumeInvalid": "Отчет о недействительном объеме закупок российских товаров",
            "closedCustomerReportRusProductsPurchasesVolumeInvalid": "Закрытый отчет о недействительном объеме закупок",
            "purchasePlan": "План закупок",
            "purchasePlanProject": "Проект плана закупок",

            # ФЗ-223    
            "purchaseRejection": "Отказ от проведения закупки",
            "protocolLotAllocation": "Протокол о распределении лотов",
            "changeRequirements": "Изменение требований",
            "purchaseLotCancellation": "Отмена лота",
            "explanation": "Разъяснение",
            "explanationRequest": "Запрос разъяснений",
            "biddingTimeInfo": "Информация о времени проведения торгов",
            "purchaseProtocol": "Протокол закупки (общий)",
            "purchaseProtocolRZ1AE94FZ": "Протокол РЗ (1 этап) запроса предложений 94-ФЗ",
            "purchaseProtocolPAAE94FZ": "Протокол ПА запроса предложений 94-ФЗ",
            "purchaseProtocolVK": "Протокол внеконкурсный",
            "purchaseProtocolRZOK": "Протокол РЗ открытый конкурс",
            "purchaseProtocolRZOA": "Протокол РЗ открытый аукцион",
            "purchaseProtocolRZAE": "Протокол РЗ запрос предложений",
            "purchaseProtocolRZ2AE94FZ": "Протокол РЗ (2 этап) запроса предложений 94-ФЗ",
            "purchaseProtocolPAEP": "Протокол ПА электронная процедура",
            "purchaseProtocolPAOA": "Протокол ПА открытый аукцион",
            "purchaseProtocolPAAE": "Протокол ПА запрос предложений",
            "purchaseProtocolOSZ": "Протокол ОСЗ (объединенная закупка)",
            "purchaseProtocolZK": "Протокол запроса котировок",
            "purchaseProtocolRZ1KESMBO": "Протокол РЗ (1 этап) закрытого запроса котировок СМБ",
            "purchaseProtocolRZ2KESMBO": "Протокол РЗ (2 этап) закрытого запроса котировок СМБ",
            "purchaseProtocolRZ1AESMBO": "Протокол РЗ (1 этап) запроса предложений СМБ",
            "purchaseProtocolRZ2AESMBO": "Протокол РЗ (2 этап) запроса предложений СМБ",
            "purchaseProtocolRZ1ZPESMBO": "Протокол РЗ (1 этап) запроса предложений СМБ (альтернативный)",
            "purchaseProtocolRZ2ZPESMBO": "Протокол РЗ (2 этап) запроса предложений СМБ (альтернативный)",
            "purchaseProtocolFCDKESMBO": "Протокол ФЦД закрытого запроса котировок СМБ",
            "purchaseProtocolFKVOKESMBO": "Протокол ФКВО закрытого запроса котировок СМБ",
            "purchaseProtocolFCODKESMBO": "Протокол ФЦОД закрытого запроса котировок СМБ",
            "purchaseProtocolSummingupKESMBO": "Протокол подведения итогов закрытого запроса котировок СМБ",
            "purchaseProtocolSummingupAESMBO": "Протокол подведения итогов запроса предложений СМБ",
            "purchaseProtocolSummingupZKESMBO": "Протокол подведения итогов запроса котировок СМБ",
            "purchaseProtocolZRPZZPESMBO": "Протокол ЗРПЗ запроса предложений СМБ",
            "purchaseProtocolCollationAESMBO": "Протокол сопоставления запроса предложений СМБ",
            "purchaseProtocolRZZKESMBO": "Протокол РЗ закрытого запроса котировок СМБ",
            "purchaseProtocolSummingupZPESMBO": "Протокол подведения итогов запроса предложений СМБ",
            "purchaseProtocolFCDZPESMBO": "Протокол ФЦД запроса предложений СМБ",
            "purchaseProtocolAdditionalCollationKESMBO": "Протокол дополнительного сопоставления закрытого запроса котировок СМБ",
            "purchaseProtocolFCODZPESMBO": "Протокол ФЦОД запроса предложений СМБ",
            "protocolCancellation": "Протокол об отмене",

            "purchaseContract": "Контракт по результатам закупки",

            "orderClause": "Положение о закупке",

            "purchaseContractAccount": "Отчетность по договору",
            "planMonitoringConclusion": "Заключение по плану мониторинга",
            "decisionSuspension": "Решение о приостановлении",
            "disagreementProtocol": "Протокол разногласий",
            "missedNotice": "Уведомление о нарушении сроков",
            "notificationIssue": "Уведомление о выявленных нарушениях",
            "stopCommodity": "Информация о приостановке оборота товара"
        }

        event = ""
        changes = []

        if xml_name in notification_events:
            if xml_version == 0:
                event = "Опубликована закупка"
            else:
                event = "Изменена закупка"
                
        elif xml_name in ("contract", "pprf615Contract", "contractCutted"):
            if xml_version == 0:
                event = "Заключен контракт/договор"
            else:
                event = "Изменен контракт/договор"
        else: 
            event = events_mapping.get(xml_name)

        return event, changes
        








    async def get_xml_texts(
        self, 
        archive_links: set,
        entry_names: set
    ):
        """
        """
        # === Парсинг ЕИС архивов
        parsing_tasks = [
            self.cloud_parser._download_http_file(
                url=url,  
                source="inner_archives",
                original_filename=url.split("/")[-1]
            )
            for url in archive_links
        ]

        async with self.parse_semaphore:
            parsing_results = await asyncio.gather(*parsing_tasks, return_exceptions=True)


        async def process_single_file(file_path, relative_name):
            """
            Чтение и нормализация xml файла"""
            try:
                async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                    raw = await f.read()

                normalized = remove_keys(normalize_xml(raw), IGNORED_FIELDS)
                return {f"{relative_name}": normalized}

            except Exception as e:
                return f"[Ошибка чтения: {e}]"


        async def process_archive(parsing_result: Dict[str, Any]):
            """
            Распаковка и чтение xml файлов
            """
            with tempfile.TemporaryDirectory() as tmpdir:

                await self.file_reader.extract_archive(
                    parsing_result.get("file_path"), 
                    parsing_result.get("file_extension"), 
                    tmpdir
                )

                # === Чтение xml файлов
                tasks = []
                for root, _, files in os.walk(tmpdir):
                    for filename in files:

                        if filename not in entry_names or filename in xml_texts.keys():
                            continue

                        full_path = os.path.join(root, filename)

                        relative_name = os.path.relpath(
                            full_path,
                            tmpdir
                        )

                        tasks.append(
                            process_single_file(
                                full_path,
                                relative_name
                            )
                        )

                return await asyncio.gather(*tasks)


        # === Распаковка архивов
        xml_tasks = []
        for result in parsing_results:
            if isinstance(result, Exception):
                logger.error(f"Ошибка скачивания архива: {result}")
                continue
            if result.get("status") == "success":
                xml_tasks.append(process_archive(result))            

        xml_texts = {}
        results = await asyncio.gather(*xml_tasks).items()
        for key, value in results:
            xml_texts[key] = value

        return xml_texts