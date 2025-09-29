import pandas as pd
from sklearn.metrics.pairwise import cosine_similarity

from configs.config import Config
from search_experts.data_fetcher import DataFetcher
from search_experts.text_processor import TextProcessor
from search_experts.embedding_client import EmbeddingClient
from search_experts.conflict_detector import ConflictDetector


class ScoringPipeline:
    """
    Конвейер оценки экспертов на основе текстового сходства, нагрузки и конфликтов интересов.

    Основной класс системы ранжирования, который объединяет загрузку данных, предобработку,
    вычисление эмбеддингов, обнаружение конфликтов и расчёт итогового рейтинга.
    Предназначен для автоматического подбора наиболее подходящих экспертов под задачу экспертизы.
    """
    def __init__(self):
        """
        Инициализирует пайплайн, загружая конфигурацию и создавая экземпляры компонентов.
        """
        # Загрузка конфигурации
        self.config = Config()
        
        # Инициализация компонентов с конфигурацией
        db_config = self.config.get_database_config()
        embedding_config = self.config.get_embedding_config()
        paths_config = self.config.get_paths_config()
        scoring_config = self.config.get_scoring_config()
        
        self.data_fetcher = DataFetcher(db_config.url, db_config.headers)
        self.text_processor = TextProcessor()
        self.embedding_client = EmbeddingClient(
            embedding_config.api_url,
            embedding_config.api_key,
            batch_size=embedding_config.batch_size
        )
        self.conflict_detector = ConflictDetector()
        
        # self.similarity_threshold = scoring_config['similarity_threshold']
        self.sql_queries_path = paths_config.sql_queries
    

    def load_sql_query(self, file_name: str) -> str:
        """
        Загружает SQL-запрос из файла и нормализует его (удаляет лишние пробелы).

        Args:
            file_name (str): Имя файла с SQL-запросом (должен находиться в директории sql_queries).

        Returns:
            str: Содержимое файла в виде одной строки без лишних переносов и пробелов.
        """
        file_path = f"{self.sql_queries_path}{file_name}"
        with open(file_path, encoding="utf-8") as f:
            sql_query = f.read()
        return " ".join(sql_query.split())


    def preprocess_data(self, df):
        """
        Очищает текстовые поля в DataFrame от мусора и приводит к единому формату.

        Применяет очистку текста (удаление спецсимволов, нормализацию пробелов) к ключевым полям,
        используемым для сравнения: описанию экспертизы и профилю эксперта.

        Args:
            df (pd.DataFrame): Входной DataFrame с сырыми текстовыми данными.

        Returns:
            pd.DataFrame: DataFrame с очищенными текстовыми полями.
        """
        df["expertise_text_feature"] = df["expertise_text_feature"].apply(self.text_processor.clean_text)
        df["expert_text_feature"] = df["expert_text_feature"].apply(self.text_processor.clean_text)
        return df


    def calculate_similarities(self, df):
        """
        Вычисляет семантическую близость между описанием экспертизы и профилями экспертов.

        Использует модель эмбеддингов для преобразования текстов в векторы и рассчитывает
        косинусное сходство между запросом экспертизы и каждым экспертом.

        Args:
            df (pd.DataFrame): DataFrame, содержащий столбцы 'expertise_text_feature' и 'expert_text_feature'.

        Returns:
            pd.DataFrame: Исходный DataFrame с добавленным столбцом 'similarity_embeddings'
                          и отсортированный по убыванию схожести.
        """
        exp_text = df["expertise_text_feature"].iloc[0]
        expert_texts = df["expert_text_feature"].tolist()
        
        exp_emb = self.embedding_client.get_embeddings([exp_text])
        expert_embs = self.embedding_client.get_embeddings(expert_texts)
        
        df["similarity_embeddings"] = cosine_similarity(exp_emb, expert_embs).flatten()
        return df.sort_values(by="similarity_embeddings", ascending=False)


    def detect_conflicts(self, df):
        """
        Фильтрует экспертов, потенциально имеющих конфликт интересов.

        Выполняет нечёткое сравнение имён и организаций экспертов с участниками контракта.
        Удаляет строки, где уровень совпадения 81% и выше (высокий риск конфликта).

        Args:
            df (pd.DataFrame): DataFrame с экспертами.

        Returns:
            pd.DataFrame: DataFrame без экспертов, вызывающих подозрение в конфликте интересов.
        """
        df['conflict_fuzzy'] = df.apply(self.conflict_detector.fuzzy_match, axis=1).astype(int)
        return df[df['conflict_fuzzy'] < 81]


    def calculate_ratings(self, df):
        """
        Рассчитывает итоговый рейтинг каждого эксперта на основе нескольких факторов.

        Комбинирует схожесть текстов, удалённость, рабочую нагрузку и средний рейтинг в один показатель.
        Формула: (similar * 4 + distance * 3 + workload * 2 + avg_rating) / 10

        Args:
            df (pd.DataFrame): DataFrame с вычисленными метриками.

        Returns:
            pd.DataFrame: DataFrame с новым столбцом 'rating'.
        """
        df['rating'] = (df['similarity_embeddings'] * 4 + df['distance_rate'] * 3 + 
                        df['workload_rate'] * 2 + df['avg_rating']) / 10
        return df


    def run_pipeline(self, sql_file_path, expertise_id):
        """
        Запускает полный конвейер обработки: от загрузки данных до расчёта рейтингов.

        Args:
            sql_file_path (str): Путь к файлу с SQL-запросом для получения данных.
            expertise_id (Any): Идентификатор конкретной экспертизы.

        Returns:
            pd.DataFrame: Отсортированный DataFrame с экспертами и их рейтингами.
        """
        # Загрузка SQL запроса
        sql_query = self.load_sql_query(sql_file_path)
        
        # Получение данных
        df = self.data_fetcher.fetch_expertise_data(sql_query, expertise_id)
        
        # Предобработка
        df = self.preprocess_data(df)
        
        # Расчет схожестей
        df = self.calculate_similarities(df)
        
        # Обнаружение конфликтов
        df = self.detect_conflicts(df)
        
        # Расчет рейтингов
        df = self.calculate_ratings(df)
        
        return df


    def get_top_results(self, df, rows=10):
        """
        Возвращает топ-N экспертов с наивысшим рейтингом и ключевой информацией.

        Args:
            df (pd.DataFrame): DataFrame с результатами выполнения пайплайна.
            rows (int, optional): Количество возвращаемых строк. По умолчанию — 10.

        Returns:
            pd.DataFrame: Таблица с топ-экспертами и основными метриками.
        """
        result_cols = [
            'expertise_id', 'priceContract', 'expertise_text_feature', 'expert_id', 'expert_text_feature', 
            'distance_rate', 'similarity_embeddings', 'possibleWeekWorkload', 'avg_rating', 'conflict_fuzzy', 'rating'
        ]
        return df[result_cols].sort_values(by='rating', ascending=False).head(rows)
