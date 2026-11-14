import json
import asyncio
import time
from typing import List

import aiofiles
import pandas as pd
from sklearn.metrics.pairwise import cosine_similarity

from configs.http_client_manager import HTTPClientManager
from configs.logger import get_logger
from configs.config import Config
from configs.data_fetcher import DataFetcher
from configs.utils import load_sql_query_async
from search_experts.text_processor import TextProcessor
from configs.embedding_client import EmbeddingClient
from search_experts.conflict_detector import ConflictDetector


logger = get_logger(__name__)


def brief(ids, limit: int = 15) -> str:
    """Короткая запись множества ID для журнала: «29: [43, 113, …] (+14)».

    Args:
        ids: ID (множество, список, ключи словаря).
        limit: Сколько ID показать.

    Returns:
        str: Число и первые ``limit`` ID по возрастанию.
    """
    items = sorted(int(i) for i in ids)
    head = ", ".join(map(str, items[:limit]))
    more = f" (+{len(items) - limit})" if len(items) > limit else ""
    return f"{len(items)}: [{head}]{more}"



def constraint_ids(value) -> List[int]:
    """id экспертов из ``constraint_experts`` (пары экспертов, которых нельзя ставить вместе).

    Значение приходит из портала по-разному: JSON-строка ``"[1, 2]"``, список, массив numpy, ``None`` / NaN.
    Раньше проверка ``pd.isna(value)`` для списка из двух и более id возвращала массив, и ``not`` от него падал
    с ``ValueError: The truth value of an array … is ambiguous`` — подбор экспертов прерывался.

    Args:
        value: Значение поля ``constraint_experts``.

    Returns:
        List[int]: id экспертов; нечисловые элементы и непонятные значения пропускаются.
    """
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else []
        except ValueError:
            return []
    elif hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = [] if value != value else [value]      # NaN — пусто, одно число — один id
    if not isinstance(value, (list, tuple, set)):
        return []
    out = []
    for v in value:
        try:
            if v is not None and v == v:               # не None и не NaN
                out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out

class ScoringPipeline:
    """
    Конвейер для оценки и ранжирования экспертов на основе схожести текстов и дополнительных метрик.

    Выполняет полный цикл обработки: загрузку данных из БД по заданному SQL-запросу,
    предобработку текстов, вычисление эмбеддингов и косинусного сходства, фильтрацию по конфликтам интересов
    и расчёт итогового рейтинга экспертов. Используется для подбора наиболее подходящих экспертов
    к конкретной экспертизе.
    """

    def __init__(self, http_manager: HTTPClientManager, environment: str = None):
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
        
        self.data_fetcher = DataFetcher(db_config.url, db_config.headers, http_manager)
        self.text_processor = TextProcessor()
        self.embedding_client = EmbeddingClient(
            embedding_config.api_url,
            embedding_config.api_key,
            embedding_config.model,
            batch_size=embedding_config.batch_size,
            http_manager=http_manager
        )
        self.conflict_detector = ConflictDetector()
        self.environment = environment
        self.embedding_model = embedding_config.model

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
    

    async def attach_org_guesses(self, df):
        """
        Восстанавливает ИНН места работы и диплома у кандидатов без реквизитов по справочнику организаций.

        Добавляет столбцы ``expert_organization_inn_guess`` / ``expert_diplom_inn_guess`` и пояснения
        ``expert_*_guess`` (``search_experts.org_directory``). Ошибка справочника подбор не останавливает:
        такие эксперты проверяются по названиям.

        Args:
            df (pandas.DataFrame): Кандидаты.

        Returns:
            pandas.DataFrame: Датафрейм с восстановленными ИНН (или исходный).
        """
        from configs import settings
        from search_experts import org_directory

        if not settings.CONFLICT_ORG_DIRECTORY_ENABLED or not self._has_valid_experts(df):
            return df
        try:
            from knowledge_store.scope import normalize_env
            env = normalize_env(self.environment or settings.ENVIRONMENT)
            directory = await org_directory.get_directory(env, self.data_fetcher)
            return await asyncio.to_thread(org_directory.resolve_frame, df, directory,
                                           settings.CONFLICT_ORG_DIRECTORY_THRESHOLD, settings.CONFLICT_ORG_DIRECTORY_MARGIN,
                                           settings.CONFLICT_ORG_DIRECTORY_BRANCH_THRESHOLD)
        except Exception as e:  # noqa: BLE001 — без справочника работает сравнение названий
            logger.warning(f"{getattr(self, 'log_ctx', '[подбор]')} Справочник организаций не применён: {type(e).__name__}: {e}")
            return df

    async def load_coauthors(self, df):
        """
        Загружает авторов публикаций кандидатов для проверки соавторства (``search_experts.coauthors``).

        Результат — ``self.coauthor_publications`` (``expert_id`` → публикации с авторами); при выключенной проверке
        или ошибке внутренней БД — пустой словарь (соавторство не проверяется, подбор продолжается).

        Args:
            df (pandas.DataFrame): Кандидаты.
        """
        from configs import settings
        from search_experts import coauthors

        self.coauthor_publications = {}
        if not settings.CONFLICT_COAUTHORS_ENABLED or not self._has_valid_experts(df):
            return
        try:
            from knowledge_store.scope import normalize_env
            env = normalize_env(self.environment or settings.ENVIRONMENT)
            self.coauthor_publications = await coauthors.load_coauthors(
                env, df["expert_id"].dropna().astype(int).unique().tolist(), coauthors.min_year(settings.CONFLICT_COAUTHOR_YEARS))
        except Exception as e:  # noqa: BLE001 — без публикаций соавторство не проверяется
            logger.warning(f"{getattr(self, 'log_ctx', '[подбор]')} Соавторы не загружены: {type(e).__name__}: {e}")

    def detect_conflicts(self, df):
        """
        Фильтрует экспертов, имеющих конфликт интересов с текущей экспертизой.

        Соавторство с контактным лицом или руководителем заказчика (``search_experts.coauthors``) — тоже конфликт.
        Организации эксперта (место работы и организация, выдавшая диплом) сравниваются с заказчиком по ИНН/КПП,
        если реквизиты есть у эксперта и заказчика, а без реквизитов — прежним методом по названиям (fuzzy matching
        ≥ 85 % и сравнение без учёта ОПФ). Исполнитель контракта и ФИО сравниваются по названиям
        (``ConflictDetector.organization_conflict_reasons``). Отдельно — возможные семейные связи по фамилиям.

        Args:
            df (pandas.DataFrame): Датафрейм с текстами и сходством.

        Returns:
            pandas.DataFrame: Отфильтрованный датафрейм, содержащий только экспертов без конфликта интересов.
        """
        if not self._has_valid_experts(df):
            return df

        from collections import Counter
        from search_experts import coauthors

        ctx = getattr(self, "log_ctx", "[подбор]")
        reasons = {eid: list(r) for eid, r in self.conflict_detector.find_requisite_conflicts(df).items()}
        if reasons:
            logger.debug(f"{ctx} конфликт с организациями экспертизы: {reasons}")
            for col in ("expert_organization_guess", "expert_diplom_guess"):
                if col in df.columns:
                    notes = df.loc[df["expert_id"].isin(reasons) & df[col].notna(), ["expert_id", col]]
                    for eid, note in notes.drop_duplicates().itertuples(index=False):
                        logger.debug(f"{ctx} эксперт {eid}: ИНН по справочнику ({col}): {note}")

        for eid in self.conflict_detector.find_family_conflicts(df) or ():
            reasons.setdefault(eid, []).append("family")

        # соавтор — контактное лицо или руководитель заказчика
        co = coauthors.customer_coauthor_conflicts(df, getattr(self, "coauthor_publications", {}) or {})
        for eid, note in co.items():
            reasons.setdefault(eid, []).append("coauthor_customer")
            logger.debug(f"{ctx} эксперт {eid}: соавторство с заказчиком: {note}")

        if reasons:
            counts = Counter(r for rs in reasons.values() for r in set(rs))
            why = ", ".join(f"{k} {v}" for k, v in counts.most_common())
            logger.info(f"{ctx} конфликт интересов: исключено {brief(reasons)}; причины: {why}")
            if hasattr(self, "funnel"):
                self.funnel["конфликт"], self.funnel["конфликт_причины"] = len(reasons), why
            df = df[~df["expert_id"].isin(reasons)]

        return df
    

    async def calculate_similarities(self, df):
        """
        Вычисляет косинусное сходство между эмбеддингами текста экспертизы и описаний экспертов.

        Векторные представления текстов берутся из кэша внутренней БД (``search_experts.embedding_cache``,
        миграция 013), недостающие считаются внешним embedding API; рассчитывает попарное сходство и добавляет
        результат в новый столбец датафрейма.
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

        # один вызов на экспертизу и кандидатов: векторы из кэша внутренней БД, в сервис — только новые тексты
        from search_experts import embedding_cache
        embs = await embedding_cache.get_embeddings(self.embedding_client, [exp_text] + expert_texts, self.embedding_model)
        exp_emb, expert_embs = embs[:1], embs[1:]
        
        df["similarity_embeddings"] = cosine_similarity(exp_emb, expert_embs).flatten()
        df = await self.apply_publication_similarity(df, exp_emb[0])
        return df.sort_values(by="similarity_embeddings", ascending=False)

    async def apply_publication_similarity(self, df, expertise_embedding):
        """
        Добавляет к сходству анкеты сходство публикаций и монографий эксперта (внутренняя БД, миграция 011).

        Итог — ``(1 − w) × сходство анкеты + w × сходство публикаций`` (``settings.EXPERT_PUB_SIMILARITY_WEIGHT``);
        эксперт без разобранных публикаций сохраняет сходство анкеты. Ошибка внутренней БД подбор не останавливает:
        сходство остаётся прежним (предупреждение в журнале).

        Args:
            df (pandas.DataFrame): Кандидаты со столбцом 'similarity_embeddings'.
            expertise_embedding: Эмбеддинг текста экспертизы.

        Returns:
            pandas.DataFrame: Датафрейм со столбцами 'similarity_profile', 'similarity_publications',
            'publications_count', 'publication_top' и обновлённым 'similarity_embeddings'.
        """
        from configs import settings
        from search_experts.publications import similarity

        if not settings.EXPERT_PUB_SIMILARITY_ENABLED:
            return df
        try:
            from knowledge_store.scope import normalize_env
            env = normalize_env(self.environment or settings.ENVIRONMENT)
            scores = await similarity.publication_scores(
                env, df["expert_id"].dropna().astype(int).unique().tolist(), list(expertise_embedding), self.embedding_model)
        except Exception as e:  # noqa: BLE001 — подбор работает и без публикаций
            logger.warning(f"{getattr(self, 'log_ctx', '[подбор]')} Сходство публикаций не учтено: {type(e).__name__}: {e}")
            return df
        if scores:
            logger.info(f"{getattr(self, 'log_ctx', '[подбор]')} публикации учтены у {len(scores)} из "
                        f"{df['expert_id'].nunique()} кандидатов")
        return similarity.blend(df, scores, settings.EXPERT_PUB_SIMILARITY_WEIGHT)
    

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

        Так же разрешаются пары кандидатов-соавторов (``search_experts.coauthors.expert_coauthor_pairs``):
        остаётся эксперт с большим сходством.

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
        ctx = getattr(self, "log_ctx", "[подбор]")

        exclude_ids = self.conflict_detector.find_nepotism(df)
        if exclude_ids:
            logger.info(f"{ctx} семейственность (совпадение фамилий): исключено {brief(exclude_ids)}")
            experts_to_exclude |= exclude_ids
            if hasattr(self, "funnel"):
                self.funnel["семейственность"] = len(exclude_ids)

        # кандидаты-соавторы: остаётся более подходящий (coauthor_expert)
        from search_experts import coauthors
        pairs = coauthors.expert_coauthor_pairs(df, getattr(self, "coauthor_publications", {}) or {})
        if pairs:
            excluded = coauthors.exclude_coauthor_pairs(df, pairs) - experts_to_exclude
            logger.info(f"{ctx} соавторство кандидатов: пар {len(pairs)}, исключено {brief(excluded)}")
            for a, b, title in pairs:
                logger.debug(f"{ctx} соавторы {a} и {b}: {title}")
            experts_to_exclude |= excluded
            if hasattr(self, "funnel") and excluded:
                self.funnel["соавторы"] = len(excluded)

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
        timings = {}
        self.timings, self.funnel = timings, {}
        self.log_ctx = f"[подбор {expertise_id}/{self.environment}]"
        started = time.perf_counter()

        def mark(step):
            """Запоминает длительность шага (с) с предыдущей отметки."""
            nonlocal started
            now = time.perf_counter()
            timings[step] = round(now - started, 2)
            started = now

        try:
            # Загрузка SQL запроса
            sql_query = await load_sql_query_async(sql_file_path)
            
            # Получение данных
            df = await self.data_fetcher.fetch_async_expertise_data(sql_query, bindings=[expertise_id] * 4)
            mark("sql")
            self.funnel["кандидатов"] = int(df["expert_id"].nunique()) if "expert_id" in df.columns else 0
        
            if df.empty or df['expert_id'].isna().all() or (df['expert_id'].astype(str) == 'None').all():
                raise Exception("Доступных экспертов нет")
            
            # Предобработка
            df = await asyncio.to_thread(self.preprocess_data, df)
            mark("preprocess")
            
            # ИНН организаций экспертов без реквизитов — из справочника организаций
            df = await self.attach_org_guesses(df)
            mark("org_directory")

            # Соавторы кандидатов (публикации во внутренней БД)
            await self.load_coauthors(df)
            mark("coauthors_load")

            # Обнаружение конфликтов
            df = await asyncio.to_thread(self.detect_conflicts, df)
            mark("conflicts")
            
            # Расчет схожестей
            df = await self.calculate_similarities(df)
            mark("similarity")
            
            # Расчет семейственности
            df = await asyncio.to_thread(self.detect_nepotism, df)
            mark("nepotism")
            
            # Расчет рейтингов
            df = await asyncio.to_thread(self.calculate_ratings, df)
            mark("ratings")
            
            return df
        
        except Exception as e:
            if str(e) == "Доступных экспертов нет":
                logger.info(f"{self.log_ctx} доступных экспертов нет (SQL {timings.get('sql')} с)")
                return pd.DataFrame(columns=['expert_id', 'scoring'])
            logger.error(f"{self.log_ctx} ошибка на шаге после {list(timings) or ['старт']}: {type(e).__name__}: {e}")
            raise

    def summary(self) -> str:
        """
        Итог подбора одной строкой: время шагов и отсев кандидатов по этапам.

        Пример: «26.2 с (sql 7.97, similarity 17.15, …) | кандидатов 369 → конфликт −12 (inn_work 5, family 4) →
        семейственность −29 → соавторы −1 → ограничения −1 → в ответе 326».

        Returns:
            str: Строка для журнала.
        """
        t = getattr(self, "timings", {}) or {}
        f = getattr(self, "funnel", {}) or {}
        steps = ", ".join(f"{k} {v}" for k, v in t.items() if v >= 0.01)
        parts = [f"кандидатов {f.get('кандидатов', '?')}"]
        for key in ("конфликт", "семейственность", "соавторы", "ограничения"):
            if f.get(key):
                why = f.get(f"{key}_причины")
                parts.append(f"{key} −{f[key]}" + (f" ({why})" if why else ""))
        if "в ответе" in f:
            parts.append(f"в ответе {f['в ответе']}")
        return f"{round(sum(t.values()), 2)} с ({steps}) | " + " → ".join(parts)

    def sort_experts(self, df, sort_column: str = "scoring"):
        
        if not self._has_valid_experts(df):
            return []          
        
        # Сортировка по убыванию рейтинга, региональной экспертизе, а также фильтрация и сортировка по загрузке
        df = df.sort_values(by=[sort_column], ascending=False)
        df = pd.concat([df[df['regionExpertise_sort'] == 1], df[df['regionExpertise_sort'] != 1]])
        df = pd.concat([df[df['possibleWeekWorkload'] >= 1], 
                        df[df['possibleWeekWorkload'] < 1].sort_values(by=['currentWeekWorkloadRequests'], ascending=True)]) 
        return df

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
        
        # Сортировка по убыванию рейтинга, региональной экспертизе, а также фильтрация и сортировка по загрузке
        df = self.sort_experts(df, "scoring")

        # Ограничение новичков для приглашения
        max_newbies = int(df['max_newbies_per_invite'].iloc[0])
        max_experts_per_invite = []
        newbies_count = 0

        for row in df.itertuples():
            if row.is_newbie == 1:
                if newbies_count >= max_newbies:
                    continue
                newbies_count += 1

            max_experts_per_invite.append(row.expert_id)

            if len(max_experts_per_invite) == int(df['max_experts_per_invite'].iloc[0]):
                break

        # сортировка остальных по семантике
        df_rest = df[~df['expert_id'].isin(max_experts_per_invite)]

        df = pd.concat([df[df['expert_id'].isin(max_experts_per_invite)], df_rest])
    
        # Фильтрация экспертов с учётом constraint_experts
        # Идём по списку сверху вниз: если у эксперта есть constraint_experts,
        # то эти эксперты удаляются из итогового списка
        excluded_experts = set()
        filtered_indices = []
        
        for idx, row in df.iterrows():
            expert_id = row['expert_id']
            
            # Пропускаем эксперта, если он уже был исключён
            if expert_id in excluded_experts:
                continue
            
            filtered_indices.append(idx)
            
            # Получаем constraint_experts и добавляем их в список исключённых
            # Пары экспертов, которых нельзя ставить вместе (JSON-строка, список или массив) — в исключённые
            excluded_experts.update(constraint_ids(row.get('constraint_experts', None)))

        # Применяем фильтрацию к датафрейму
        before = df["expert_id"].nunique()
        df = df.loc[filtered_indices]
        dropped = before - df["expert_id"].nunique()
        if excluded_experts:
            logger.info(f"{getattr(self, 'log_ctx', '[подбор]')} ограничения пар экспертов (constraint_experts): "
                        f"исключено из ответа {dropped}, в ограничениях {brief(excluded_experts)}")
        if hasattr(self, "funnel"):
            if dropped:
                self.funnel["ограничения"] = int(dropped)
            self.funnel["в ответе"] = int(df["expert_id"].nunique())
        
        # Формирование списка экспертов для вывода
        if details:
            results = []
            
            for row in df.itertuples():
                results.append({
                    int(row.expert_id): {
                        "total_rate": f"{round(row.scoring * 100, 2)}",
                        "predict_rate": None, # добавить предсказание модели, когда будет реализовано
                        "semantic_rate": f"{round(row.similarity_embeddings * 100, 2)}",
                        # сходство публикаций и самая близкая публикация (None — публикаций нет или учёт выключен)
                        "publication_rate": None if pd.isna(getattr(row, "similarity_publications", float("nan")))
                        else f"{round(row.similarity_publications * 100, 2)}",
                        "publication_top": getattr(row, "publication_top", None),
                        "distance_rate": f"{round(row.distance_rate * 100, 2)}",
                        "criterion_rate": None if pd.isna(row.criterion_rating) else f"{round(row.criterion_rating * 100, 2)}",
                        "div_rate": f"{round(row.avg_rating * 100, 2)}",
                        "is_newbie": bool(row.is_newbie)
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

    def __init__(self, http_manager: HTTPClientManager, environment: str = None):
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
        
        self.data_fetcher = DataFetcher(db_config.url, db_config.headers, http_manager)        
        self.sql_queries_path = paths_config.sql_queries

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


    async def get_experts_rating(self, sql_file_path: str, start_date, end_date):
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
        sql_query = await load_sql_query_async(sql_file_path)
        
        # Получение данных с рассчитанными рейтингами
        df = await self.data_fetcher.fetch_async_expertise_data(sql_query, bindings=[end_date, start_date] + [start_date, end_date] * 5)
        
        # Преобразование данных в словарь
        ratings = await asyncio.to_thread(self.to_rating_dict, df)
        
        return ratings