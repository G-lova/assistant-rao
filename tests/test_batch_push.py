"""Пакетная запись пересчётов риск-мониторинга (``risk_monitoring.batch_push``) и асинхронная загрузка экспертизы.

* PATCH идут параллельно, но не больше ``concurrency``; сбой одной закупки не останавливает остальные;
* сохранение у нас — одним вызовом ``save_rm_results``; без него — по одному ``save_rm_result``;
* ``AIRepo.save_rm_results`` — один ``executemany`` в транзакции;
* ``TasksPipeline.load_expertise`` берёт данные асинхронным запросом (без ``requests`` в конструкторе).
"""
import asyncio
import types
import unittest

import pandas as pd

from risk_monitoring.batch_push import push_and_save


class Repo:
    """Хранилище результатов в памяти с пакетной записью."""

    def __init__(self):
        self.batches = []

    async def save_rm_results(self, env, kind, items):
        """Запоминает пачку."""
        self.batches.append((env, kind, list(items)))


class BatchPushTest(unittest.TestCase):
    """``push_and_save``."""

    def test_parallel_limited_and_errors(self):
        """Не больше 2 PATCH одновременно; сбой закупки 3 — pushed=False и ошибка; одна пачка в хранилище."""
        state = {"now": 0, "max": 0, "calls": []}

        async def patch(table, eid, ai):
            """PATCH внешней БД: закупка 3 — ошибка."""
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            await asyncio.sleep(0.01)
            state["now"] -= 1
            state["calls"].append((table, eid))
            if eid == 3:
                raise RuntimeError("422")

        repo, out = Repo(), {"checked": 5, "updated": 0, "pushed": 0, "errors": 0}
        items = [(i, {"score": i}) for i in range(1, 6)]
        asyncio.run(push_and_save(repo, patch, "stage", "contract", "risk_monitoring_contracts", items, True, out,
                                  concurrency=2))
        self.assertEqual(state["max"], 2)
        self.assertEqual(len(state["calls"]), 5)
        self.assertEqual(out, {"checked": 5, "updated": 5, "pushed": 4, "errors": 1})
        self.assertEqual(len(repo.batches), 1)
        env, kind, saved = repo.batches[0]
        self.assertEqual((env, kind), ("stage", "contract"))
        self.assertEqual([s[0] for s in saved], [1, 2, 3, 4, 5])          # порядок сохранён
        self.assertEqual(saved[2][2:], (False, "422"))
        self.assertEqual(saved[0][2:], (True, None))

    def test_no_push(self):
        """Без записи во внешнюю БД — PATCH не вызывается, результаты сохраняются с pushed=False."""
        async def patch(*a):
            raise AssertionError("PATCH не должен вызываться")

        repo, out = Repo(), {}
        asyncio.run(push_and_save(repo, patch, "stage", "contract", "t", [(7, {"a": 1})], False, out))
        self.assertEqual(repo.batches[0][2], [(7, {"a": 1}, False, None)])
        self.assertEqual(out, {"updated": 1, "pushed": 0, "errors": 0})

    def test_empty_and_fallback(self):
        """Пустая пачка — ничего; хранилище без save_rm_results — по одному save_rm_result."""
        saved = []

        class OldRepo:
            """Хранилище без пакетной записи."""

            async def save_rm_result(self, env, kind, eid, ai, pushed, err):
                saved.append((eid, pushed))

        out = {}
        asyncio.run(push_and_save(OldRepo(), None, "stage", "contract", "t", [], True, out))
        self.assertEqual((saved, out), ([], {}))
        asyncio.run(push_and_save(OldRepo(), None, "stage", "contract", "t", [(1, {}), (2, {})], True, out))
        self.assertEqual(saved, [(1, False), (2, False)])


class SaveRmResultsTest(unittest.TestCase):
    """``AIRepo.save_rm_results``: один ``executemany`` в транзакции, те же параметры, что у ``save_rm_result``."""

    def test_executemany(self):
        from tests.stubs import stubbed_import

        calls = []

        class Conn:
            """Соединение asyncpg."""

            def transaction(self):
                return self

            async def __aenter__(self):
                calls.append("begin")
                return self

            async def __aexit__(self, *exc):
                calls.append("commit")
                return False

            async def executemany(self, sql, args):
                calls.append(("executemany", sql, args))

        class Pool:
            """Пул asyncpg."""

            def acquire(self):
                return Conn()

            async def execute(self, sql, *args):
                calls.append(("execute", sql, args))

        with stubbed_import("configs.ai_repo") as m:
            repo = m.AIRepo.__new__(m.AIRepo)
            repo.pool = Pool()
            ai = {"pipeline": {"status": "completed", "debug": True}, "score": 1}
            n = asyncio.run(repo.save_rm_results("stage", "contract", [(5, ai, True, None), (6, {}, False, "e")]))
            asyncio.run(repo.save_rm_result("stage", "contract", 5, ai, True, None))
            sql_const = m.SQL_SAVE_RM_RESULT
        self.assertEqual(n, 2)
        many = [c for c in calls if isinstance(c, tuple) and c[0] == "executemany"][0]
        single = [c for c in calls if isinstance(c, tuple) and c[0] == "execute"][0]
        self.assertEqual(many[1], sql_const)
        self.assertEqual(many[2][0], single[2])                       # те же параметры, что у save_rm_result
        self.assertEqual(many[2][1][4:], (None, False, False, "e"))
        self.assertEqual(calls[0], "begin")
        self.assertIn("commit", calls)


class PipelineLoadTest(unittest.TestCase):
    """``TasksPipeline.load_expertise`` / ``run_pipeline`` без синхронного запроса в конструкторе."""

    def pipeline(self, module, df):
        """Конвейер без конструктора с фальшивым ``DataFetcher``."""
        p = module.TasksPipeline.__new__(module.TasksPipeline)
        p.http_manager, p.media_storage, p.file_storage, p.expertise_id = None, "m", "f", 42
        p.df, p.cloud_parser, p.x_api_database = None, None, "stage"
        seen = []

        async def fetch(sql, bindings):
            """Асинхронный запрос к порталу."""
            seen.append(bindings)
            return df

        def sync(*a, **kw):
            raise AssertionError("синхронный запрос не должен вызываться")

        p.data_fetcher = types.SimpleNamespace(fetch_async_expertise_data=fetch, fetch_expertise_data=sync)
        return p, seen

    def test_load(self):
        """Данные загружаются асинхронно, создаётся парсер ссылок; пустой ответ — ValueError."""
        from tests.stubs import stubbed_import

        with stubbed_import("evaluate_documents.evaluate_documents_pipeline") as m:
            m.load_sql_query = lambda name: "SELECT ?"
            m.CloudStorageParser = lambda mgr, obj: ("parser", obj)
            p, seen = self.pipeline(m, pd.DataFrame([{"id": 42, "object": "Поставка"}]))
            asyncio.run(p.load_expertise())
            self.assertEqual(seen, [["m", "f", 42]])
            self.assertEqual(p.cloud_parser, ("parser", "Поставка"))
            empty, _ = self.pipeline(m, pd.DataFrame())
            with self.assertRaises(ValueError):
                asyncio.run(empty.load_expertise())

    def test_run_pipeline_loads_lazily(self):
        """``run_pipeline`` без ``create()`` сам загружает данные."""
        from tests.stubs import stubbed_import

        with stubbed_import("evaluate_documents.evaluate_documents_pipeline") as m:
            m.load_sql_query = lambda name: "SELECT ?"
            m.CloudStorageParser = lambda mgr, obj: obj
            p, seen = self.pipeline(m, pd.DataFrame([{"id": 42, "object": "Поставка"}]))

            async def inner():
                return {"ok": True}

            p._run_pipeline = inner
            p._log_run_summary = lambda *a, **kw: None
            res = asyncio.run(p.run_pipeline())
        self.assertEqual(res, {"ok": True})
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
