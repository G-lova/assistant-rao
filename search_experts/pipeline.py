import asyncio

import aiofiles
import pandas as pd
from sklearn.metrics.pairwise import cosine_similarity

from configs.config import Config
from configs.data_fetcher import DataFetcher
from search_experts.text_processor import TextProcessor
from search_experts.embedding_client import EmbeddingClient
from search_experts.conflict_detector import ConflictDetector


class ScoringPipeline:
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
        - DataFetcher — для получения данных из базы,
        - TextProcessor — для очистки текста,
        - EmbeddingClient — для генерации эмбеддингов,
        - ConflictDetector — для выявления конфликтов интересов.
        Также сохраняет путь к директории с SQL-запросами.
        """
        # Загрузка конфигурации
        self.config = Config()
        
        # Инициализация компонентов с конфигурацией
        db_config = self.config.get_database_config(environment)
        embedding_config = self.config.get_embedding_config()
        paths_config = self.config.get_paths_config()
        
        self.data_fetcher = DataFetcher(db_config.url, db_config.headers)
        self.text_processor = TextProcessor()
        self.embedding_client = EmbeddingClient(
            embedding_config.api_url,
            embedding_config.api_key,
            embedding_config.model,
            batch_size=embedding_config.batch_size
        )
        self.conflict_detector = ConflictDetector()
        
        self.sql_queries_path = paths_config.sql_queries
    
    
    def _has_valid_experts(self, df):
        """
        Вспомогательный метод для проверки наличия валидных экспертов.
        
        Args:
            df (pandas.DataFrame): Датафрейм для проверки
            
        Returns:
            bool: True если есть валидные эксперты, False если DataFrame пустой или все expert_id невалидны
        """
        if df.empty:
            return False
            
        if 'expert_id' not in df.columns:
            return False
            
        valid_experts = df['expert_id'].dropna()
        if len(valid_experts) == 0:
            return False
            
        if (valid_experts.astype(str) == 'None').all():
            return False
            
        return True
    

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
    

    def preprocess_data(self, df):
        """
        Очищает текстовые поля в датафрейме от лишних символов и нормализует их.

        Применяет функцию `clean_text` к столбцам с текстом экспертизы и описанием эксперта,
        оставляя только буквы и пробелы, что улучшает качество последующего сравнения.

        Args:
            df (pandas.DataFrame): Датафрейм с колонками 'expertise_text_feature' и 'expert_text_feature'.

        Returns:
            pandas.DataFrame: Датафрейм с очищенными текстовыми полями.
        """
        if not self._has_valid_experts(df):
            return df
        
        for col in ['expert_name', 'expertise_name', 'expertise_organization', 'expert_organization', 'expert_diplom', 'exucutorContract']:
            df[col] = df[col].apply(self.text_processor.normalize_text)
            
        df["expertise_text_feature"] = df["expertise_text_feature"].apply(self.text_processor.clean_text)
        df["expert_text_feature"] = df["expert_text_feature"].apply(self.text_processor.clean_text)
        return df
    

    def detect_conflicts(self, df):
        """
        Фильтрует экспертов, имеющих конфликт интересов с текущей экспертизой.

        Применяет нечёткое сравнение (fuzzy matching) между текстом экспертизы и описанием эксперта,
        а также сравнивает фамилию заказчика экспертизы с фамилиями экспертов в группе.

        Исключает записи, где степень совпадения fuzzy matching превышает порог (85%) или выявлены потенциальные семейные связи,
        что указывает на возможный конфликт.

        Args:
            df (pandas.DataFrame): Датафрейм с текстами и сходством.

        Returns:
            pandas.DataFrame: Отфильтрованный датафрейм, содержащий только экспертов без конфликта интересов.
        """
        if not self._has_valid_experts(df):
            return df
        
        df['conflict_fuzzy'] = df.apply(self.conflict_detector.fuzzy_match, axis=1).astype(int)
        df = df[df['conflict_fuzzy'] < 85]

        experts_to_exclude = set()
        exclude_ids = self.conflict_detector.find_family_conflicts(df)
        if exclude_ids:
            experts_to_exclude |= exclude_ids
        df = df[~df["expert_id"].isin(experts_to_exclude)]

        return df
    

    async def calculate_similarities(self, df):
        """
        Вычисляет косинусное сходство между эмбеддингами текста экспертизы и описаний экспертов.

        Генерирует векторные представления текстов с помощью внешнего embedding API,
        рассчитывает попарное сходство и добавляет результат в новый столбец датафрейма.
        Возвращает датафрейм, отсортированный по убыванию сходства.

        Args:
            df (pandas.DataFrame): Датафрейм с очищенными текстами.

        Returns:
            pandas.DataFrame: Датафрейм с добавленным столбцом 'similarity_embeddings',
            отсортированный по этому столбцу в порядке убывания.
        """
        if not self._has_valid_experts(df):
            df["similarity_embeddings"] = pd.Series([], dtype='float64')
            return df
            
        exp_text = df["expertise_text_feature"].iloc[0]
        expert_texts = df["expert_text_feature"].tolist()

        exp_emb_task = self.embedding_client.get_embeddings([exp_text])
        expert_embs_task = self.embedding_client.get_embeddings(expert_texts)

        exp_emb, expert_embs = await asyncio.gather(
            exp_emb_task,
            expert_embs_task
        )
        
        df["similarity_embeddings"] = cosine_similarity(exp_emb, expert_embs).flatten()
        return df.sort_values(by="similarity_embeddings", ascending=False)
    

    def detect_nepotism(self, df):
        '''
        Обнаруживает и исключает экспертов, подозреваемых в наличии родственных связей,
        на основе анализа схожести фамилий в рамках одной экспертной группы.

        Метод делегирует выявление конфликтующих пар экспертов внутреннему детектору
        (`self.conflict_detector.find_nepotism`), который применяет эвристические правила
        для сравнения фамилий (полное совпадение или различие на один символ с вложением,
        например «Иванов» / «Иванова»). При обнаружении такой пары исключается эксперт
        с более низким значением метрики `similarity_embeddings`, что предполагает
        его меньшую релевантность или соответствие требованиям экспертизы.

        После определения идентификаторов экспертов для исключения, соответствующие строки
        удаляются из переданного датафрейма.

        Args:
            df (pandas.DataFrame): Датафрейм с данными об экспертах, участвующих в одной экспертизе.
                Должен содержать как минимум следующие столбцы:
                    - expert_id: Уникальный идентификатор эксперта.
                    - expert_surname: Фамилия эксперта.
                    - similarity_embeddings: Числовая метрика соответствия эксперта критериям экспертизы.

        Returns:
            pandas.DataFrame: Отфильтрованный датафрейм, из которого удалены эксперты,
                            подозреваемые в непотизме. Структура и состав столбцов сохраняются.
        '''
        if not self._has_valid_experts(df):
            return df
        
        experts_to_exclude = set()

        exclude_ids = self.conflict_detector.find_nepotism(df)
        if exclude_ids:
            experts_to_exclude |= exclude_ids

        df = df[~df["expert_id"].isin(experts_to_exclude)]
        return df
    

    def calculate_ratings(self, df):
        """
        Рассчитывает итоговый рейтинг эксперта на основе нескольких факторов.

        Комбинирует три метрики:
        - сходство текстов,
        - дистанционная оценка,
        - рейтинг эксперта по 5 критериям.
        Результат сохраняется в столбце 'scoring'.

        Args:
            df (pandas.DataFrame): Датафрейм с колонками 'similarity_embeddings', 'distance_rate', 'rating'.

        Returns:
            pandas.DataFrame: Датафрейм с добавленным столбцом 'scoring'.
        """
        if not self._has_valid_experts(df):
            df['scoring'] = pd.Series([], dtype='float64')
            return df
        
        df['scoring'] = 0.3 * df['similarity_embeddings'] + 0.3 * df['distance_rate'] + 0.3 * df['criterion_rating_for_model'] + 0.1 * df['avg_rating']
        
        # Сортировка по убыванию рейтинга, региональной экспертизе, а также фильтрация и сортировка по загрузке
        df = df.sort_values(by=["scoring"], ascending=False)
        df = pd.concat([df[df['regionExpertise_sort'] == 1], df[df['regionExpertise_sort'] != 1]])
        df = pd.concat([df[df['possibleWeekWorkload'] >= 1], 
                        df[df['possibleWeekWorkload'] < 1].sort_values(by=['currentWeekWorkloadRequests'], ascending=True)])
        return df
    

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
            df = await self.data_fetcher.fetch_async_expertise_data(sql_query, bindings=[expertise_id] * 3)
        
            if df.empty or df['expert_id'].isna().all() or (df['expert_id'].astype(str) == 'None').all():
                raise Exception("Доступных экспертов нет")
            
            # Предобработка
            df = await asyncio.to_thread(self.preprocess_data, df)
            
            # Обнаружение конфликтов
            df = await asyncio.to_thread(self.detect_conflicts, df)
            
            # Расчет схожестей
            df = await self.calculate_similarities(df)
            
            # Расчет семейственности
            df = await asyncio.to_thread(self.detect_nepotism, df)
            
            # Расчет рейтингов
            df = await asyncio.to_thread(self.calculate_ratings, df)
            
            return df
        
        except Exception as e:
            if str(e) == "Доступных экспертов нет":
                return pd.DataFrame(columns=['expert_id', 'scoring'])
            else:
                raise

    def get_top_results(self, df, details):
        """
        Извлекает идентификаторы экспертов, отсортированных по убыванию рейтинга.

        Возвращает список ID топовых экспертов для дальнейшего использования (например, в рекомендациях).

        Args:
            df (pandas.DataFrame): Датафрейм с колонками 'expert_id' и 'scoring'.

        Returns:
            list: Серия с идентификаторами экспертов, отсортированная по рейтингу по убыванию.
        """
        if not self._has_valid_experts(df):
            return []        
        
        if details:
            results = []
            
            for row in df.itertuples():
                results.append({
                    int(row.expert_id): {
                        "total_rate": f"{round(row.scoring * 100, 2)}",
                        "predict_rate": None, # добавить предсказание модели, когда будет реализовано
                        "semantic_rate": f"{round(row.similarity_embeddings * 100, 2)}",
                        "distance_rate": f"{round(row.distance_rate * 100, 2)}",
                        "criterion_rate": None if pd.isna(row.criterion_rating) else f"{round(row.criterion_rating * 100, 2)}",
                        "div_rate": f"{round(row.avg_rating * 100, 2)}"
                    }
                })
        else:
            results = df['expert_id'].tolist()

        return results


class RatingPipeline:
    """
    Конвейер для оценки и ранжирования экспертов на основе комплексного расчёта рейтинга по 5 критериям.

    Выполняет полный цикл обработки: загрузку данных из внешнего источника (через HTTP-запрос с SQL),
    расчёт метрик соответствия и формирование итогового рейтинга экспертов. 
    Предназначен для поддержки объективного отбора квалифицированных экспертов на конкретную экспертизу.
    """

    def __init__(self, environment: str = None):
        """
        Инициализирует компоненты конвейера с использованием глобальной конфигурации.

        Загружает настройки из конфигурационного файла и создаёт экземпляры зависимостей:
        - DataFetcher — для выполнения запросов к внешнему API с SQL-запросами,
        - а также определяет путь к директории с SQL-файлами.

        Args:
            environment (str, optional): Наименование окружения (например, 'prod', 'dev'),
                используемое для выбора соответствующих параметров подключения к БД.
                Если не указано, используется значение по умолчанию из конфигурации.
        """
        # Загрузка конфигурации
        self.config = Config()
        
        # Инициализация компонентов с конфигурацией
        db_config = self.config.get_database_config(environment)
        paths_config = self.config.get_paths_config()
        
        self.data_fetcher = DataFetcher(db_config.url, db_config.headers)        
        self.sql_queries_path = paths_config.sql_queries
    

    def load_sql_query(self, file_name: str):
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

    @staticmethod
    def to_rating_dict(data: list):
        """
        Преобразует список словарей с данными экспертов в словарь {expert_id: criterion_rating}.

        Извлекает идентификатор эксперта и его рассчитанный рейтинг по критерию,
        округляя значение рейтинга до двух знаков после запятой.

        Заменяет nan на None для JSON-совместимости.

        Args:
            data (list of dict): Список записей, полученных от внешнего API.
                Каждая запись должна содержать ключи:
                    - 'expert_id': уникальный идентификатор эксперта,
                    - 'criterion_rating': числовое значение рейтинга.

        Returns:
            dict: Словарь вида {expert_id: rating}, где rating — float, округлённый до 2 знаков.
        """    
        import math
        
        if hasattr(data, 'to_dict'):
            ratings = data.set_index('expert_id')['criterion_rating'].round(2).to_dict()
            # Заменяем nan на None
            return {
                k: (None if (isinstance(v, float) and math.isnan(v)) else v)
                for k, v in ratings.items()
            }
        else:
            return {}


    def get_experts_rating(self, sql_file_path: str, start_date, end_date):
        """
        Запускает полный конвейер получения рейтингов экспертов и сохраняет результат в файл.

        Выполняет следующие шаги:
            1. Загружает SQL-запрос из файла.
            2. Отправляет запрос через DataFetcher для получения данных об экспертах.
            3. Преобразует полученные данные в словарь рейтингов.

        Args:
            sql_file_path (str): Имя SQL-файла (без пути), расположенного в директории запросов.

        Returns:
            dict: Словарь с рейтингами экспертов в формате {expert_id: rating}.
        """
        # Загрузка SQL запроса
        sql_query = self.load_sql_query(sql_file_path)
        
        # Получение данных с рассчитанными рейтингами
        df = self.data_fetcher.fetch_expertise_data(sql_query, bindings=[start_date, end_date] * 7)
        
        # Преобразование данных в словарь
        ratings = self.to_rating_dict(df)
        
        return ratings