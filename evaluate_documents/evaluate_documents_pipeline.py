import aiofiles
import asyncio
import json
import os
import pandas as pd
import asyncio
import datetime

from configs.config import Config
from configs.data_fetcher import DataFetcher
from configs.file_reader import read_file
from configs.llm_client import get_llm
from configs.logger import get_logger
from configs.utils import split_large_text
from configs.working_with_db import save_raw_data, save_summary_report, delete_procurement_data
from evaluate_documents.completeness_checker import CompletenessChecker
from evaluate_documents.consistency_checker import ConsistencyChecker
from configs.parsing import parse_cloud_storage_link
from configs.retry_utils import async_retry, API_RETRY_CONFIG, CLOUD_PARSING_RETRY_CONFIG
from evaluate_documents.type_data_extractor import DOCUMENT_TYPE_MAPPING, TypeDataExtractor


logger = get_logger(__name__)


class EvaluateDocumentsPipeline:
    """
    Конвейер для оценки и ранжирования экспертов на основе схожести текстов и дополнительных метрик.

    Выполняет полный цикл обработки: загрузку данных из БД по заданному SQL-запросу,
    предобработку текстов, вычисление эмбеддингов и косинусного сходства, фильтрацию по конфликтам интересов
    и расчёт итогового рейтинга экспертов. Используется для подбора наиболее подходящих экспертов
    к конкретной экспертизе.
    """

    def __init__(self, environment: str = None):
        """
        Инициализирует компоненты конвейера с использованием глобальной конфигурации.

        Загружает настройки из конфигурационного файла и создаёт экземпляры зависимостей:
        - DataFetcher — для получения данных из базы.
        Также сохраняет путь к директории с SQL-запросами.
        """
        # Загрузка конфигурации
        self.config = Config()
        
        # Инициализация компонентов с конфигурацией
        db_config = self.config.get_database_config(environment)
        paths_config = self.config.get_paths_config()
        
        self.file_storage = db_config.storage_path
        self.data_fetcher = DataFetcher(db_config.url, db_config.headers)        
        self.sql_queries_path = paths_config.sql_queries
    

    async def load_sql_query(self, file_name: str) -> str:
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
        async with aiofiles.open(file_path, encoding="utf-8") as f:
            sql_query = await f.read()
        return " ".join(sql_query.split())

    

    async def run_pipeline(self, sql_file_path, expertise_id):
        """
        Запускает полный конвейер оценки экспертов для заданной экспертизы.

        Последовательно выполняет все этапы: загрузку данных, предобработку, расчёт сходства,
        фильтрацию по конфликтам и вычисление рейтинга.

        Args:
            sql_file_path (str): Имя файла с SQL-запросом для получения данных об экспертах.
            expertise_id (str или int): Уникальный идентификатор экспертизы.

        Returns:
            pandas.DataFrame: Датафрейм с отфильтрованными и ранжированными экспертами,
            содержащий колонки 'expert_id' и 'rating'.
        """
        try:
            # Загрузка SQL запроса
            sql_query = await self.load_sql_query(sql_file_path)
            
            # Получение данных
            df = await self.data_fetcher.fetch_async_expertise_data(sql_query, bindings=[self.file_storage, expertise_id])

            return df
        
        except Exception as e:
            if str(e) == "Нет данных для анализа":
                return []
            else:
                raise


class TasksPipeline:
    """
    
    """

    def __init__(self, df):
        """
        
        """
        self.df = pd.DataFrame(df) if not isinstance(df, pd.DataFrame) else df

        # Загрузка конфигурации
        client, model = get_llm()
        
        self.type_data_extractor = TypeDataExtractor(client, model)
        self.completeness_checker = CompletenessChecker(client, model)
        self.consistency_checker = ConsistencyChecker(client, model)


    async def process_link(self, link):
        """
        
        """
        try:
            parse_result = await parse_cloud_storage_link(link.media_links, link.id)
            logger.info(f'Результат парсинга {link.media_links}: {parse_result}')

            if "zakupki.gov.ru" in link.media_links:
                eis_procurement_number = parse_result.get("procurement_number")
                eis_status = "available" if parse_result.get("status") == "success" else "unavailable"
                eis_error = parse_result.get("error")

                eis_data = {
                    "document_name": "Ссылка на ЕИС",
                    "eis_procurement_number": str(eis_procurement_number) if eis_procurement_number else None,
                    "eis_link": link.media_links,
                    "eis_status": eis_status,
                    "eis_error": eis_error,
                    "checked_at": datetime.datetime.utcnow().isoformat()
                }
                
                result = {                     
                    "type_compliance": {
                        "status": "allow" if eis_status == "available" else "deny",
                        "detected_type": link.doc_code,
                        "issues": [eis_error]
                    },
                    "readability": {
                        "status": "allow" if eis_status == "available" else "deny",
                        "issues": [eis_error]
                    },
                    "raw_data": eis_data,
                    "completeness": {
                        "status": "allow" if eis_procurement_number else "deny",
                        "description": "Ссылка корректна" if eis_status == "available" and eis_procurement_number else "Некорректная ссылка"
                    }
                }
                return result

            else:
                tasks = []
                for file_info in parse_result.get("files", [parse_result]):
                    logger.info(f'file_info: {file_info}')
                    file_path = file_info.get("file_path")
                    filename = file_info.get("filename", f"doc_from_link_{file_info.get('original_url')}")
                    tasks.append(self.process_parse_result(file_path, filename, link))
                results = await asyncio.gather(*tasks)
                return results


        except Exception as e:
            print(f"Критическая ошибка при обработке ссылки {link.media_links}: {e}")

            if "zakupki.gov.ru" in link.media_links:
                logger.error(f"Ошибка обработки ЕИС-ссылки {link.media_links}: {e}")
                eis_data = {
                    "document_name": "Ссылка на ЕИС",
                    "eis_link": link.media_links,
                    "eis_status": "error",
                    "eis_error": str(e),
                    "checked_at": datetime.datetime.utcnow().isoformat()
                }
                
                fallback = {                     
                    "type_compliance": {
                        "status": "deny",
                        "detected_type": "linkDocs",
                        "issues": ["Ошибка обработки ЕИС-ссылки"]
                    },
                    "readability": {
                        "status": "deny",
                        "issues": ["Ошибка обработки ЕИС-ссылки"]
                    },
                    "raw_data": eis_data,
                    "completeness": {
                        "status": "deny",
                        "description": eis_error
                    }
                }
            else:
                fallback = {
                    "type_compliance": {
                        "status": "deny",
                        "detected_type": "unknown",
                        "issues": [f"Не удалось обработать документ: {e}"]
                    },
                    "readability": {
                        "status": "deny",
                        "issues": [f"Ошибка обработки: {e}"]
                    },
                    "raw_data": {
                    },
                    "completeness": {
                        "status": "deny",
                        "description": f"Документ '{filename}' не был проанализирован из-за ошибки: {e}"}
                }
            
            return fallback


    async def process_parse_result(self, file_path, filename, link):
        """
        
        """
        extracted_text = await read_file(file_path, filename)
        if not extracted_text or "[Нет читаемого текста]" in extracted_text:
            raise ValueError("Не удалось извлечь текст")

        chunks = split_large_text(extracted_text, max_chunk_size=20000)
        if not chunks:
            fallback = {
                "type_compliance": {"status": "deny", "detected_type": "unknown", "issues": ["Пустой документ"]},
                "readability": {"status": "deny", "issues": ["Пустой документ"]},
                "raw_data": {},
                "completeness": {"status": "deny", "description": "Документ пуст или не содержит читаемого текста"}
            }
            return fallback
            
        # ====== Определение типа документа, оценка читаемости и извлечение ключевых данных ======        
        extracted_data = await self.type_data_extractor.extract_data_from_document(chunks, filename, link.doc_code)
        if isinstance(extracted_data, str):
            extracted_data = json.loads(extracted_data)

        # ====== Оценка полноты и соответствия данных в документе ======
        data_for_final_evaluation = {
            "expertise_id": self.df['id'].iloc[0],
            "organization": self.df['organization'].iloc[0],
            "object": self.df['expertise_object'].iloc[0],
            "law_reference": self.df['law_reference'].iloc[0],
            "procurement_method": self.df['procurement_method'].iloc[0],
            "expertise_details": self.df['expertise_details'].iloc[0]
        }
        data_for_completeness = {**data_for_final_evaluation, **extracted_data['raw_data']}
        completeness = await self.completeness_checker.check_doc_completeness(data_for_completeness, filename)
        if isinstance(completeness, str):
            completeness = json.loads(completeness)

        # собираем результаты обработки документов в единый массив для подачи в llm
        result = {"completeness": completeness, **extracted_data}
        return result


    async def process_file(self, file_path, filename, link):
        """
        
        """
        extracted_text = await read_file(file_path, filename)
        if not extracted_text or "[Нет читаемого текста]" in extracted_text:
            raise ValueError("Не удалось извлечь текст")

        chunks = split_large_text(extracted_text, max_chunk_size=20000)
        if not chunks:
            fallback = {
                "type_compliance": {"status": "deny", "detected_type": "unknown", "issues": ["Пустой документ"]},
                "readability": {"status": "deny", "issues": ["Пустой документ"]},
                "raw_data": {},
                "completeness": {"status": "deny", "description": "Документ пуст или не содержит читаемого текста"}
            }
            return fallback
            
        # ====== Определение типа документа, оценка читаемости и извлечение ключевых данных ======        
        extracted_data = await self.type_data_extractor.extract_data_from_document(chunks, filename, link.doc_code)
        if isinstance(extracted_data, str):
            extracted_data = json.loads(extracted_data)

        # ====== Оценка полноты и соответствия данных в документе ======
        data_for_final_evaluation = {
            "expertise_id": self.df['id'].iloc[0],
            "organization": self.df['organization'].iloc[0],
            "object": self.df['expertise_object'].iloc[0],
            "law_reference": self.df['law_reference'].iloc[0],
            "procurement_method": self.df['procurement_method'].iloc[0],
            "expertise_details": self.df['expertise_details'].iloc[0]
        }
        data_for_completeness = {**data_for_final_evaluation, **extracted_data['raw_data']}
        completeness = await self.completeness_checker.check_doc_completeness(data_for_completeness, filename)
        if isinstance(completeness, str):
            completeness = json.loads(completeness)

        # собираем результаты обработки документов в единый массив для подачи в llm
        result = {"completeness": completeness, **extracted_data}
        return result
    

    async def run_pipeline(self):
        """
        
        """
        # === Очистка старых данных ===
        try:
            delete_procurement_data(self.df['id'].iloc[0])
            logger.info(f"Запись в БД удалена {self.df['id'].iloc[0]}")
        except:
            logger.info(f"Запись в БД не существует {self.df['id'].iloc[0]}")

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

            # создание нового признака для сохранения результатов проверки документов по типам документов
            self.df['documents_results'] = [[] for _ in range(len(self.df))]

            # === Парсинг ссылок ===
            tasks = []
            media_links = self.df[self.df['media_links'].notna()]
            if len(media_links) > 0:
                media_links['media_links'] = media_links['media_links'].apply(json.loads)
                media_links_exploded = media_links.explode('media_links')
                media_links_exploded = media_links_exploded[media_links_exploded['media_links'].notna()]
                for link in media_links_exploded.itertuples():
                    tasks.append(self.process_link(link))

                results = await asyncio.gather(*tasks)
                for link, result in zip(media_links_exploded.itertuples(), results):
                    if not isinstance(result, list):
                        result = [result]
                    for res in result:
                        detected_type = res.get('type_compliance', {}).get("detected_type", "unknown")
                        # заполнение столбца данными о предоставленных документах 
                        if link.doc_code == 'linkDocs':
                            self.df.loc[self.df['doc_code'] == 'linkDocs', 'provided_docs'] = 1
                        self.df.loc[self.df['doc_code'] == detected_type, 'provided_docs'] = 1
                        # self.df.loc[self.df['doc_code'] == (detected_type if (link.doc_code == 'linkDocs') else link.doc_code), 'documents_results'].iloc[0].append(res)
                        self.df.loc[self.df['doc_code'] == link.doc_code, 'documents_results'].iloc[0].append(res)

            # сохранение данных в БД
            for row in self.df[self.df['documents_results'].map(bool)].itertuples():
                save_raw_data(procurement_id=self.df.id.iloc[0], document_code=row.doc_code, analysis=row.documents_results)

                                    
            # добавление дополнительных данных для финального анализа    
            dop_fields = {
                "contract": "Реквизиты контракта",
                "dateContract": 'Дата контракта',
                "subjectContract": "Предмет контракта"
            }
            for k, v in dop_fields.items():
                if self.df.loc[self.df['doc_code'] == k, 'required_docs'].iloc[0] == 1:
                    result = {
                        "document_code": k,
                        "raw_data": {
                            "document_name": v,
                            "contract_number": self.df.loc[self.df['doc_code'] == k, k].iloc[0]
                        }
                    }
                    self.df.loc[self.df['doc_code'] == k, 'documents_results'].iloc[0].append(result)


            # Определяем недостающие документы
            self.df['missed_docs'] = (self.df['required_docs'].where(self.df['required_docs'] == 1) - self.df['provided_docs']).fillna(0).astype(int)

            # формирование итоговых данных для подачи в финальный запрос
            data_for_final_evaluation["missed_documents"] = self.df.loc[self.df['missed_docs'] == 1, 'doc_code'].map(DOCUMENT_TYPE_MAPPING).dropna().unique().tolist()
            data_for_final_evaluation["documents"] = []


            df_prepared = self.df[self.df['documents_results'].map(bool)]


            for _, row in df_prepared.iterrows():
                for item in row['documents_results']:
                    documents_data = {
                        "document_code": row['doc_code'],
                        "document_type": DOCUMENT_TYPE_MAPPING.get(row['doc_code'], "Неизвестный документ"),
                        "required": 'Обязательный' if row['required_docs'] == 1 else 'Необязательный',
                        "empty_comment": row['empty_comment'],
                        **item
                    }
                
                    data_for_final_evaluation['documents'].append(documents_data)
                        
            # ЭТАП 3: Оценка согласованности и эвристик и формирование финального отчета
            # logger.info(f"type: {type(data_for_final_evaluation)}, data_for_final_evaluation: {data_for_final_evaluation}")
            final_evaluation_result = await self.consistency_checker.check_consistency(data_for_final_evaluation)
            save_summary_report(procurement_id=self.df['id'].iloc[0], summary_data=final_evaluation_result)

            return final_evaluation_result
            
        except Exception as e:
            logger.error(f"Error in evaluator pipeline: {e}", exc_info=True)
            raise