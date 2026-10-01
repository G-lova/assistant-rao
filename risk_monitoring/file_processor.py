import asyncio
import json
import datetime
import math
import os
import sys
from typing import Any, List
from configs.data_fetcher import DataFetcher
from evaluate_documents.data_extractor import DataExtractor
from evaluate_documents.type_detector import TypeDetector
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

from configs.config import Config
from configs.file_reader import FileReader
from configs.http_client_manager import HTTPClientManager
from configs.llm_client import get_llm
from configs.logger import get_logger
from configs.parsing import CloudStorageParser
from configs.utils import load_sql_query_async, split_large_text
from evaluate_documents.consistency_checker import ConsistencyChecker
from evaluate_documents.type_data_extractor import ALLOWED_DOC_TYPES, DOCUMENT_TYPE_MAPPING, TypeDataExtractor
from risk_monitoring.doc_risks_detector import DocumentRisksDetector
from risk_monitoring.send_ai_analysis_service import SendAIAnalysisService
from search_experts.embedding_client import EmbeddingClient


# Добавляем путь к проекту для корректного импорта
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.insert(0, project_root)


logger = get_logger(__name__)


class FileProcessor:
    """
    
    """

    def __init__(self, http_manager: HTTPClientManager, environment: str = None):
        """
        
        """

        # Загрузка конфигурации
        client, model = get_llm()
        self.config = Config()
        
        # Инициализация компонентов с конфигурацией
        db_config = self.config.get_database_config(environment)
        paths_config = self.config.get_paths_config()
        embedding_config = self.config.get_embedding_config()
        
        self.file_storage = db_config.storage_path      
        self.sql_queries_path = paths_config.sql_queries
    
        self.file_reader = FileReader(client, model)
        self.data_fetcher = DataFetcher(db_config.url, db_config.headers, http_manager)
        self.type_data_extractor = TypeDataExtractor(client, model)
        self.type_detector = TypeDetector(client, model)
        self.data_extractor = DataExtractor(client, model)
        self.doc_risks_detector = DocumentRisksDetector(client, model)
        self.send_ai_analysis_service = SendAIAnalysisService(http_manager, environment)
        self.cloud_parser = CloudStorageParser(http_manager, None)
        self.consistency_checker = ConsistencyChecker(client, model)
        self.embedding_client = EmbeddingClient(
            embedding_config.api_url,
            embedding_config.api_key,
            embedding_config.model,
            batch_size=embedding_config.batch_size,
            http_manager=http_manager
        )


    async def get_doc_text(
        self, 
        url: str,
        source: str,
        filename: str,
        contract_id: str = None
    ):
        """
        
        """
        try:
            # === 1. Парсинг документа
            logger.info(f"Парсинг документа {filename}")
            metadata = await self.cloud_parser._download_http_file(
                url=url, 
                procurement_id=contract_id, 
                source=source,
                original_filename=filename
            )
            logger.info(f"Результат парсинга документа {filename}: {metadata}")

        except Exception as e:
            logger.error(f"Ошибка парсинга документа {filename}: {e}")
            return ""

        try:
            # === 2. Чтение документа
            logger.info(f"Чтение документа {filename}")
            text = await self.file_reader.read_file(
                file_path=metadata.get("file_path"), 
                original_filename=metadata.get("filename")
            )

            return text

        except Exception as e:
            logger.error(f"Ошибка чтения документа {filename}: {e}")
            return ""


    async def analyze_doc_text(
        self, 
        id: int,
        text: str,
        filename: str,
        source: str,
        context: str = None,
        send_to_external: bool = False
    ):
        """
        
        """
        try:
            if not text:
                return {
                    "doc_type": {
                        "detected_type": "unknown", 
                        "type_decode": "Неизвестный документ",
                        "issues": ["Пустой документ"]
                    },
                    "readability": {
                        "status": "deny", 
                        "issues": ["Пустой документ"]
                    },
                    "raw_data": {},
                    "emdeddings": [],
                    "similar_doc_ids": [],
                    "resume": "Документ пуст или не содержит читаемого текста", 
                    "confidence": 0.0,
                    "risks": [
                        {
                            "rule_id": "DOC-001",
                            "title": "Неполнота документа",
                            "description": "Документ пуст или не содержит читаемого текста"
                        }
                    ],
                    "model": self.config.get_m_model_config().model,
                    "analyzed_at": datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
                }


            # === 3. Извлечение данных из документа
            logger.info(f"Извлечение данных из документа {filename}")
            chunks = split_large_text(text=text, max_chunk_size=10000)
            if not chunks:
                return {
                    "doc_type": {
                        "detected_type": "unknown", 
                        "type_decode": "Неизвестный документ",
                        "issues": ["Пустой документ"]
                    },
                    "readability": {
                        "status": "deny", 
                        "issues": ["Пустой документ"]
                    },
                    "raw_data": {},
                    "emdeddings": [],
                    "similar_doc_ids": [],
                    "resume": "Документ пуст или не содержит читаемого текста", 
                    "confidence": 0.0,
                    "risks": [
                        {
                            "rule_id": "DOC-001",
                            "title": "Неполнота документа",
                            "description": "Документ пуст или не содержит читаемого текста"
                        }
                    ],
                    "model": self.config.get_m_model_config().model,
                    "analyzed_at": datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
                }

            extracted_data = await self.data_extractor.extract_data_from_document(
                chunks=chunks, 
                filename=filename
            )
            
            if isinstance(extracted_data, str):
                extracted_data = json.loads(extracted_data)

            # === 4. Определение типа документа
            type_info = await self.type_detector.detect_document_type(content=chunks[0], filename=filename)
            detected_type = type_info.get("detected_type", "")

            if detected_type not in ALLOWED_DOC_TYPES:
                document_name = extracted_data.get("document_name", "")
                detected_type = self.type_detector.detect_document_type_by_name(document_name)
            
            if "contract" not in source and detected_type in {'docProjContractFiles', 'docContractDoWorkFiles', 'docContractNIRFiles', 'docContractPostTovarFiles'}:
                detected_type = 'docProjContractFiles'
            
            type_info["detected_type"] = detected_type
            type_info["type_decode"] = DOCUMENT_TYPE_MAPPING.get(detected_type)


            # === 5. Извлечение эмбеддингов документа
            logger.info(f"Расчет эмбеддингов для документа '{filename}'")
            results = await asyncio.gather(
                *(self.embedding_client.get_embeddings(chunk) for chunk in chunks), 
                return_exceptions=True
            )
            
            embeddings = []
            doc_embedding = []
            similar_ids = []
            for res in results:
                if isinstance(res, Exception):
                    logger.warning(f"Не удалось получить эмбеддинг для чанка в {filename}: {res}")
                    continue
                embeddings.append(res)
            
            if embeddings:
                doc_embedding = self.build_document_embedding(embeddings)
                logger.info(f"Embeddings: {doc_embedding}")

                # === 6. Поиск похожих документов
                logger.info(f"Поиск похожих документов для '{filename}'")
                similar_ids = await self.get_similar_documents(
                    id=id,
                    doc_emb=doc_embedding
                )

                logger.info(f"ID похожих документов: {similar_ids}")

            else:
                logger.warning(f"Не удалось получить эмбеддинг для документа {filename}")


            # === 7. Анализ документа на риски
            risks_weights = {
                "fin": 0.2,
                "doc": 0.2,
                "proc": 0.2,
                "ctr": 0.2,
                "ai": 0.2
            }

            risk_types = {
                "fin": [],
                "doc": [],
                "proc": [],
                "ctr": [],
                "ai": []
            }

            analyse_content = {
                "doc_data": self.consistency_checker.remove_empty(extracted_data),
                "context": self.consistency_checker.remove_empty(context)
            }
            file_risks = await self.doc_risks_detector.check_doc_risks(
                content=json.dumps(analyse_content, ensure_ascii=False, indent=2), 
                filename=filename
            )
            if isinstance(file_risks, str):
                file_risks = json.loads(file_risks)

            all_risks = file_risks.get("risks", [])
            if all_risks:
                risks_filtered = [r for r in all_risks if r.get("confidence", 0) >= 0.8]
                file_risks["risks"] = risks_filtered

                # === 8. Расчет интегрального риска
                
                for r in file_risks.get("risks"):
                    rule_type = r.get("rule_id").split("-")[0].lower()
                    level = float(r.get("level", 0))
                    for k in risk_types.keys():
                        if rule_type in k:
                            risk_types[k].append(level)

            total_doc_risk = round(sum([(np.mean(v) if v else 0) * risks_weights.get(k) for k, v in risk_types.items()]), 4)


            # === 9. Сборка итогового анализа 
            ai_analysis = {
                "status": "completed",
                "doc_type": type_info,
                **self.consistency_checker.remove_empty(extracted_data),
                "embeddings": doc_embedding,
                "similar_doc_ids": similar_ids,
                **file_risks,
                "total_doc_risk": total_doc_risk if total_doc_risk else 0,
                "model": self.config.get_m_model_config().model,
                "analyzed_at": datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
            }
            logger.info(f"Результат полного анализа документа {filename}: {ai_analysis}")


            # === 10. Запись ИИ-анализа в БД
            await self.send_ai_analysis_service.send_to_db(
                table_name="risk_monitoring_files",
                id=id,
                ai_analysis=ai_analysis,
                send_to_external=send_to_external
            )

            return ai_analysis

        except Exception as e:
            logger.exception(f"Ошибка анализа документа {filename}: {e}")
            return {
                "doc_type": {
                    "detected_type": "unknown", 
                    "type_decode": "Неизвестный документ",
                    "issues": ["Ошибка анализа документа"]
                },
                "readability": {
                    "status": "deny", 
                    "issues": ["Ошибка анализа документа"]
                },
                "raw_data": {},
                "emdeddings": [],
                "similar_doc_ids": [],
                "resume": "Ошибка анализа документа", 
                "confidence": 0.0,
                "risks": [],
                "model": self.config.get_m_model_config().model,
                "analyzed_at": datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
            }


    async def process_document(
        self, 
        id: int,
        url: str,
        source: str,
        filename: str,
        contract_id: str = None,
        context: str = None,
        send_to_external: bool = False
    ):
        """
        
        """
        try:
            doc_text = await self.get_doc_text(
                url=url,
                source=source,
                filename=filename,
                contract_id=contract_id
            )

            ai_analysis = await self.analyze_doc_text(
                id=id,
                text=doc_text,
                filename=filename,
                source=source,
                context=context,
                send_to_external=send_to_external
            )

            return ai_analysis

        except Exception as e:
            logger.error(f"Ошибка обработки документа {filename}: {e}")
            return {
                "doc_type": {
                    "detected_type": "unknown", 
                    "type_decode": "Неизвестный документ",
                    "issues": ["Ошибка обработки документа"]
                },
                "readability": {
                    "status": "deny", 
                    "issues": ["Ошибка обработки документа"]
                },
                "raw_data": {},
                "emdeddings": [],
                "similar_doc_ids": [],
                "resume": "Ошибка обработки документа", 
                "confidence": 0.0,
                "risks": [],
                "model": self.config.get_m_model_config().model,
                "analyzed_at": datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
            }


    def build_document_embedding(self, chunk_vectors):
        if not chunk_vectors:
            return []
        
        processed_vectors = []
        for v in chunk_vectors:
            arr = np.asarray(v)
            # Схлопываем все лишние размерности до формы (N, D), 
            # где D - размерность эмбеддинга (последняя ось)
            if arr.ndim > 2:
                arr = arr.reshape(-1, arr.shape[-1])
            elif arr.ndim == 1:
                arr = arr.reshape(1, -1)
            processed_vectors.append(arr)
            
        # np.vstack корректно склеит массивы с разным количеством строк (N) 
        # в один общий 2D-массив формы (sum(N), D)
        vectors = np.vstack(processed_vectors)
        
        # Усредняем по всем векторам (ось 0), получаем итоговый вектор формы (D,)
        document_vector = vectors.mean(axis=0)
        
        # Вычисляем L2 норму
        norm = np.linalg.norm(document_vector)
        
        # Защита от деления на ноль
        if norm == 0:
            return document_vector.tolist()
            
        # Нормализуем и возвращаем строго 1D список (flat list)
        return (document_vector / norm).tolist()


    async def get_similar_documents(
            self,
            id: int,
            doc_emb: List[Any]
    ):
        # получение других ранее загруженных документов и их эмбеддингов
        comparing_docs = await self.data_fetcher.fetch_async_expertise_data(
                await load_sql_query_async("get_files_to_compare_emeddings.sql"), 
                bindings=[id]
            )
        if comparing_docs.empty:
            return []
        
        comparing_docs["embeddings"] = comparing_docs["embeddings"].apply(lambda x: json.loads(x) if isinstance(x, str) else x)

        # Расчет косинусного сходства
        try:
            comparing_embs_matrix = np.vstack(comparing_docs["embeddings"].tolist())
            
            similarities = cosine_similarity([doc_emb], comparing_embs_matrix).flatten()
            comparing_docs["similarity"] = similarities

            # comparing_docs["similarity"] = cosine_similarity(doc_emb, comparing_docs["embeddings"].to_list()).flatten()            
            # return comparing_docs_sorted.loc[comparing_docs_sorted["similarity"] >= 0.7, "id"].to_list()
            
            comparing_docs_sorted = comparing_docs.sort_values(by="similarity", ascending=False)
            comparing_docs_filtered = comparing_docs_sorted.loc[comparing_docs_sorted["similarity"] >= 0.75]

            similarity_ids = []
            for row in comparing_docs_filtered.itertuples():
                similarity_ids.append({
                    "id": row.id,
                    "similarity": row.similarity
                })

            return similarity_ids
            
        except Exception as e:
            logger.error(f"Ошибка при расчете косинусного сходства для документа {id}: {e}")
            return []