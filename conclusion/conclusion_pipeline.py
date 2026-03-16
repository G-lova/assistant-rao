import asyncio
import json
import pandas as pd

from typing import Any, Dict, List
from conclusion.ai_conclusion_consolidator import ConclusionConsoladator
from configs.llm_client import get_llm
from configs.logger import get_logger
from configs.config import Config
from conclusion.external_api_service import ExternalAPIService
from configs.working_with_db import get_async_summary_report_from_db
from openai import OpenAI, AsyncOpenAI
from configs.data_fetcher import DataFetcher


logger = get_logger(__name__)



class RaoConclusionPipeline:
    """
    
    
    """

    def __init__(self, expertise_id: int, environment: str = None):
        """
        
        """
        # Загрузка конфигурации
        self.config = Config()
        
        # Инициализация компонентов с конфигурацией
        db_config = self.config.get_database_config(environment)
        paths_config = self.config.get_paths_config()
        llm_client, llm_model = get_llm()
        
        self.data_fetcher = DataFetcher(db_config.url, db_config.headers)
        self.conclusion_consolidator = ConclusionConsoladator(llm_client, llm_model)
        self.external_api_service = ExternalAPIService(environment)
        
        self.sql_queries_path = paths_config.sql_queries

        self.expertise_id = expertise_id

    async def get_expert_opinions(self):
        """Получает все заключения экспертов по expertise_id"""
        try:
            # Загрузка SQL запроса
            sql_query = """
                SELECT ee.*, e.object, e.type AS type_orig, e.checkType2
                FROM `expertise_expert_opinion7s` ee
                LEFT JOIN expertises e
                ON ee.expertise_id = e.id
                LEFT JOIN users u
                ON u.id = ee.expert_id
                WHERE data IS NOT NULL AND data != CAST('[]' AS JSON)
                AND u.role = 'expert'
                AND e.id = ?
            """
            
            # Получение данных
            logger.info(f"Загрузка даннных для expertise_id={self.expertise_id}")
            df = await self.data_fetcher.fetch_async_expertise_data(sql_query, bindings=[self.expertise_id])

            if df.empty or df['id'].isna().all() or (df['id'].astype(str) == 'None').all():
                raise Exception("Нет данных для анализа")
                
            logger.info(f"Получено {len(df)} записей из MySQL")
            
            if len(df) < 2:
                error_msg = (f"Недостаточно данных для expertise_id={self.expertise_id}. Требуется не менее 2 записей.")
                logger.error(error_msg)
                return pd.DataFrame()
            
            return df
        
        except Exception as e:
            logger.error(f"Ошибка получения данных: {str(e)}", exc_info=True)
            raise Exception(f"Ошибка получения данных для expertise_id={self.expertise_id}: {str(e)}")

    @staticmethod
    def normalize_value(val: Any) -> Any:
        """Приводит значение к каноническому виду для сравнения"""
        if isinstance(val, str):
            return val.strip() if val else ""
        return val


    def merge_arrays(self, expertise_object, arrs: List[List], path: str) -> list:
        """Слияние массивов с сохранением порядка"""
        merged = []
        zip_arr = []

        if not arrs:
            return merged

        if len(arrs) == 1:
            zip_arr.extend(arrs[0])
            for v in zip_arr:
                merged.append(self.merge_values(expertise_object, v, path))
            return merged
        
        zip_arr.extend([list(z) for z in zip(*arrs)])
        min_len = len(zip_arr)
        for arr in arrs:
            if len(arr) <= min_len:
                arrs.remove(arr)
            arr = arr[min_len:]
        for v in zip_arr:
            merged.append(self.merge_values(expertise_object, v, path))
        return merged

    def merge_values(self, expertise_object: int, values: List[Any], path: str = "") -> Any:
        """
        Объединяет значения из нескольких экспертов по правилам:
        - Если все одинаковые → вернуть это значение.
        - Если различаются:
            - для чисел/булевых → оставить "проблемное" (True, !=0, непустое)
            - для текста → объединить через "\n или\n"
        """
        if not values:
            return None

        # Убираем None и пустые строки, нормализуем
        non_empty = [self.normalize_value(v) for v in values if self.normalize_value(v) is not None and v != ""]
        if not non_empty:
            return None
        
        # Проверяем уникальность значений
        unique_vals = []
        for v in non_empty:
            if (v.lower() if isinstance(v, str) else v) not in unique_vals:
                unique_vals.append(v)


        # Все совпадают
        if len(unique_vals) == 1:
            return unique_vals[0]

        # Разные значения: 
        # Для булевых/чисел: если есть хотя бы один "проблемный" — оставляем его
        # Пример: q1 = true/false → если хоть один true → оставляем true
        if isinstance(unique_vals[0], bool):
            if expertise_object == 7 and True in unique_vals:
                return True
            if expertise_object == 6 and False in unique_vals:
                return False
            
        if isinstance(unique_vals[0], (int, float)):
            if expertise_object == 7 and 1 in unique_vals:
                return 1
            if expertise_object == 6 and 0 in unique_vals:
                return 0
            return max(unique_vals)
        
        # Текстовые поля → объединяем
        if isinstance(unique_vals[0], str):
            return "\n или \n".join(str(v) for v in unique_vals if v)
        
        if isinstance(unique_vals[0], list):
            return self.merge_arrays(expertise_object, unique_vals, path)
        
        if isinstance(unique_vals[0], dict):
            return self.deep_merge_dicts(expertise_object, unique_vals, path)

        # По умолчанию — возвращаем первое непустое
        return unique_vals[0]

    def deep_merge_dicts(self, expertise_object: int, dict_list: List[Dict], path: str = "") -> Dict:
        """Рекурсивно объединяет список словарей"""
        if not dict_list:
            return {}

        all_keys = set()
        for d in dict_list:
            if isinstance(d, dict):
                all_keys.update(d.keys())

        merged = {}
        for key in all_keys:
            values = []
            for d in dict_list:
                if isinstance(d, dict) and key in d:
                    val = d[key]
                    if key in ['field1_1', 'field1_2']:
                        try:
                            # Преобразуем только если это строка числа
                            if isinstance(val, str) and val.replace('.', '').isdigit():
                                num = float(val)
                                val = int(num) if num.is_integer() else num
                        except ValueError:
                            pass
                    values.append(val)

            current_path = f"{path}.{key}" if path else key

            merged[key] = self.merge_values(expertise_object, values, current_path)

        return merged    

    async def run_pipeline(self):
        """
        
        """
        try:            
            # Получение данных о заключениях экспертов
            df = await self.get_expert_opinions()
            if df.empty:
                logger.info(f"Нет заключений экспертов для expertise_id={self.expertise_id}")
                return {}
            
            df['data'] = df['data'].map(json.loads)
            opinions = df['data'].to_list()
            expertise_object = df['object'].iloc[0]
            expertise_type = df['checkType2'].iloc[0]
          
            if not opinions:
                logger.info(f"Нет заключений экспертов для expertise_id={self.expertise_id}")
                return {}
            
            # Получение данных о загруженных документах
            summary_report = await get_async_summary_report_from_db(self.expertise_id)
            if isinstance(summary_report, str):
                try:
                    summary_report = json.loads(summary_report)
                except Exception:
                    pass

            if not summary_report:
                summary_report = {}
            
            content =  {
                "summary_report": summary_report,
                "opinions": opinions
            }


            # Получение полей, основанных на данных контракта
            ai_conclusion = await self.conclusion_consolidator.get_rao_conclusion(json.dumps(content, ensure_ascii=False), self.expertise_id, expertise_object)

            # Генерация итогового заключения
            rao_conclusion = self.deep_merge_dicts(expertise_object, opinions)
            logger.info(f"Слияние успешно завершено для expertise_id={self.expertise_id}")

            logger.info(f"Попытка использовать значения ИИ для expertise_id={self.expertise_id}")
            if ai_conclusion and ai_conclusion.get('status', {}) == 'success' and ai_conclusion.get('conclusion', {}):
                change = 0
                for key, value in ai_conclusion.get('conclusion', {}).items():
                    if key in rao_conclusion and value:
                        rao_conclusion[key] = value
                        change +=1
                logger.info(f"Внесено {change} изменений с помощью ИИ")
            else:
                logger.info(f"Не удалось внести изменения с помощью ИИ: {ai_conclusion.get('error', {})}")

            logger.info(f'Добавление недостающих полей')
            field = 'field4_4_1' if expertise_type == 14 else 'field4_0_1'
            rao_conclusion[field] = True
            if expertise_object == 7 and 'field1_3' in rao_conclusion:
                for item in rao_conclusion['field1_3']:
                    item[field] = {
                        'q1': True,
                        'q2': '',
                        'q3': '',
                        'q4': '',
                        'q5': '',
                        'q6': False,
                        'q7': None
                    }
            
            return rao_conclusion
        
        except Exception as e:
            logger.error(f"Ошибка получения данных: {str(e)}", exc_info=True)
            raise Exception(f"Ошибка получения данных для expertise_id={self.expertise_id}: {str(e)}")

    async def send_rao_conclusion(self, rao_conclusion, send_to_external = False):
        """
        
        """
        if send_to_external:
            await self.external_api_service.send_expertise_data(self.expertise_id, rao_conclusion)
