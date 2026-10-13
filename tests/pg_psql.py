"""Тестовый «asyncpg-совместимый» адаптер поверх ``psql`` (в песочнице нет драйверов Postgres).

Подставляет параметры ``$n`` как безопасно экранированные литералы и выполняет SQL настоящим
Postgres — этого достаточно, чтобы проверить синтаксис и семантику запросов репозитория.
"""
import json
import re
import subprocess
from contextlib import asynccontextmanager


def literal(value) -> str:
    """Превращает Python-значение в SQL-литерал (``None`` -> ``NULL``, строки экранируются удвоением кавычек)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "ARRAY[" + ",".join(literal(v) for v in value) + "]" if value else "'{}'"
    return "'" + str(value).replace("'", "''") + "'"


class PsqlConn:
    """Соединение-заглушка с методами ``execute``/``fetchval``/``fetchrow``-подобного интерфейса asyncpg."""

    def __init__(self, host: str, port: int, user: str = "postgres", db: str = "postgres"):
        """Запоминает параметры подключения к тестовому Postgres."""
        self.args = ["psql", "-h", host, "-p", str(port), "-U", user, "-d", db, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-A", "-t"]

    def _render(self, sql: str, params) -> str:
        """Подставляет параметры ``$n`` (от больших номеров к меньшим, чтобы ``$1`` не ломал ``$10``).

        Литерал pgvector ``$n::vector`` (``'[0.1,0.2]'``) переводится в массив ``real[]`` тестовой схемы.
        """
        sql = re.sub(r"(\$\d+)::vector", r"translate(\1, '[]', '{}')::real[]", sql)
        for i in range(len(params), 0, -1):
            sql = re.sub(r"\$%d(?!\d)" % i, lambda _m, v=params[i - 1]: literal(v), sql)
        return sql

    def _run(self, sql: str) -> str:
        """Выполняет SQL через psql и возвращает stdout; при ошибке бросает RuntimeError."""
        r = subprocess.run(self.args, input=sql, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr)
        return r.stdout.strip()

    async def execute(self, sql: str, *params) -> None:
        """Аналог ``asyncpg.Connection.execute``."""
        self._run(self._render(sql, params))

    async def fetchval(self, sql: str, *params):
        """Аналог ``asyncpg.Connection.fetchval`` (значения возвращаются строкой, целые приводятся к int)."""
        out = self._run(self._render(sql, params)).splitlines()
        val = out[0] if out else None
        return int(val) if val is not None and val.lstrip("-").isdigit() else val

    async def executemany(self, sql: str, rows) -> None:
        """Аналог ``asyncpg.Connection.executemany``: выполняет запрос для каждой строки параметров."""
        for params in rows:
            await self.execute(sql, *params)

    async def fetch(self, sql: str, *params):
        """Аналог ``asyncpg.Connection.fetch``: возвращает список словарей (через ``json_agg``)."""
        body = self._render(sql, params)
        if body.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")):   # DML ... RETURNING — только в CTE
            wrapped = "WITH q AS (" + body + ") SELECT COALESCE(json_agg(q), '[]'::json) FROM q"
        else:
            wrapped = "SELECT COALESCE(json_agg(q), '[]'::json) FROM (" + body + ") q"
        return json.loads(self._run(wrapped))

    async def fetchrow(self, sql: str, *params):
        """Аналог ``asyncpg.Connection.fetchrow``: первая строка как словарь или ``None``."""
        rows = await self.fetch(sql, *params)
        return rows[0] if rows else None

    @asynccontextmanager
    async def transaction(self):
        """Заглушка транзакции (каждый запрос выполняется отдельно)."""
        yield
