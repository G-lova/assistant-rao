import asyncio
import json
import pandas as pd
import asyncio
import datetime

from configs.config import Config
from configs.data_fetcher import DataFetcher
from configs.file_reader import FileReader
from configs.http_client_manager import HTTPClientManager
from configs.llm_client import get_llm
from configs.logger import get_logger
from configs.utils import split_large_text
from configs.working_with_db import save_raw_data, save_summary_report, delete_procurement_data
from evaluate_documents.completeness_checker import CompletenessChecker
from evaluate_documents.consistency_checker import ConsistencyChecker
from configs.parsing import CloudStorageParser
from evaluate_documents.type_data_extractor import DOCUMENT_TYPE_MAPPING, TypeDataExtractor


logger = get_logger(__name__)


class TasksPipeline:
    """
    
    """

    def __init__(self, http_manager: HTTPClientManager, expertise_id: int, environment: str = None):
        """
        
        """

        # Загрузка конфигурации
        client, model = get_llm()
        self.config = Config()
        self.http_manager = http_manager
        
        # Инициализация компонентов с конфигурацией
        db_config = self.config.get_database_config(environment)
        paths_config = self.config.get_paths_config()
        
        self.file_storage = db_config.storage_path
        self.data_fetcher = DataFetcher(db_config.url, db_config.headers, self.http_manager)        
        self.sql_queries_path = paths_config.sql_queries

        # try:
        # Загрузка SQL запроса и получение данных
        sql_query = self.load_sql_query("evaluate_docs_script.sql")        
        self.df = self.data_fetcher.fetch_expertise_data(sql_query, bindings=[self.file_storage, expertise_id])

        #     return df
        
        # except Exception as e:
        #     if str(e) == "Нет данных для анализа":
        #         return []
        #     else:
        #         raise
    
        self.file_reader = FileReader(client, model)
        self.type_data_extractor = TypeDataExtractor(client, model)
        self.completeness_checker = CompletenessChecker(client, model)
        self.consistency_checker = ConsistencyChecker(client, model)
        self.cloud_parser = CloudStorageParser(self.http_manager, self.df['object'].iloc[0])
        self.llm_semaphore = asyncio.Semaphore(3)

    def load_sql_query(self, file_name: str) -> str:
        """
        Загружает и нормализует SQL-запрос из файла.

        Читает содержимое SQL-файла из предопределённой директории и удаляет лишние пробелы и переносы,
        возвращая запрос в виде одной строки для корректной передачи в HTTP-запрос.

        Args:
            file_name (str): Имя файла с SQL-запросом (например, "get_experts.sql").

        Returns:
            str: SQL-запрос в виде одной строки без лишних пробельных символов.
        """
        file_path = f"{self.sql_queries_path}{file_name}"
        with open(file_path, encoding="utf-8") as f:
            sql_query = f.read()
        return " ".join(sql_query.split())


    async def process_link(self, link):
        """
        
        """
        try:
            parse_result = await self.cloud_parser.parse_cloud_storage_link(link.media_links, link.id)
            logger.info(f'Результат парсинга {link.media_links}: {parse_result}')

            tasks = []
            for file_info in parse_result.get("files", [parse_result]):
                logger.info(f'file_info: {file_info}')
                if parse_result.get("status") == "success":
                    file_path = file_info.get("file_path")
                    filename = file_info.get("filename", f"doc_from_link_{file_info.get('original_url')}")
                    tasks.append(self.process_parse_result(file_path, filename, link))
            async with self.llm_semaphore:
                results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Обрабатываем результаты
            successful_results = []
            for file_info, result in zip(parse_result.get("files", [parse_result]), results):
                if isinstance(result, Exception):
                    logger.error(f'Ошибка при обработке документа {file_info.get("filename", file_info.get("original_url"))}: {result}')
                    continue
                if result != None:
                    result['raw_data']['url'] = file_info.get("original_url")
                    successful_results.append(result)

            if link.doc_code == 'linkDocs':  
                eis_procurement_number = parse_result.get("procurement_number")
                eis_error = parse_result.get("error")

                eis_data = {
                    "document_name": "Ссылка на ЕИС",
                    "eis_procurement_number": str(eis_procurement_number) if eis_procurement_number else None,
                    "url": link.media_links,
                    "eis_status": "available" if parse_result.get("status") == "success" else "unavailable",
                    "eis_error": eis_error,
                    "checked_at": datetime.datetime.utcnow().isoformat()
                }    

                result = {                     
                    "type_compliance": {
                        "status": "allow" if parse_result.get("status") == "success" else "deny",
                        "detected_type": "linkDocs",
                        "issues": [parse_result.get("error")]
                    },
                    "readability": {
                        "status": "allow" if parse_result.get("status") == "success" else "deny",
                        "issues": [parse_result.get("error")]
                    },
                    "raw_data": eis_data
                }

                successful_results.append(result)

            return successful_results

        except Exception as e:

            if link.doc_code == 'linkDocs':  
            # if "zakupki.gov.ru" in link.media_links:
                logger.error(f"Ошибка обработки ЕИС-ссылки {link.media_links}: {e}")
                eis_data = {
                    "document_name": "Ссылка на ЕИС",
                    "url": link.media_links,
                    "eis_status": "error",
                    "eis_error": str(e),
                    "checked_at": datetime.datetime.utcnow().isoformat()
                }
                
                fallback = {                     
                    "type_compliance": {
                        "status": "deny",
                        "detected_type": "linkDocs",
                        "issues": [f"Ошибка обработки ЕИС-ссылки: {str(e)}"]
                    },
                    "readability": {
                        "status": "deny",
                        "issues": [f"Ошибка обработки ЕИС-ссылки: {str(e)}"]
                    },
                    "raw_data": eis_data
                }

            else:
                logger.warning(f"Критическая ошибка при обработке ссылки {link.media_links}: {e}")

                fallback = {
                    "type_compliance": {
                        "status": "deny",
                        "detected_type": "unknown",
                        "issues": [f"Не удалось обработать документ: {str(e)}"]
                    },
                    "readability": {
                        "status": "deny",
                        "issues": [f"Ошибка обработки: {str(e)}"]
                    },
                    "raw_data": {}
                }
            
            return fallback


    async def process_parse_result(self, file_path, filename, link):
        """
        
        """
        extracted_text = await self.file_reader.read_file(file_path, filename)
        if not extracted_text or "[Нет читаемого текста]" in extracted_text:
            raise ValueError("Не удалось извлечь текст")

        chunks = split_large_text(extracted_text, max_chunk_size=10000)
        if not chunks:
            fallback = {
                "type_compliance": {"status": "deny", "detected_type": "unknown", "issues": ["Пустой документ"]},
                "readability": {"status": "deny", "issues": ["Пустой документ"]},
                "raw_data": {},
                "completeness": {"status": "deny", "description": "Документ пуст или не содержит читаемого текста"}
            }
            return fallback
            
        # ====== Определение типа документа, оценка читаемости и извлечение ключевых данных ======        
        extracted_data = await self.type_data_extractor.extract_data_from_document(chunks, filename, link.doc_code, link.object)
        if isinstance(extracted_data, str):
            extracted_data = json.loads(extracted_data)

        return extracted_data
    

    async def run_pipeline(self):
        """
        
        """
        try:
            logger.info(f"Запуск фоновой задачи обработки документов", extra={"expertise_id": self.df['id'].iloc[0]})

            # формирование итоговых данных для подачи в финальный запрос
            data_for_final_evaluation = {
                "expertise_id": self.df['id'].iloc[0],
                "organization": self.df['organization'].iloc[0],
                "object": self.df['expertise_object'].iloc[0],
                "law_reference": self.df['law_reference'].iloc[0],
                "procurement_method": self.df['procurement_method'].iloc[0],
                "expertise_details": self.df['expertise_details'].iloc[0]
            }
                                    
            # добавление дополнительных данных для финального анализа    
            dop_fields = {
                "contract": "contract_number",
                "dateContract": "date_contract",
                "subjectContract": "subject_contract"
            }
            for k, v in dop_fields.items():
                if self.df.loc[self.df['doc_code'] == k, 'required_docs'].iloc[0] == 1:
                    data_for_final_evaluation[v] = self.df[k].iloc[0]

            # определение типа документа для правильной категоризации и оценки полноты
            obj = self.df['object'].iloc[0]
            check_type2 = self.df['checkType2'].iloc[0]
            type_ = self.df['type'].iloc[0]

            if obj in [5, 6]:
                contract_replacement = "docProjContractFiles"
            elif check_type2 == 13 or type_ == 2:
                contract_replacement = "docContractNIRFiles"
            elif check_type2 == 14 or type_ == 3:
                contract_replacement = "docContractPostTovarFiles"
            else:
                contract_replacement = "docContractDoWorkFiles"

            self.df['doc_code'] = self.df['doc_code'].replace({
                "contractFiles": contract_replacement,
                "dopMaterialFiles": "docDopMaterialsFiles", 
                "dopContractFiles": "docDopConsentContractFiles", 
                "docFiles": "docValidAllIfFiles", 
                "rao": "docExpertReportFiles", 
                "our": "docExpertReportFiles"
            })

            # создание нового признака для сохранения результатов проверки документов по типам документов
            self.df['documents_results'] = [[] for _ in range(len(self.df))]

            # === Парсинг ссылок ===
            media_links = self.df[self.df['media_links'].notna()]
            if len(media_links) > 0:
                media_links['media_links'] = media_links['media_links'].apply(json.loads)
                media_links_exploded = media_links.explode('media_links')
                media_links_exploded = media_links_exploded[media_links_exploded['media_links'].notna()]
                
                async with self.llm_semaphore:
                    extracted_results = await asyncio.gather(*(self.process_link(link) for link in media_links_exploded.itertuples()))

                # Обрабатываем результаты
                successful_extracted_results = []
                for link, result in zip(media_links_exploded.itertuples(), extracted_results):
                    if isinstance(result, Exception):
                        logger.error(f"Ошибка при анализе ссылки {link}: {result}")
                        continue
                    if result != None:
                        successful_extracted_results.append(result)

                # Заполняем данные о предоставленных документах и результаты анализа документов в датафрейме
                for link, result in zip(media_links_exploded.itertuples(), successful_extracted_results):
                    if not isinstance(result, list):
                        result = [result]
                    for res in result:
                        if isinstance(res, str):
                            res = json.loads(res)
                        detected_type = res.get('type_compliance') if isinstance(res.get('type_compliance'), str) else res.get('type_compliance', {}).get("detected_type", "unknown")
                        # заполнение столбца данными о предоставленных документах 
                        # if link.doc_code == 'linkDocs':
                        #     self.df.loc[self.df['doc_code'] == 'linkDocs', 'provided_docs'] = 1
                        self.df.loc[self.df['doc_code'] == detected_type, 'provided_docs'] = 1
                        self.df.loc[self.df['doc_code'] == (detected_type if (
                            link.doc_code == 'linkDocs' and detected_type in DOCUMENT_TYPE_MAPPING.keys()
                            ) else link.doc_code), 'documents_results'].iloc[0].append(res)
                        # self.df.loc[self.df['doc_code'] == (detected_type if (link.doc_code in {"contractFiles", "dopMaterialFiles", "dopContractFiles", "docFiles", "rao", "our"}) else link.doc_code
                        #                                     ), 'documents_results'].iloc[0].append(res)
                        # self.df.loc[self.df['doc_code'] == link.doc_code, 'documents_results'].iloc[0].append(res)


            # Определяем недостающие документы
            self.df['missed_docs'] = (self.df['required_docs'].where(self.df['required_docs'] == 1) - self.df['provided_docs']).fillna(0).astype(int)

            # формирование итоговых данных для подачи в финальный запрос
            data_for_final_evaluation["missed_documents"] = self.df.loc[self.df['missed_docs'] == 1, 'doc_code'].map(DOCUMENT_TYPE_MAPPING).dropna().unique().tolist()
            data_for_final_evaluation["documents"] = []
            # ====== Оценка полноты и соответствия данных в документе ======
            completeness_tasks = []
            for row in self.df[self.df['documents_results'].map(bool)].itertuples():
                data_for_completeness = {
                    **data_for_final_evaluation, 
                    "doc_code": row.doc_code, 
                    "doc_type": 'Ссылка на ЕИС' if row.doc_code == 'linkDocs' else DOCUMENT_TYPE_MAPPING.get(row.doc_code, "Неизвестный документ"),
                    "documents": row.documents_results
                }
                completeness_tasks.append(self.completeness_checker.check_doc_completeness(self.consistency_checker.remove_empty(data_for_completeness), row.doc_code))

            async with self.llm_semaphore:
                completeness_results = await asyncio.gather(*completeness_tasks, return_exceptions=True)
                
            for completeness, row in zip(completeness_results, self.df[self.df['documents_results'].map(bool)].itertuples()):
                if isinstance(completeness, Exception):
                    logger.error(f"Ошибка при оценке полноты документов типа {row.doc_code}")
                    continue
                if isinstance(completeness, str):
                    completeness = json.loads(completeness)
                data_for_final_evaluation["documents"].append({
                    "doc_code": row.doc_code,
                    "doc_type": 'Ссылка на ЕИС' if row.doc_code == "linkDocs" else DOCUMENT_TYPE_MAPPING.get(row.doc_code, "Неизвестный документ"),
                    "required": 'Обязательный' if row.required_docs == 1 else 'Необязательный',
                    "empty_comment": row.empty_comment,
                    **completeness
                })

            # сортировка документов внутри data_for_final_evaluation["documents"] по приоритету (обязательные документы и ссылка на ЕИС в приоритете)
            data_for_final_evaluation["documents"].sort(key=lambda x: (x["required"] != "Обязательный", x["doc_code"] != "linkDocs"))


            # === Очистка старых данных ===
            try:
                await delete_procurement_data(self.df['id'].iloc[0])
                logger.info(f"Запись в БД удалена {self.df['id'].iloc[0]}")
            except:
                logger.info(f"Запись в БД не существует {self.df['id'].iloc[0]}")

                
            
            # сохранение данных в БД
            await save_raw_data(procurement_id=self.df.id.iloc[0], analysis=data_for_final_evaluation["documents"]) 
                        
            # ЭТАП 3: Оценка согласованности и эвристик и формирование финального отчета
            # logger.info(f"type: {type(data_for_final_evaluation)}, data_for_final_evaluation: {data_for_final_evaluation}")
            final_evaluation_result = await self.consistency_checker.check_consistency(data_for_final_evaluation)
            await save_summary_report(procurement_id=self.df['id'].iloc[0], summary_data=final_evaluation_result)

            return final_evaluation_result
            
        except Exception as e:
            logger.error(f"Error in evaluator pipeline: {e}", exc_info=True)
            raise