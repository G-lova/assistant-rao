import pandas as pd
from sklearn.metrics.pairwise import cosine_similarity

from configs.config import Config
from search_experts.data_fetcher import DataFetcher
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

    def __init__(self):
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
        db_config = self.config.get_database_config()
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
        df['conflict_fuzzy'] = df.apply(self.conflict_detector.fuzzy_match, axis=1).astype(int)
        df = df[df['conflict_fuzzy'] < 85]

        experts_to_exclude = set()
        exclude_ids = self.conflict_detector.find_family_conflicts(df)
        if exclude_ids:
            experts_to_exclude |= exclude_ids
        df = df[~df["expert_id"].isin(experts_to_exclude)]

        return df
    

    def calculate_similarities(self, df):
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
        exp_text = df["expertise_text_feature"].iloc[0]
        expert_texts = df["expert_text_feature"].tolist()
        
        exp_emb = self.embedding_client.get_embeddings([exp_text])
        expert_embs = self.embedding_client.get_embeddings(expert_texts)
        
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
        df['scoring'] = 0.3 * df['similarity_embeddings'] + 0.3 * df['distance_rate'] + 0.3 * df['criterion_rating'] + 0.1 * df['avg_rating']
        return df
    

    def run_pipeline(self, sql_file_path, expertise_id, defaultWorkload=5):
        """
        Запускает полный конвейер оценки экспертов для заданной экспертизы.

        Последовательно выполняет все этапы: загрузку данных, предобработку, расчёт сходства,
        фильтрацию по конфликтам и вычисление рейтинга.

        Args:
            sql_file_path (str): Имя файла с SQL-запросом для получения данных об экспертах.
            expertise_id (str или int): Уникальный идентификатор экспертизы.
            defaultWorkload (int): Рабочая нагрузка на эксперта, выставляемая при подборе экспертов на экспертизу (по умолчанию 5).

        Returns:
            pandas.DataFrame: Датафрейм с отфильтрованными и ранжированными экспертами,
            содержащий колонки 'expert_id' и 'rating'.
        """
        # Загрузка SQL запроса
        sql_query = self.load_sql_query(sql_file_path)
        
        # Получение данных
        df = self.data_fetcher.fetch_expertise_data(sql_query, expertise_id)
        
        # Предобработка
        df = self.preprocess_data(df)
        
        # Обнаружение конфликтов
        df = self.detect_conflicts(df)
        
        # Расчет схожестей
        df = self.calculate_similarities(df)
        
        # Расчет семейственности
        df = self.detect_nepotism(df)
        
        # Расчет рейтингов
        df = self.calculate_ratings(df)
        
        return df
    

    def get_top_results(self, df):
        """
        Извлекает идентификаторы экспертов, отсортированных по убыванию рейтинга.

        Возвращает список ID топовых экспертов для дальнейшего использования (например, в рекомендациях).

        Args:
            df (pandas.DataFrame): Датафрейм с колонками 'expert_id' и 'scoring'.

        Returns:
            pandas.Series: Серия с идентификаторами экспертов, отсортированная по рейтингу по убыванию.
        """
        experts = df[['expert_id', 'scoring']].sort_values(by='scoring', ascending=False)
        return experts['expert_id']