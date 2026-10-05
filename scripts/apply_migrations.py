"""Простой раннер SQL-миграций из каталога ``migrations/``.

Нужен потому, что ``init.sql`` в docker выполняется только на пустом томе. Применённые
миграции фиксируются в таблице ``schema_migrations``; повторный запуск ничего не меняет.

Запуск: ``python -m scripts.apply_migrations`` (параметры БД — из переменных ``DB_*``).
"""
import asyncio
import hashlib
import os
import re
from pathlib import Path


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
FILE_RE = re.compile(r"^(\d{3})_.+\.sql$")


def render_sql(sql: str, embedding_dim: int) -> str:
    """Подставляет параметры в текст миграции.

    Args:
        sql: Исходный SQL с плейсхолдером ``{{EMBEDDING_DIM}}``.
        embedding_dim: Размерность вектора эмбеддинга.

    Returns:
        str: SQL с подставленными значениями.
    """
    return sql.replace("{{EMBEDDING_DIM}}", str(int(embedding_dim)))


def list_migrations(directory: Path = MIGRATIONS_DIR) -> list:
    """Возвращает отсортированный список файлов миграций ``NNN_name.sql``.

    Args:
        directory: Каталог с миграциями.

    Returns:
        list[Path]: Файлы миграций по возрастанию номера.
    """
    return sorted(p for p in directory.iterdir() if FILE_RE.match(p.name))


async def apply_all() -> list:
    """Применяет все ещё не применённые миграции, каждую в своей транзакции.

    Если миграция уже применена, но её файл изменился (другая контрольная сумма), выбрасывается
    ``RuntimeError`` — изменённые миграции нужно оформлять новым файлом.

    Returns:
        list[str]: Имена миграций, применённых в этом запуске.
    """
    import asyncpg  # локальный импорт: вспомогательные функции модуля тестируются без asyncpg

    dim = 1024  # значение для необязательного плейсхолдера {{EMBEDDING_DIM}} (в 001 размерность задана прямо)
    conn = await asyncpg.connect(
        host=os.getenv("DB_HOST"), port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME"), user=os.getenv("DB_USER"), password=os.getenv("DB_PASSWORD"),
    )
    applied_now = []
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ DEFAULT NOW())"
        )
        done = {r["name"]: r["checksum"] for r in await conn.fetch("SELECT name, checksum FROM schema_migrations")}
        for path in list_migrations():
            sql = render_sql(path.read_text(encoding="utf-8"), dim)
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            if path.name in done:
                if done[path.name] != checksum:
                    raise RuntimeError(f"Миграция {path.name} изменена после применения")
                continue
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute("INSERT INTO schema_migrations (name, checksum) VALUES ($1, $2)", path.name, checksum)
            applied_now.append(path.name)
            print(f"применена: {path.name}")
    finally:
        await conn.close()
    return applied_now


if __name__ == "__main__":
    asyncio.run(apply_all())
