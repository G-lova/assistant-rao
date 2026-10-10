"""SQL-запросы риск-мониторинга к внешней БД (MySQL через ``DataFetcher``): загрузка и подстановка списков.

Запросы лежат в ``queries/rm/*.sql``. Списки значений для ``IN (...)`` задаются маркерами ``{ids}``,
``{urls}``, ``{nums}`` и раскрываются в нужное число плейсхолдеров ``?`` (значения уходят биндингами,
а не подставляются в текст запроса).
"""
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
QUERIES_DIR = ROOT / "queries" / "rm"


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """Текст запроса ``queries/rm/<name>`` без комментариев-строк.

    Args:
        name: Имя файла (``files_meta.sql``).

    Returns:
        str: Текст SQL.
    """
    text = (QUERIES_DIR / name).read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("--")).strip()


def render(name: str, head: Sequence[Any] = (), **lists: Sequence[Any]) -> Tuple[str, List[Any]]:
    """Готовит запрос и биндинги: раскрывает маркеры списков в плейсхолдеры.

    Биндинги идут в порядке: сначала ``head`` (одиночные параметры до списков, например
    ``storage_path``), затем списки в порядке появления маркеров в тексте.

    Args:
        name: Имя файла запроса.
        head: Параметры, стоящие в запросе до первого списка.
        **lists: Списки значений по именам маркеров (``ids=[...]``).

    Returns:
        Tuple[str, List[Any]]: Текст запроса и биндинги.

    Пустой список раскрывается в ``IN (NULL)`` — запрос валиден и ничего не находит.

    Raises:
        ValueError: Если в запросе есть маркер, для которого не передан список.
    """
    sql = load(name)
    bindings: List[Any] = list(head)
    order = sorted(((sql.find("{" + k + "}"), k) for k in lists if "{" + k + "}" in sql))
    missing = [k for k in ("ids", "urls", "nums") if "{" + k + "}" in sql and k not in lists]
    if missing:
        raise ValueError(f"{name}: не переданы списки {missing}")
    fmt: Dict[str, str] = {}
    for _, key in order:
        values = list(lists[key])
        if not values:
            values = [None]  # IN (NULL) — пустое совпадение вместо синтаксической ошибки
        fmt[key] = ",".join("?" for _ in values)
        bindings.extend(values)
    return sql.format(**fmt), bindings
