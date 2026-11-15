"""Схемы API на pydantic v2: поведение, на которое рассчитывают внешние системы (как в pydantic v1).

* число в строковом поле принимается как строка (``reg_number``, ``procurement_id``);
* ``None`` в необязательных строковых полях;
* проверки ``risk_monitoring/router.py``: id, роли, регионы (числом тоже), даты, этапы, ограничения длины списков.
"""
import unittest


def v2():
    """Установлен ли pydantic v2 (иначе тесты пропускаются)."""
    import pydantic

    return pydantic.VERSION.startswith("2")


@unittest.skipUnless(v2(), "нужен pydantic v2")
class SchemasTest(unittest.TestCase):
    """``configs.schemas``."""

    def test_numbers_to_str_and_none(self):
        from configs import schemas

        r = schemas.EISParseRequest(request_method="getDocsByReestrNumberRequest", reg_number=32514850391,
                                    procurement_id=7185, fz=223)
        self.assertEqual((r.reg_number, r.procurement_id, r.fz), ("32514850391", "7185", 223))
        self.assertIsNone(schemas.EISParseRequest(request_method="x", procurement_id=None).procurement_id)
        self.assertIsNone(schemas.FileEntity(id=1, context=None).context)
        self.assertEqual(schemas.EvaluateRequest(expertise_id="42").expertise_id, 42)
        self.assertEqual(schemas.ViolationsReportRequest(data={}).charts, [])


@unittest.skipUnless(v2(), "нужен pydantic v2")
class RouterModelsTest(unittest.TestCase):
    """Модели ``risk_monitoring/router.py``."""

    @classmethod
    def setUpClass(cls):
        from tests.stubs import stubbed_import

        cls.ctx = stubbed_import("risk_monitoring.router")
        cls.r = cls.ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def test_ids(self):
        from pydantic import ValidationError

        self.assertEqual(self.r.AnalysisRequest(ids=[3, "3", 5]).ids, [3, 5])
        for bad in ([], [0], list(range(1, 502))):
            with self.assertRaises(ValidationError):
                self.r.AnalysisRequest(ids=bad)

    def test_roles_regions_dates(self):
        from pydantic import ValidationError

        self.assertEqual(self.r.PackageRequest(ids=[1], roles=["supplier", "supplier"]).roles, ["supplier"])
        with self.assertRaises(ValidationError):
            self.r.PackageRequest(ids=[1], roles=["x"])
        t = self.r.TypalLoadRequest(regions=[34, "7"], dates=["2026-11-01"], kinds=["typal"])
        self.assertEqual((t.regions, t.dates, t.kinds), (["34", "07"], ["2026-11-01"], ["typal"]))
        for kw in ({"regions": ["345"]}, {"dates": ["01.11.2026"]}, {"kinds": ["other"]},
                   {"dates": ["2026-01-01"] * 94}):
            with self.assertRaises(ValidationError):
                self.r.TypalLoadRequest(**kw)

    def test_daily(self):
        from pydantic import ValidationError

        d = self.r.DailyRequest(date="2026-11-13T00:00:00", stages=["files", "xml"])
        self.assertEqual((d.date, d.stages), ("2026-11-13", ["files", "xml"]))
        with self.assertRaises(ValidationError):
            self.r.DailyRequest(date="2026-11-13", stages=["nope"])


if __name__ == "__main__":
    unittest.main()


class BodyTypesTest(unittest.TestCase):
    """Тела запросов в ``main.py`` без строгих ``Dict[str, str]``: pydantic v2 не приводит число к строке — портал
    получал 422 на ``/get-contract-info`` с ``procurement_id`` числом (прод, 15.11.2026)."""

    def test_no_str_dict_bodies(self):
        import ast
        import os

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "main.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        bad = []
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            args = fn.args.args + fn.args.kwonlyargs
            defaults = [None] * (len(fn.args.args) - len(fn.args.defaults)) + fn.args.defaults + fn.args.kw_defaults
            for arg, default in zip(args, defaults):
                is_body = isinstance(default, ast.Call) and getattr(default.func, "id", "") == "Body"
                if is_body and arg.annotation is not None and ast.unparse(arg.annotation) == "Dict[str, str]":
                    bad.append(fn.name)
        self.assertEqual(bad, [])

    @unittest.skipUnless(v2(), "нужен pydantic v2")
    def test_dict_any_accepts_number(self):
        from typing import Any, Dict

        from pydantic import TypeAdapter, ValidationError

        with self.assertRaises(ValidationError):
            TypeAdapter(Dict[str, str]).validate_python({"procurement_id": 12345})   # так было: 422
        self.assertEqual(TypeAdapter(Dict[str, Any]).validate_python({"procurement_id": 12345}),
                         {"procurement_id": 12345})
