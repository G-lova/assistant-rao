"""Пополнение псевдонимов организаций (``org_name_aliases``) по изменениям анкет экспертов.

Портал при заполнении ИНН заменяет написание эксперта («ФГБОУ ВО ЗабГУ») официальным названием, и прежнее
написание пропадает. Задача ``org_aliases_task`` раз в сутки сравнивает анкеты с тем, что видела в прошлый раз
(``expert_org_names``): если у места работы или диплома эксперта не было ИНН, а теперь он есть, прежнее написание
записывается псевдонимом этого ИНН — по нему справочник (:mod:`search_experts.org_directory`) найдёт ИНН другого
эксперта, написавшего так же.
"""
from typing import Dict, Iterable, List, Optional, Tuple

from configs.logger import get_logger

logger = get_logger(__name__)

FIELDS = (("organization", "organization_inn", "organization_kpp"), ("diplom", "diplom_inn", "diplom_kpp"))


def _text(value) -> Optional[str]:
    """Строка без лишних пробелов или ``None`` (пусто, NaN)."""
    if value is None or (isinstance(value, float) and value != value):
        return None
    text = " ".join(str(value).split())
    return text or None


def learn(previous: Dict[Tuple[int, str], dict], rows: Iterable[dict]) -> Tuple[List[tuple], List[tuple]]:
    """Находит новые псевдонимы и текущее состояние анкет.

    Args:
        previous: Сохранённое состояние ``(expert_id, field)`` → ``{"name", "inn"}``.
        rows: Строки ``queries/expert_org_requisites.sql``.

    Returns:
        Tuple[List[tuple], List[tuple]]: Псевдонимы ``(написание, ИНН, КПП)`` и состояние для записи
        ``(expert_id, field, name, inn, kpp)``.
    """
    from search_experts.conflict_detector import normalize_inn, normalize_kpp
    from search_experts.org_directory import _core

    aliases, state = [], []
    for r in rows:
        eid = int(r["expert_id"])
        for field, inn_col, kpp_col in FIELDS:
            name, inn, kpp = _text(r.get(field)), normalize_inn(r.get(inn_col)), normalize_kpp(r.get(kpp_col))
            old = previous.get((eid, field))
            if old and not normalize_inn(old.get("inn")) and inn:
                old_name = _text(old.get("name"))
                if old_name and len(_core(old_name)) >= 3 and _core(old_name) != _core(name):
                    aliases.append((old_name, inn, kpp))
            state.append((eid, field, name, inn, kpp))
    return aliases, state


async def save(conn, env: str, aliases: List[tuple], state: List[tuple]) -> int:
    """Записывает псевдонимы и состояние анкет.

    Args:
        conn: Соединение с внутренней БД.
        env: Окружение.
        aliases: ``(написание, ИНН, КПП)``.
        state: ``(expert_id, field, name, inn, kpp)``.

    Returns:
        int: Сколько псевдонимов добавлено (без уже известных).
    """
    added = 0
    for alias, inn, kpp in aliases:
        row = await conn.fetchrow(
            "INSERT INTO org_name_aliases (alias, inn, kpp, source) VALUES ($1, $2, $3, 'learned') "
            "ON CONFLICT (alias, inn) DO NOTHING RETURNING id", alias, inn, kpp)
        added += 1 if row else 0
    if state:
        await conn.executemany(
            """
            INSERT INTO expert_org_names (env, expert_id, field, name, inn, kpp, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, NOW())
            ON CONFLICT (env, expert_id, field) DO UPDATE SET
                name = EXCLUDED.name, inn = EXCLUDED.inn, kpp = EXCLUDED.kpp, updated_at = NOW()
            WHERE expert_org_names.name IS DISTINCT FROM EXCLUDED.name OR expert_org_names.inn IS DISTINCT FROM EXCLUDED.inn
            """,
            [(env, *s) for s in state])
    return added


async def run(env: str) -> dict:
    """Читает анкеты экспертов из внешней БД, пополняет псевдонимы и сохраняет состояние.

    Args:
        env: Окружение.

    Returns:
        dict: ``experts``, ``aliases_added``.
    """
    from configs.config import Config
    from configs.data_fetcher import DataFetcher
    from configs.http_client_manager import HTTPClientManager
    from configs.utils import load_sql_query_async
    from knowledge_store.db import connection, task_pool
    from knowledge_store.scope import normalize_env

    env = normalize_env(env)
    async with HTTPClientManager(timeout=120.0) as http, task_pool():
        db_config = Config.get_database_config(env)
        df = await DataFetcher(db_config.url, db_config.headers, http).fetch_async_expertise_data(
            await load_sql_query_async("expert_org_requisites.sql"), bindings=[])
        rows = df.to_dict("records") if len(df) else []
        async with connection() as conn:
            prev_rows = await conn.fetch("SELECT expert_id, field, name, inn FROM expert_org_names WHERE env = $1", env)
            previous = {(int(r["expert_id"]), r["field"]): dict(r) for r in prev_rows}
            aliases, state = learn(previous, rows)
            added = await save(conn, env, aliases, state)
    if added:
        logger.info(f"Псевдонимы организаций {env}: добавлено {added}")
    return {"env": env, "experts": len(rows), "aliases_added": added}
