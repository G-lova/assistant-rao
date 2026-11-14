"""Ресурсы процесса (пункты 12–17 ревью оптимизации, 14.11.2026).

* ``get_async_db_connection``: пул задачи → пул процесса API (в своём цикле событий) → одно временное соединение;
* ``replace_facts``: удаление и вставка — в транзакции, вставка одним ``executemany``;
* семафоры ``llm_resilience`` удаляются вместе с циклом событий;
* архив ЕИС распаковывается с диска потоком; ``_process_eis_archive`` принимает путь и (по-старому) байты;
* ``brief`` / ``mask_headers`` для журнала.
"""
import asyncio
import gc
import os
import shutil
import sys
import tempfile
import types
import unittest
import zipfile
from contextlib import asynccontextmanager
from unittest import mock


class FakeConn:
    """Соединение asyncpg: запоминает вызовы."""

    def __init__(self, name, log):
        self.name, self.log = name, log

    def transaction(self):
        """Транзакция (асинхронный контекст)."""
        conn = self

        class Tx:
            async def __aenter__(self):
                conn.log.append("begin")

            async def __aexit__(self, *exc):
                conn.log.append("rollback" if exc[0] else "commit")
                return False

        return Tx()

    async def execute(self, sql, *args):
        self.log.append(("execute", sql.split()[0]))

    async def executemany(self, sql, rows):
        self.log.append(("executemany", len(list(rows))))

    async def close(self):
        self.log.append(f"close {self.name}")


class FakePool:
    """Пул asyncpg."""

    def __init__(self, name, log):
        self.name, self.log = name, log

    def acquire(self):
        pool = self

        @asynccontextmanager
        async def cm():
            pool.log.append(f"acquire {pool.name}")
            yield FakeConn(pool.name, pool.log)

        return cm()

    async def close(self):
        self.log.append(f"close pool {self.name}")


class DbConnectionTest(unittest.TestCase):
    """Порядок выбора соединения в ``get_async_db_connection``."""

    def test_priority(self):
        from tests.stubs import stubbed_import

        log = []
        with stubbed_import("configs.working_with_db") as w:
            from knowledge_store import db as ks_db

            async def create_pool(**kw):
                log.append("create_pool")
                return FakePool("app", log)

            async def connect(**kw):
                log.append("connect")
                return FakeConn("temp", log)

            w.asyncpg = types.SimpleNamespace(create_pool=create_pool, connect=connect)

            async def names():
                """Какое соединение выдаётся вне пулов, в пуле API и в пуле задачи."""
                out = []
                async with w.get_async_db_connection() as c:
                    out.append(c.name)
                await w.open_app_pool()
                async with w.get_async_db_connection() as c:
                    out.append(c.name)
                holder = ks_db._LazyPool(1, 2)

                async def get():
                    return FakePool("task", log)

                holder.get = get
                token = ks_db._CURRENT.set(holder)
                try:
                    async with w.get_async_db_connection() as c:
                        out.append(c.name)
                finally:
                    ks_db._CURRENT.reset(token)
                return out

            self.assertEqual(asyncio.run(names()), ["temp", "app", "task"])
            # пул API привязан к своему циклу: в новом цикле (Celery-задача) — временное соединение
            async def other_loop():
                async with w.get_async_db_connection() as c:
                    return c.name

            self.assertEqual(asyncio.run(other_loop()), "temp")
            asyncio.run(w.close_app_pool())
        self.assertIn("close temp", log)
        self.assertEqual(log.count("create_pool"), 1)
        self.assertIn("close pool app", log)


class ReplaceFactsTest(unittest.TestCase):
    """``knowledge_store.repository.replace_facts``."""

    def test_transaction_and_executemany(self):
        from knowledge_store import repository as repo
        from knowledge_store.scope import Scope

        log = []
        conn = FakeConn("c", log)
        facts = [{"fact_key": f"k{i}", "value": {"value": i}, "quote": "ц" * 600} for i in range(30)]
        n = asyncio.run(repo.replace_facts(conn, Scope("stage", 1), "eis_xml", facts))
        self.assertEqual(n, 30)
        self.assertEqual(log, ["begin", ("execute", "DELETE"), ("executemany", 30), "commit"])
        log.clear()
        asyncio.run(repo.replace_facts(conn, Scope("stage", 1), "eis_xml", []))
        self.assertEqual(log, ["begin", ("execute", "DELETE"), "commit"])


class GatesTest(unittest.TestCase):
    """Семафоры запросов к модели не копятся по закрытым циклам событий."""

    def test_weak(self):
        from configs import llm_resilience as lr

        async def take():
            async with lr._gate("http://gates-test"):
                return len(lr._gates)

        for _ in range(5):
            self.assertGreaterEqual(asyncio.run(take()), 1)
        gc.collect()
        alive = [loop for loop in list(lr._gates.keys()) if not loop.is_closed()]
        self.assertEqual(alive, [])
        self.assertLessEqual(len(lr._gates), 1)


class EisArchiveTest(unittest.TestCase):
    """``configs.eis_archive`` и ``_process_eis_archive``."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.zip_path = os.path.join(self.tmp, "a.zip")
        with zipfile.ZipFile(self.zip_path, "w") as z:
            z.writestr("Извещение.docx", b"PK" + b"x" * 300_000)
            z.writestr("папка/", b"")
            z.writestr("без_расширения", b"%PDF-1.4\n" + b"y" * 1000)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_extract(self):
        """Файлы — на диске, папки пропускаются, содержимое совпадает."""
        from configs import eis_archive as ea

        utils = types.SimpleNamespace(get_file_extension=lambda content=None, **kw:
                                      ".pdf" if content and content.startswith(b"%PDF") else ".bin")
        with mock.patch.dict(sys.modules, {"configs.utils": utils}):
            files = ea.extract_zip(self.zip_path, "http://eis/a", "77")
        try:
            self.assertEqual([f["filename"] for f in files], ["Извещение.docx", "без_расширения"])
            self.assertEqual([f["file_extension"] for f in files], [".docx", ".pdf"])
            with open(files[0]["file_path"], "rb") as f:
                self.assertEqual(len(f.read()), 300_002)
            with open(files[1]["file_path"], "rb") as f:
                self.assertTrue(f.read().startswith(b"%PDF-1.4"))
            self.assertEqual({f["source"] for f in files}, {"eis_soap"})
        finally:
            for f in files:
                os.remove(f["file_path"])

    def test_process_path_and_bytes(self):
        """``_process_eis_archive``: путь и байты дают одинаковый результат; временный архив из байтов удаляется."""
        from tests.stubs import stubbed_import

        with stubbed_import("configs.parsing") as p:
            sys.modules["configs.utils"].get_file_extension = lambda content=None, **kw: ".bin"
            parser = p.CloudStorageParser.__new__(p.CloudStorageParser)
            by_path = asyncio.run(parser._process_eis_archive(self.zip_path, "N1", "http://eis/a", "77"))
            with open(self.zip_path, "rb") as f:
                before = set(os.listdir(tempfile.gettempdir()))
                by_bytes = asyncio.run(parser._process_eis_archive(f.read(), "N1", "http://eis/a", "77"))
        try:
            self.assertEqual(by_path["status"], "success")
            self.assertEqual(by_path["file_count"], 2)
            self.assertEqual(by_path["procurement_number"], "N1")
            self.assertEqual(by_path["archive_size"], os.path.getsize(self.zip_path))
            self.assertEqual(by_bytes["file_count"], 2)
            left = set(os.listdir(tempfile.gettempdir())) - before
            self.assertFalse([n for n in left if n.endswith(".zip")])
            self.assertTrue(os.path.exists(self.zip_path))      # переданный путь вызывающий удаляет сам
        finally:
            for res in (by_path, by_bytes):
                for f in res.get("files", []):
                    os.remove(f["file_path"])

    def test_download_to_tempfile(self):
        """Ответ пишется на диск порциями; при обрыве временный файл удаляется."""
        from configs import eis_archive as ea

        def response(parts, fail=False):
            """Ответ aiohttp с телом из частей."""
            async def gen(size):
                for p in parts:
                    yield p
                if fail:
                    raise ConnectionError("обрыв")
            return types.SimpleNamespace(content=types.SimpleNamespace(iter_chunked=gen))

        path, size = asyncio.run(ea.download_to_tempfile(response([b"ab", b"cde"])))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"abcde")
        self.assertEqual(size, 5)
        os.remove(path)
        before = set(os.listdir(tempfile.gettempdir()))
        with self.assertRaises(ConnectionError):
            asyncio.run(ea.download_to_tempfile(response([b"ab"], fail=True)))
        self.assertEqual(set(os.listdir(tempfile.gettempdir())) - before, set())

    def test_bad_zip(self):
        """Не ZIP — ошибка в ответе, без исключения."""
        from tests.stubs import stubbed_import

        bad = os.path.join(self.tmp, "bad.zip")
        with open(bad, "wb") as f:
            f.write(b"not a zip")
        with stubbed_import("configs.parsing") as p:
            parser = p.CloudStorageParser.__new__(p.CloudStorageParser)
            res = asyncio.run(parser._process_eis_archive(bad, "N1", "http://eis/a"))
        self.assertEqual(res["status"], "error")


class LogHelpersTest(unittest.TestCase):
    """``configs.logger.brief`` и ``mask_headers``."""

    def test_brief(self):
        from configs.logger import brief

        self.assertEqual(brief("abc", 10), "abc")
        self.assertEqual(brief("a" * 15, 10), "a" * 10 + "…[+5 симв.]")
        self.assertEqual(brief({"k": "в"}, 0), '{"k": "в"}')
        self.assertEqual(len(brief("a" * 5000, 0)), 5000)

    def test_mask(self):
        from configs.logger import mask_headers

        m = mask_headers({"X-API-Key": "secret123", "Authorization": "Bearer t", "Content-Type": "json",
                          "individualPerson_token": "abc"})
        self.assertEqual(m["Content-Type"], "json")
        self.assertEqual(m["X-API-Key"], "*** (len=9)")
        self.assertNotIn("secret", str(m))
        self.assertTrue(m["individualPerson_token"].startswith("***"))
        self.assertEqual(mask_headers(None), {})


if __name__ == "__main__":
    unittest.main()
