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
