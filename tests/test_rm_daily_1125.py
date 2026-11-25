"""Сводка и прогон, 25.11.2026: название заказчика, первая оценка ≠ рост, подсказка модели вне фактов, группировка
новых рисков, дата старых событий; один документ в двух файлах — без параллельных запросов к модели."""
import asyncio
import unittest

from risk_monitoring import daily, risk_dynamics


class ShortNameTest(unittest.TestCase):
    """Одно название вместо перечня вариантов из справочника."""

    def test_variants(self):
        """«;» и запятые между вариантами — первый вариант; запятая в кавычках и в полном названии — не разделитель."""
        f = daily.short_org_name
        self.assertEqual(f("ФИЦ КНЦ СО РАН; КНЦ СО РАН"), "ФИЦ КНЦ СО РАН")
        self.assertEqual(f('ИРНИТУ, ФГБОУ ВО "ИРНИТУ", "ИРКУТСКИЙ ПОЛИТЕХ"'), "ИРНИТУ")
        self.assertEqual(f('ООО "Ромашка, Плюс"'), 'ООО "Ромашка, Плюс"')
        full = "Федеральное государственное бюджетное учреждение науки Институт химии, физики, г. Москва"
        self.assertEqual(f(full), full)
        self.assertIsNone(f(None))


class FirstScoredTest(unittest.TestCase):
    """Прошлый балл 0 — первая оценка по документам, а не «рост балла»."""

    def test_item(self):
        """Пересчитана, прошлый 0, новый > 0."""
        self.assertTrue(daily.first_scored({"changed": True, "previous_score": 0, "score": 63.1}))
        self.assertFalse(daily.first_scored({"changed": True, "previous_score": 20, "score": 63.1}))
        self.assertFalse(daily.first_scored({"changed": True, "previous_score": None, "score": 63.1}))

    def test_priority_reason(self):
        """Причина приоритета: «первая оценка по документам»; рост — с запятой."""
        w = {"risk": 0.4, "growth": 0.25, "money": 0.15, "novelty": 0.2}
        first = risk_dynamics.priority(63.1, 63.1, None, [], None, w)["reasons"]
        grow = risk_dynamics.priority(88.4, 44.6, None, [], None, w)["reasons"]
        self.assertIn("первая оценка по документам", first)
        self.assertIn("балл вырос на 44,6", grow)


class FactsTest(unittest.TestCase):
    """Факты сводки: без подсказки модели, новые риски по закупке, номера просроченных контрактов, формулировка
    незавершённого прогона, дата старого события."""

    def _summary(self):
        """Минимальная сводка."""
        return {"date": "2026-11-24", "scope": {"xml": 10, "contracts_in_scope": 2},
                "contracts": {"by_zone": {"red": 0, "yellow": 1, "green": 1}, "changed": 1, "red": []},
                "events": {"total": 0, "by_type": {}},
                "risk_changes": {"by_type": {"RSK-001": 3},
                                 "new": [{"purchase_number": "A", "rule_id": "CTR-008"},
                                         {"purchase_number": "A", "rule_id": "CTR-005"},
                                         {"purchase_number": "B", "rule_id": "CTR-008"}]},
                "execution_open_past_plan": 2, "execution_open_past_plan_list": ["111", "222"],
                "attention": [{"kind": "first_scored", "text": "Первая оценка по документам: балл 63,1",
                               "purchase_number": "A"}],
                "customers": [], "stages": {"files": {"status": "partial", "xml_left": 5}}}

    def test_facts(self):
        """Проверки текста фактов."""
        facts = daily.summary_facts(self._summary())
        text = "\n".join(facts)
        self.assertNotIn("называй числа", text)
        self.assertIn("закупка A — ", next(f for f in facts if f.startswith("Новые риски")))
        self.assertEqual(sum(1 for f in facts if f.startswith("Новые риски")), 1)
        self.assertIn("закупки 111, 222", text)
        self.assertIn("первая оценка по документам — 1", text)
        self.assertIn("продолжается автоматически", text)
        self.assertNotIn("нужно повторить", text)
        self.assertIn("называй числа", daily.RESUME_NOTE)

    def test_event_date(self):
        """Событие старше выгрузки больше чем на неделю — с датой."""
        self.assertEqual(daily.event_date_note("2026-01-14T22:06:29+07:00", "2026-11-24"), " (событие от 14.01.2026)")
        self.assertEqual(daily.event_date_note("2026-11-23T16:22:39+07:00", "2026-11-24"), "")
        self.assertEqual(daily.event_date_note(None, "2026-11-24"), "")


class SameDocumentLockTest(unittest.TestCase):
    """Один документ в двух файлах одной волны: модель — один раз, второй файл — из кэша."""

    def test_lock(self):
        """Два параллельных _cached по одному документу — один вызов produce."""
        from tests.stubs import stubbed_import

        ctx = stubbed_import("risk_monitoring.ai_file_analyzer")
        m = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        fa = m.AIFileAnalyzer.__new__(m.AIFileAnalyzer)
        fa.versions = {"data_extraction": "v1"}
        fa.recompute = False
        fa.extraction_model = "m"
        cache, calls = {}, []

        class Repo:
            """Кэш в памяти."""

            async def get_analysis(self, doc, kind, version, model):
                """Чтение."""
                return cache.get((doc, kind, version))

            async def save_analysis(self, doc, kind, version, model, result, compact, conf):
                """Запись."""
                cache[(doc, kind, version)] = {"result": result}

        fa.repo = Repo()

        async def produce():
            """Запрос к модели."""
            calls.append(1)
            await asyncio.sleep(0.02)
            return {"raw_data": {"summary": "x"}}

        async def main():
            """Два файла с одним документом."""
            return await asyncio.gather(fa._cached(5, "data_extraction", produce), fa._cached(5, "data_extraction", produce))

        a, b = asyncio.run(main())
        self.assertEqual(len(calls), 1)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
