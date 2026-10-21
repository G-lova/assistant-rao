"""Прогон 21.10.2026: OCR больше контекста, отклонённая ссылка на предыдущую версию, бюджет времени прогона."""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

from configs import settings


class OcrFitTest(unittest.TestCase):
    """Страница крупного формата не переполняет контекст модели."""

    def setUp(self):
        """PIL нужен для изображений."""
        try:
            from PIL import Image  # noqa: F401
        except ImportError as e:
            raise unittest.SkipTest(f"нет PIL: {e}")

    def test_fit_and_shrink_on_overflow(self):
        """Большая страница уменьшается заранее; на «exceeds maximum context length» — ещё раз и повтор."""
        from PIL import Image

        from risk_monitoring import pdf_text

        big = Image.new("RGB", (3000, 4200), "white")
        fitted = pdf_text.fit_image(big)
        self.assertLessEqual(fitted.size[0] * fitted.size[1], pdf_text.OCR_MAX_PIXELS)
        small = Image.new("RGB", (100, 100))
        self.assertIs(pdf_text.fit_image(small), small)

        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "p.jpg")
        Image.new("RGB", (1000, 1000), "white").save(path, "JPEG")
        calls = []

        async def fake_call(client, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("Error code: 400 - Input length (16774) exceeds model's maximum context length (16384).")
            return '{"raw_text": "Текст страницы"}'

        proc = type("P", (), {"client": None, "model": "m", "ocr_prompt": "p", "OCR_SCHEMA": {}})()
        with mock.patch("risk_monitoring.llm_json.call_llm", fake_call):
            text = asyncio.run(pdf_text.ocr_image(proc, path, "f.pdf", 1.05))
        self.assertEqual(text, "Текст страницы")
        self.assertEqual(len(calls), 2)
        with Image.open(path) as im:
            self.assertEqual(im.size, (700, 700))


class PreviousRejectedTest(unittest.TestCase):
    """422 на previous_scope_id — запись без ссылки, причина в pipeline."""

    def test_retry_without_previous(self):
        """Первый PATCH — 422 «Предыдущая версия…», второй — без previous_scope_id."""
        try:
            from risk_monitoring import risk_monitoring_api as api_mod
        except ImportError as e:
            raise unittest.SkipTest(f"нет зависимостей: {e}")
        api = api_mod.RiskMonitoringAPI.__new__(api_mod.RiskMonitoringAPI)
        sent = []

        async def request(method, url=None, **kw):
            sent.append(dict(kw["json"]["ai_analysis"]["pipeline"]))
            if len(sent) == 1:
                raise api_mod.RiskAPIError("HTTP 422: Предыдущая версия допустима только для более раннего файла", 422)
            return {"status": "ok", "data": {"id": 5367}}

        api._request = request
        ai = {"pipeline": {"previous_scope_id": 9999}}
        asyncio.run(api.patch_ai_analysis("risk_monitoring_files", 5367, ai))
        self.assertEqual(sent[1]["previous_scope_id"], None)
        self.assertEqual(ai["pipeline"]["previous_scope_rejected"]["id"], 9999)
        api._request = mock.AsyncMock(side_effect=api_mod.RiskAPIError("HTTP 422: другое", 422))
        with self.assertRaises(api_mod.RiskAPIError):
            asyncio.run(api.patch_ai_analysis("risk_monitoring_files", 1, {"pipeline": {"previous_scope_id": 1}}))


class BudgetTest(unittest.TestCase):
    """Бюджет времени на документы: остальные пачки — следующим запуском, сводка — partial."""

    def test_partial(self):
        """При исчерпанном бюджете пачки не начинаются, этапы и сводка — partial, повтор не заблокирован."""
        from risk_monitoring import daily

        run = object.__new__(daily.DailyRun)
        run.stages_on, run.stages = daily.STAGES, {}
        xa = mock.MagicMock()
        xa.pending_files = mock.AsyncMock(return_value=[])
        xa.analyze_ids = mock.AsyncMock(return_value=[])
        cache = mock.MagicMock()
        scope = {"files": 0}
        with mock.patch.object(settings, "DAILY_DOCUMENTS_BUDGET_SEC", -1), mock.patch.object(settings, "DAILY_XML_BATCH", 2):
            asyncio.run(run._documents([1, 2, 3], xa, cache, scope, {}, {}))
        self.assertEqual(run.stages["xml"]["status"], "partial")
        self.assertEqual(run.stages["xml"]["xml_left"], 3)
        xa.analyze_ids.assert_not_called()
        s = daily.build_summary(export_date="2026-07-03", environment="stage", cards={}, contracts={}, changed=[],
                                events={}, xml_cards={}, customers=[], scope={"xml": 3}, stages=run.stages)
        self.assertIn("сводка предварительная", " ".join(daily.summary_facts(s)))
        self.assertFalse(daily.completed({"ai_analysis": {"pipeline": {"status": "partial"}}}))


if __name__ == "__main__":
    unittest.main()
