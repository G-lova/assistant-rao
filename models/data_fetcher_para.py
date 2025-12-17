import json
import requests
import pandas as pd
import logging
from typing import Any, Dict, List, Optional
import pandas as pd

logger = logging.getLogger(__name__)

class DataFetcher:
    """
    Утилита для получения данных из внешнего API с поддержкой разных СУБД
    """
    
    def __init__(self, url: str, headers: Dict[str, str]):
        self.url = url
        self.headers = headers
        logger.info(f"Intialized DataFetcher with URL: {url}")

    def fetch_expertise_data(
        self, 
        sql_query: str, 
        bindings: Optional[List[Any]] = None,
        raw_sql: bool = False
    ) -> pd.DataFrame:
        """
        Извлекает данные экспертизы с поддержкой разных СУБД
        
        Args:
            sql_query: SQL-запрос
            bindings: Параметры для подстановки
            raw_sql: Если True - не конвертируем ? в $1 (для MySQL)
        """
        # КОНВЕРТАЦИЯ ТОЛЬКО ДЛЯ POSTGRESQL
        if not raw_sql and "?" in sql_query and "$1" not in sql_query:
            for i in range(len(bindings or []), 0, -1):
                sql_query = sql_query.replace("?", f"${i}", 1)
            logger.debug(f"Конвертирован SQL-запрос для PostgreSQL: {sql_query}")
        
        data = {
            "sql": sql_query,
            "bindings": bindings or []
        }
        
        logger.debug(f"Отправка запроса к API: {data}")
        
        try:
            response = requests.post(
                self.url, 
                headers=self.headers, 
                data=json.dumps(data),
                timeout=30
            )
            
            logger.debug(f"Статус ответа: {response.status_code}")
            
            if response.status_code != 200:
                # Пытаемся распарсить ошибку
                try:
                    error_data = response.json()
                    error_detail = error_data.get('error', response.text)
                except:
                    error_detail = response.text
                
                raise Exception(f"Ошибка API ({response.status_code}): {error_detail}")
            
            result = response.json()
            
            if 'data' not in result:
                raise ValueError(f"Ответ не содержит поле 'data'. Ответ: {result}")
            
            if not isinstance(result['data'], list):
                raise ValueError(
                    f"Поле 'data' должно быть списком. "
                    f"Получен тип: {type(result['data']).__name__}, значение: {result['data']}"
                )
            
            df = pd.DataFrame(result['data'])
            logger.info(f" Успешно получено {len(df)} записей")
            return df
            
        except Exception as e:
            logger.error(f" Ошибка в fetch_expertise_data: {str(e)}", exc_info=True)
            raise
