"""Настройки Celery: пределы долгих задач и задачи без сохранения результата.

* предел времени долгих задач меньше ``VISIBILITY_TIMEOUT`` (иначе брокер выдаст задачу повторно, пока она идёт);
* задачи из ``NO_RESULT_TASKS`` не проверяют повторную доставку через ``already_succeeded`` — без сохранённого
  результата такая проверка всегда давала бы «не выполнена»;
* задачи очереди evaluation объявлены с ``LONG_TASK_LIMITS`` (в пуле prefork общий предел 1800 с оборвал бы их);
* число процессов воркера evaluation — из ``configs/settings.py`` (не из .env и не из docker-compose.yml).
"""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def task_functions(path: str) -> dict:
    """Задачи Celery файла: имя задачи → (узел функции, ключевые слова декоратора).

    Args:
        path: Путь к файлу относительно корня репозитория.

    Returns:
        dict: ``{имя задачи: (ast.FunctionDef, [имена keyword/**kwargs декоратора])}``.
    """
    tree = ast.parse(open(os.path.join(ROOT, path), encoding="utf-8").read())
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and getattr(dec.func, "attr", "") == "task":
                kws = {k.arg: k.value for k in dec.keywords}
                name = kws.get("name")
                if isinstance(name, ast.Constant):
                    extra = [k.arg or ast.unparse(k.value) for k in dec.keywords]
                    out[name.value] = (node, extra)
    return out


class CeleryConfigTest(unittest.TestCase):
    """Пределы времени и ``ignore_result``."""

    @classmethod
    def setUpClass(cls):
        from tests.stubs import stubbed_import

        with stubbed_import("celery_app") as m:
            cls.limits = dict(m.LONG_TASK_LIMITS)
            cls.visibility = m.VISIBILITY_TIMEOUT
            cls.no_result = tuple(m.NO_RESULT_TASKS)
            cls.concurrency = m.celery_app.conf.worker_concurrency
            from configs import settings
            cls.settings_concurrency = settings.CELERY_EVAL_CONCURRENCY
        cls.tasks = {**task_functions("tasks.py"), **task_functions("risk_monitoring/tasks.py")}

    def test_limits_below_visibility(self):
        """Предел долгих задач меньше visibility_timeout брокера; мягкий — раньше жёсткого."""
        self.assertLess(self.limits["time_limit"], self.visibility)
        self.assertLess(self.limits["soft_time_limit"], self.limits["time_limit"])

    def test_no_result_tasks_exist_and_skip_redelivery_check(self):
        """Задачи без результата существуют и не вызывают already_succeeded."""
        for name in self.no_result:
            self.assertIn(name, self.tasks, name)
            body = ast.unparse(self.tasks[name][0])
            self.assertNotIn("already_succeeded", body, name)

    def test_evaluation_tasks_have_long_limits(self):
        """Задачи tasks.py (очередь evaluation) — с LONG_TASK_LIMITS."""
        for name, (_, extra) in task_functions("tasks.py").items():
            self.assertIn("LONG_TASK_LIMITS", extra, name)

    def test_concurrency_from_settings(self):
        """``worker_concurrency`` = ``CELERY_EVAL_CONCURRENCY``; в команде celery_worker нет --concurrency и ${…}."""
        self.assertEqual(self.concurrency, max(1, int(self.settings_concurrency)))
        compose = open(os.path.join(ROOT, "docker-compose.yml"), encoding="utf-8").read()
        line = next(x for x in compose.splitlines() if "-Q evaluation" in x and x.strip().startswith("command:"))
        self.assertIn("--pool=prefork", line)
        self.assertNotIn("--concurrency", line)
        self.assertNotIn("${", line)


if __name__ == "__main__":
    unittest.main()
