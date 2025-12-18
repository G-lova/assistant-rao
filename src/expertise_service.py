import json
from collections import OrderedDict
from typing import Dict, Any, Tuple, Optional
import logging

from configs.config import Config
from src.json_merger import JSONMerger
from search_experts.data_fetcher import DataFetcher

logger = logging.getLogger(__name__)

class ExpertiseService:
    """Сервис для работы с данными экспертиз в MySQL"""
    
    def __init__(self, data_fetcher: DataFetcher):
        self.data_fetcher = data_fetcher
        self.json_merger = JSONMerger()
    
    def get_expertise_jsons(self, expertise_id: str) -> Tuple[OrderedDict, OrderedDict]:
        """
        Получает два JSON-документа для указанной экспертизы из MySQL
        """
        #  SQL-ЗАПРОС ДЛЯ MYSQL
        sql_query = """
        SELECT 
            JSON_UNQUOTE(JSON_EXTRACT(data, '$')) AS json_data,
            expert_id,
            id
        FROM expertise_expert_opinion7s 
        WHERE expertise_id = ?
        ORDER BY expert_id
        LIMIT 2
        """
        
        logger.info(f" Выполнение SQL-запроса для MySQL expertise_id={expertise_id}")
        logger.debug(f"MySQL Query: {sql_query}")
        
        try:
            df = self.data_fetcher.fetch_expertise_data(
                sql_query=sql_query,
                bindings=[expertise_id],
                raw_sql=True  #
            )
            
            logger.info(f" Получено {len(df)} записей из MySQL")
            
            if len(df) < 2:
                error_msg = (
                    f"Недостаточно данных для expertise_id={expertise_id}. "
                    f"Найдено записей: {len(df)}. Требуется ровно 2 записи."
                )
                logger.error(error_msg)
                if not df.empty:
                    logger.error(f"Найденные expert_id: {df['expert_id'].tolist()}")
                raise Exception(error_msg)
            
            # Извлекаем JSON из полей 'json_data'
            json_1 = df.iloc[0]['json_data']
            json_2 = df.iloc[1]['json_data']
            expert_id_1 = df.iloc[0]['expert_id']
            expert_id_2 = df.iloc[1]['expert_id']
            
            logger.info(f"✅ Найдены данные для expert_id: {expert_id_1} и {expert_id_2}")
            logger.debug(f"json_1 sample: {str(json_1)[:200]}...")
            logger.debug(f"json_2 sample: {str(json_2)[:200]}...")
            
            # Конвертируем в OrderedDict 
            json_1_ordered = self._convert_to_ordered_dict(json_1)
            json_2_ordered = self._convert_to_ordered_dict(json_2)
            
            return json_1_ordered, json_2_ordered
            
        except Exception as e:
            logger.error(f" Ошибка получения данных: {str(e)}", exc_info=True)
            raise Exception(f"Ошибка получения данных для expertise_id={expertise_id}: {str(e)}")
    
    def _convert_to_ordered_dict(self, data: Any) -> OrderedDict:
        """
        Рекурсивная конвертация данных в OrderedDict
        
        ИСПРАВЛЕНО: правильная сигнатура метода с параметром 'data'
        """
        # Если данные - строка, пытаемся распарсить JSON
        if isinstance(data, str):
            try:
                
                data = data.strip()
                if data.startswith('"') and data.endswith('"'):
                    data = data[1:-1]
                
                parsed_data = json.loads(data)
                logger.debug(" Успешно распарсен JSON из строки")
                return self._convert_to_ordered_dict(parsed_data)
            except json.JSONDecodeError as e:
                logger.warning(f"⚠️ Невозможно распарсить JSON: {e}")
                # Возвращаем как есть, но оборачиваем в OrderedDict для совместимости
                return data
        
        # Если данные - словарь
        if isinstance(data, dict):
            return OrderedDict(
                (str(k), self._convert_to_ordered_dict(v)) 
                for k, v in data.items()
            )
        
        # Если данные - список
        if isinstance(data, list):
            return [self._convert_to_ordered_dict(item) for item in data]
        
        # Для всех остальных типов возвращаем как есть
        return data
    
    def merge_expertise_jsons(self, expertise_id: str) -> dict:
        """
        Сливает два JSON-документа экспертизы
        """
        try:
            logger.info(f"🚀 Начало слияния для expertise_id={expertise_id}")
            json_1, json_2 = self.get_expertise_jsons(expertise_id)
            merged_result = self.json_merger.merge_jsons(json_1, json_2)
            logger.info(f"✅ Слияние успешно завершено для expertise_id={expertise_id}")
            return merged_result
        except Exception as e:
            logger.error(f" Ошибка слияния: {str(e)}", exc_info=True)
            raise
