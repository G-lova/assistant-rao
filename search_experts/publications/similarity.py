"""Сходство публикаций эксперта с экспертизой для скоринга (``ScoringPipeline.calculate_similarities``).

Для каждого кандидата берутся его учитываемые публикации (источник ``done``, эмбеддинг текущей модели, автор —
эксперт или авторы не указаны) и косинусное сходство их эмбеддингов с эмбеддингом текста экспертизы:

    сходство_публикаций = среднее K лучших (сходство × свежесть),
    свежесть = max(FLOOR, 0.5 ** (возраст_лет / HALF_LIFE)), год неизвестен — FLOOR.

Берутся лучшие K, а не среднее по всем публикациям: эксперт с широким профилем не штрафуется за работы не по теме.
Итоговая similarity эксперта с публикациями — ``(1 − w) × сходство_анкеты + w × сходство_публикаций``; у эксперта
без публикаций — прежнее сходство анкеты.
"""
import datetime
from typing import Any, Callable, Dict, Iterable, Optional, Sequence

from configs import settings
from configs.logger import get_logger

logger = get_logger(__name__)


def recency(year: Optional[int], current_year: int, half_life: float, floor: float) -> float:
    """Множитель свежести публикации.

    Args:
        year: Год публикации (``None`` — неизвестен).
        current_year: Текущий год.
        half_life: За столько лет вклад падает вдвое.
        floor: Минимальный множитель.

    Returns:
        float: Множитель от ``floor`` до 1.
    """
    if not year:
        return floor
    age = max(0, current_year - int(year))
    return max(floor, 0.5 ** (age / half_life)) if half_life > 0 else 1.0


def aggregate(rows: Iterable[Dict[str, Any]], top_k: int, half_life: float, floor: float,
              current_year: Optional[int] = None) -> Dict[int, Dict[str, Any]]:
    """Сходство публикаций по экспертам.

    Args:
        rows: Строки :func:`search_experts.publications.repo.publication_similarities`.
        top_k: Сколько лучших публикаций усреднять.
        half_life: Период полураспада свежести, лет.
        floor: Минимальный множитель свежести.
        current_year: Текущий год (по умолчанию — сегодняшний).

    Returns:
        Dict[int, Dict[str, Any]]: ``expert_id`` → ``{"score", "count", "top_title", "top_year"}``.
    """
    year = current_year or datetime.date.today().year
    by_expert: Dict[int, list] = {}
    for r in rows:
        sim = float(r["similarity"]) * recency(int(r["year"]) if r.get("year") not in (None, "") else None,
                                               year, half_life, floor)
        by_expert.setdefault(int(r["expert_id"]), []).append((sim, r.get("title"), r.get("year")))
    out = {}
    for eid, items in by_expert.items():
        items.sort(key=lambda x: x[0], reverse=True)
        best = items[:max(1, top_k)]
        out[eid] = {"score": sum(x[0] for x in best) / len(best), "count": len(items),
                    "top_title": best[0][1], "top_year": best[0][2]}
    return out


async def publication_scores(env: str, expert_ids: Sequence[int], embedding: Sequence[float], embedding_model: str,
                             connection: Optional[Callable] = None) -> Dict[int, Dict[str, Any]]:
    """Сходство публикаций кандидатов с экспертизой (запрос к внутренней БД).

    Args:
        env: Окружение.
        expert_ids: Кандидаты.
        embedding: Эмбеддинг текста экспертизы.
        embedding_model: Модель эмбеддингов.
        connection: Фабрика соединений (по умолчанию ``knowledge_store.db.connection``).

    Returns:
        Dict[int, Dict[str, Any]]: См. :func:`aggregate`.
    """
    from search_experts.publications import repo

    if connection is None:
        from knowledge_store.db import connection
    async with connection() as conn:
        rows = await repo.publication_similarities(conn, env, expert_ids, embedding, embedding_model,
                                                   settings.EXPERT_PUB_REQUIRE_AUTHOR)
    return aggregate(rows, settings.EXPERT_PUB_TOP_K, settings.EXPERT_PUB_HALF_LIFE_YEARS, settings.EXPERT_PUB_RECENCY_FLOOR)


def blend(df, scores: Dict[int, Dict[str, Any]], weight: float):
    """Смешивает сходство анкеты со сходством публикаций в столбце ``similarity_embeddings``.

    Добавляет столбцы ``similarity_profile`` (прежнее сходство анкеты), ``similarity_publications`` (NaN у экспертов
    без публикаций), ``publications_count`` и ``publication_top`` (название самой близкой публикации).

    Args:
        df: Кандидаты с ``expert_id`` и ``similarity_embeddings``.
        scores: Результат :func:`publication_scores`.
        weight: Вес сходства публикаций (0…1).

    Returns:
        pandas.DataFrame: Тот же датафрейм с новыми столбцами.
    """
    import numpy as np

    w = min(max(float(weight), 0.0), 1.0)
    ids = df["expert_id"].astype(int)
    df["similarity_profile"] = df["similarity_embeddings"]
    df["similarity_publications"] = ids.map(lambda i: scores[i]["score"] if i in scores else np.nan)
    df["publications_count"] = ids.map(lambda i: scores[i]["count"] if i in scores else 0)
    df["publication_top"] = ids.map(lambda i: scores[i]["top_title"] if i in scores else None)
    has = df["similarity_publications"].notna()
    df.loc[has, "similarity_embeddings"] = (1 - w) * df.loc[has, "similarity_profile"] + w * df.loc[has, "similarity_publications"]
    return df
