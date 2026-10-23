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


class XlsxRawTest(unittest.TestCase):
    """Книга без обычных листов в workbook.xml: текст из XML листов."""

    def test_raw_text(self):
        """Общие строки, строки в ячейке и числа; не ZIP — пусто."""
        import zipfile

        from risk_monitoring.xlsx_raw import xlsx_raw_text

        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "book.xlsx")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("xl/workbook.xml", "<workbook><sheets/></workbook>")
            zf.writestr("xl/sharedStrings.xml", "<sst><si><t>Ведомость объёмов</t></si><si><t>Бетон &amp; арматура</t></si></sst>")
            zf.writestr("xl/worksheets/sheet1.xml",
                        '<worksheet><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row>'
                        '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2"><v>12.5</v></c>'
                        '<c r="C2" t="inlineStr"><is><t>м3</t></is></c></row></sheetData></worksheet>')
        self.assertEqual(xlsx_raw_text(path), "Лист 1:\nВедомость объёмов\nБетон & арматура | 12.5 | м3")
        bad = os.path.join(tmp, "bad.xlsx")
        with open(bad, "wb") as f:
            f.write(b"not a zip")
        self.assertEqual(xlsx_raw_text(bad), "")


class ArchiveVolumesTest(unittest.TestCase):
    """Многотомные архивы: первый том читается со всеми томами, остальные — ссылка на первый."""

    def test_volume_info(self):
        """partNN.rar и .zip.001; обычный архив — не том."""
        from risk_monitoring.archive_volumes import volume_info

        self.assertEqual(volume_info("Проектная документация.part04.rar")[:2], ("Проектная документация", 4))
        self.assertEqual(volume_info("Проектная документация.part04.rar")[2], "Проектная документация.part%.rar")
        self.assertEqual(volume_info("ПД_1.zip.002")[:2], ("ПД_1.zip", 2))
        self.assertEqual(volume_info("ПД_1.zip.002")[2], "ПД\\_1.zip.%")
        self.assertIsNone(volume_info("Рабочая документация.rar"))

    def test_siblings_and_complete(self):
        """Тома одной основы по порядку; пропуск тома — неполный набор."""
        from risk_monitoring.archive_volumes import complete, siblings

        rows = [{"id": 5283, "file_name": "ПД.part02.rar"}, {"id": 5285, "file_name": "ПД.part01.rar"},
                {"id": 9, "file_name": "Другое.part01.rar"}, {"id": 5282, "file_name": "ПД.part03.rar"}]
        vols = siblings(rows, "ПД")
        self.assertEqual([v["id"] for v in vols], [5285, 5283, 5282])
        self.assertTrue(complete(vols))
        self.assertFalse(complete([vols[0], vols[2]]))

    def test_volume_passport(self):
        """Том 2+ — паспорт без анализа со ссылкой на первый том."""
        try:
            from risk_monitoring.ai_file_analyzer import AIFileAnalyzer
        except ImportError as e:
            raise unittest.SkipTest(f"нет зависимостей: {e}")
        fa = AIFileAnalyzer.__new__(AIFileAnalyzer)
        fa.versions, fa.force = {}, False
        vols = [{"id": 5285, "file_name": "ПД.part01.rar"}, {"id": 5283, "file_name": "ПД.part02.rar"}]
        ai = fa._volume_analysis({"id": 5283, "file_name": "ПД.part02.rar"}, None, 1, vols)
        self.assertEqual(ai["archive_volume"]["first_file_id"], 5285)
        self.assertEqual(ai["pipeline"]["result"], "archive_volume")
        self.assertEqual(ai["risks"], [])


class Run1021FalsePositivesTest(unittest.TestCase):
    """Ложные риски промежуточных итогов 21.10.2026 (журнал risk_monitoring_ai_analysis_log)."""

    def test_rejected(self):
        """ПП 1042 обеих сторон, ПП 2571 с «N», ПП 2604, реестры российской продукции, подсказка площадки, МНН,
        Ethernet/RJ-45, приказы о методиках НМЦК, реестровые записи, шапка таблицы и итоги сметы."""
        from risk_monitoring.document_risk_analyzer import rejected_by_rules

        r = lambda c, f: rejected_by_rules({"code": c, "evidence": {"fragment": f}})  # noqa: E731
        self.assertTrue(r("FIN-016", "7.2.1. Пеня начисляется за каждый день просрочки исполнения заказчиком обязательства"))
        self.assertTrue(r("DOC-006", "9.3.2. Штрафы начисляются за неисполнение или ненадлежащее исполнение Поставщиком"))
        self.assertTrue(r("DOC-004", "Подрядчик должен в соответствии с Постановлением Правительства РФ от 29.12.2021 N 2571"))
        self.assertTrue(r("DOC-004", "Xmax - предельное максимальное значение характеристики квалификации участников"))
        self.assertTrue(r("DOC-005", "номер реестровой записи из реестра российской промышленной продукции"))
        self.assertTrue(r("DOC-006", "порядковый номер реестровой записи из единого реестра российских программ для ЭВМ"))
        self.assertTrue(r("DOC-002", 'рекомендуется в поле "Товарный знак" экранной формы веб-интерфейса электронной площадки'))
        self.assertTrue(r("DOC-002", "nameDrugProduct: ТИРОФИБАН, КОНЦЕНТРАТ ДЛЯ ПРИГОТОВЛЕНИЯ РАСТВОРА"))
        self.assertTrue(r("DOC-002", "Тип абонентского порта: медный, ethernet, RJ-45."))
        self.assertTrue(r("DOC-010", "Начальная (максимальная) цена контракта сформирована в соответствии с приказом Минздрава России"))
        self.assertTrue(r("DOC-010", "Предложение 2: Реестровая запись: 2272400261625001102"))
        self.assertTrue(r("DOC-007", "№ п/п Наименование товара Объем поставки Предложение 1 Предложение 2"))
        self.assertTrue(r("DOC-007", "ИТОГИ ПО СМЕТЕ: ВСЕГО по смете Итого прямые затраты (справочно) 640133,30"))

    def test_kept(self):
        """Реальные признаки остаются: модель принтера, торговая марка штукатурки, совместимость с конкретным аппаратом."""
        from risk_monitoring.document_risk_analyzer import rejected_by_rules

        r = lambda c, f: rejected_by_rules({"code": c, "evidence": {"fragment": f}})  # noqa: E731
        self.assertFalse(r("DOC-002", "Совместимость: Canon i-SENSYS MF752CDW"))
        self.assertFalse(r("DOC-002", "Смесь штукатурная «Ротбанд», КНАУФ кг 340"))
        self.assertFalse(r("DOC-007", "5.1. Срок оказания услуг: С 01.08.2026 по 31.07.2027. Дата окончания исполнения контракта 15.09.2027."))

    def test_readability_extraction_failed(self):
        """Сбой извлечения на всех блоках — «читаемость не оценена», а не deny."""
        from risk_monitoring import passport

        rd = passport.readability_block({"readability": {"status": "deny", "issues": ["Ошибка анализа документа"]},
                                         "raw_data": {}}, 3, 5000)
        self.assertIsNone(rd["status"])
        self.assertTrue(rd["extraction_failed"])
        self.assertEqual(passport.readability_block({"readability": {"status": "allow"}}, 1, 10)["status"], "allow")


class WorkerLogTest(unittest.TestCase):
    """Журнал воркеров в файл: имя по процессу Celery."""

    def test_paths(self):
        """Воркер риск-мониторинга, общий воркер и beat — разные файлы в logs/."""
        try:
            import celery_app
        except ImportError as e:
            raise unittest.SkipTest(f"нет celery: {e}")
        rm = ["celery", "-A", "celery_app", "worker", "-Q", "risk_monitoring", "-n", "rm@%h"]
        self.assertTrue(celery_app.worker_log_path(rm).endswith("logs/celery_rm.log"))
        self.assertTrue(celery_app.worker_log_path(["celery", "-A", "celery_app", "worker", "-Q", "evaluation"]).endswith("logs/celery.log"))
        self.assertTrue(celery_app.worker_log_path(["celery", "-A", "celery_app", "beat"]).endswith("logs/celery_beat.log"))


class TypalEisLimitTest(unittest.TestCase):
    """Типовые положения: лимит запросов ЕИС («Код 13») — пауза и повтор; «нет архива» — не предупреждение."""

    def test_retry_on_limit(self):
        """Первый ответ — Код 13, второй — без документов; запросов два, результат пустой без ошибки."""
        from risk_monitoring import typal_loader

        calls = []

        class Eis:
            async def _parse_eis_soap_ip(self, *a, **kw):
                calls.append(kw.get("org_region"))
                if len(calls) == 1:
                    return {"status": "error", "error": "Ошибка парсинга ЕИС: Код 13: Превышено количество допустимых запросов"}
                return {"status": "error", "error": "Не удалось найти URL архива в ответе ЕИС"}

        tl = typal_loader.TypalLoader.__new__(typal_loader.TypalLoader)
        tl.eis = Eis()
        with mock.patch.object(settings, "TYPAL_EIS_LIMIT_WAIT_SEC", 0), \
                mock.patch.object(settings, "TYPAL_EIS_MIN_INTERVAL_SEC", 0):
            out = asyncio.run(tl.load_region("78", "2026-10-21"))
        self.assertEqual(out, [])
        self.assertEqual(calls, ["78", "78"])
        self.assertTrue(typal_loader.NO_DATA_RE.search("Не удалось найти URL архива в ответе ЕИС"))
