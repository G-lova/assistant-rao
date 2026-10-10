"""Тесты разбора XML ЕИС и классификации событий риск-мониторинга (``risk_monitoring.xml_events``)."""
import unittest
from pathlib import Path

from risk_monitoring import risk_catalog, xml_events

DATA = Path(__file__).resolve().parent / "data" / "rm"


def load(name):
    """Разбирает XML из ``tests/data/rm``."""
    return xml_events.parse_xml((DATA / name).read_bytes())


class ParseTests(unittest.TestCase):
    """Разбор и нормализация XML."""

    def test_export_root_and_namespaces_removed(self):
        """Снимается ``<export>``, префиксы пространств имён убираются, версия читается из XML."""
        tag, version, body = load("notice_v0.xml")
        self.assertEqual(tag, "epNotificationEF2020")
        self.assertEqual(version, 0)
        self.assertEqual(body["commonInfo"]["purchaseNumber"], "0354100008624000011")
        self.assertNotIn("ns2:commonInfo", body)

    def test_rejects_dtd(self):
        """XML с DTD/сущностями не принимается (защита от XXE и «XML-бомб»)."""
        raw = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaa">]><x>&a;</x>'
        with self.assertRaises(ValueError):
            xml_events.parse_xml(raw)

    def test_rejects_empty(self):
        """Пустой XML — ошибка."""
        with self.assertRaises(ValueError):
            xml_events.parse_xml(b"")


class DiffTests(unittest.TestCase):
    """Сравнение версий."""

    def setUp(self):
        """Две версии извещения."""
        _, _, self.v0 = load("notice_v0.xml")
        _, _, self.v1 = load("notice_v1.xml")

    def test_service_fields_ignored(self):
        """Служебные поля (id, versionNumber, publishDTInEIS, printForm) не дают изменений."""
        changes = xml_events.diff(xml_events.strip_ignored(self.v0), xml_events.strip_ignored(self.v1))
        paths = " ".join(c["path"] for c in changes)
        for noise in ("versionNumber", "publishDTInEIS", "printFormInfo", ".id"):
            self.assertNotIn(noise, paths)

    def test_nmck_change_with_percent(self):
        """Изменение НМЦК: было/стало, дельта и процент."""
        changes = xml_events.diff(xml_events.strip_ignored(self.v0), xml_events.strip_ignored(self.v1))
        nmck = [c for c in changes if c["label"] == "НМЦК"]
        self.assertEqual(len(nmck), 1)
        self.assertEqual(nmck[0]["old"], "12100920")
        self.assertEqual(nmck[0]["new"], "13310000")
        self.assertAlmostEqual(nmck[0]["delta_pct"], 9.99, places=2)

    def test_keyed_lists_stable(self):
        """Перестановка элементов списка без изменений содержимого не даёт изменений."""
        a = {"items": [{"sid": "1", "v": "x"}, {"sid": "2", "v": "y"}]}
        b = {"items": [{"sid": "2", "v": "y"}, {"sid": "1", "v": "x"}]}
        self.assertEqual(xml_events.diff(a, b), [])

    def test_single_item_to_list_with_key(self):
        """Переход «один элемент с sid → список» не порождает шума."""
        a = {"stages": {"stage": {"sid": "1", "sum": "10"}}}
        b = {"stages": {"stage": [{"sid": "1", "sum": "10"}, {"sid": "2", "sum": "5"}]}}
        changes = xml_events.diff(a, b)
        self.assertTrue(all("sid=2" in c["path"] for c in changes), changes)


class ClassifyTests(unittest.TestCase):
    """Тип события, подтипы и детерминированные риски."""

    def test_notice_first_version_is_publication(self):
        """Извещение версии 0 — «Опубликована закупка»."""
        tag, _, body = load("notice_v0.xml")
        event = xml_events.classify(tag, 0, False, [], xml_events.extract_facts(tag, body))
        self.assertEqual(event["code"], "PUR-001")
        self.assertTrue(event["first_version"])

    def test_notice_change_subtypes(self):
        """Новая версия извещения: изменены НМЦК, сроки и документация."""
        tag, _, v0 = load("notice_v0.xml")
        _, _, v1 = load("notice_v1.xml")
        changes = xml_events.diff(xml_events.strip_ignored(v0), xml_events.strip_ignored(v1))
        docs = xml_events.document_changes(xml_events.attachments(v0), xml_events.attachments(v1))
        event = xml_events.classify(tag, 1, True, changes, xml_events.extract_facts(tag, v1), docs)
        codes = [s["code"] for s in event["subtypes"]]
        self.assertEqual(event["code"], "PUR-003")
        self.assertIn("PUR-004", codes)
        self.assertIn("PUR-005", codes)
        self.assertFalse(event["previous_missing"])

    def test_nmck_risk_threshold(self):
        """Риск FIN-007 — только при изменении НМЦК от 10 %."""
        changes = [{"path": "a.maxPrice", "label": "НМЦК", "old": "100", "new": "125", "change": "modified", "delta_pct": 25.0}]
        event = {"code": "PUR-003", "subtypes": [], "title": "x", "xml_tag": "epNotificationEF2020"}
        risks = xml_events.rule_risks(event, changes, {})
        self.assertEqual([r["rule_id"] for r in risks], ["FIN-007"])
        self.assertEqual(risks[0]["level"], 0.8)
        small = [dict(changes[0], delta_pct=5.0)]
        self.assertEqual(xml_events.rule_risks(event, small, {}), [])

    def test_contract_modification(self):
        """Новая версия контракта с доп. соглашением: подтипы цены и сроков, риски CTR-001/CTR-003/CTR-004."""
        tag, _, v0 = load("contract_v0.xml")
        _, _, v1 = load("contract_v1.xml")
        facts = xml_events.extract_facts(tag, v1)
        self.assertEqual(facts["modification_reason_code"], "011")
        changes = xml_events.diff(xml_events.strip_ignored(v0), xml_events.strip_ignored(v1))
        event = xml_events.classify(tag, 1, True, changes, facts)
        codes = {event["code"]} | {s["code"] for s in event["subtypes"]}
        self.assertTrue({"CON-002", "CON-003", "CON-004"} <= codes, codes)
        risk_codes = {r["rule_id"] for r in xml_events.rule_risks(event, changes, facts)}
        self.assertTrue({"CTR-001", "CTR-003", "CTR-004"} <= risk_codes, risk_codes)

    def test_contract_first_version(self):
        """Контракт версии 0 — «Заключён контракт», без подтипов."""
        tag, _, v0 = load("contract_v0.xml")
        event = xml_events.classify(tag, 0, False, [], xml_events.extract_facts(tag, v0))
        self.assertEqual(event["code"], "CON-001")
        self.assertEqual(event["subtypes"], [])

    def test_termination(self):
        """Сведения об исполнении с расторжением — «Расторжение контракта», критично, риск CTR-009."""
        tag, _, body = load("procedure_term.xml")
        facts = xml_events.extract_facts(tag, body)
        event = xml_events.classify(tag, 0, False, [], facts)
        self.assertEqual(event["code"], "CON-007")
        self.assertEqual(event["importance"], "critical")
        self.assertEqual([r["rule_id"] for r in xml_events.rule_risks(event, [], facts)], ["CTR-009"])

    def test_previous_missing(self):
        """Версия больше 0 без предыдущей версии в БД — помечается ``previous_missing``."""
        event = xml_events.classify("epNotificationEF2020", 2, False, [], {})
        self.assertEqual(event["code"], "PUR-002")
        self.assertTrue(event["previous_missing"])

    def test_known_tags(self):
        """Теги из статистики «сирот» классифицируются, а не уходят в «Прочее»."""
        for tag in ("fcsPlacementResult", "fcsProposalsResult", "epProtocolEF2020Final", "contractAvailableForElAct",
                    "epClarificationDoc", "epProtocolEF2020SubmitOffers", "epProtocolCancel", "epNoticeApplicationCancel",
                    "epProtocolEvasion", "epProtocolEOK2020FirstSections"):
            self.assertNotEqual(xml_events.classify(tag, 0, False, [], {})["code"], "OTH-000", tag)


class FactsTests(unittest.TestCase):
    """Сведения, вложения, ИКЗ, привязка."""

    def test_facts_notice(self):
        """Номер закупки, ИКЗ, НМЦК, способ, дата публикации."""
        tag, _, body = load("notice_v0.xml")
        f = xml_events.extract_facts(tag, body)
        self.assertEqual(f["purchase_number"], "0354100008624000011")
        self.assertEqual(f["ikz"], "241572000318857200100100160164339243")
        self.assertEqual(f["max_price"], 12100920.0)
        self.assertEqual(f["placing_way_code"], "EAB20")
        self.assertTrue(f["published_at"].startswith("2024-11-12"))

    def test_parse_ikz(self):
        """ИКЗ раскладывается на год, ИКУ, ИНН/КПП заказчика, ОКПД2 и КВР."""
        p = xml_events.parse_ikz("241572000318857200100100160164339243")
        self.assertEqual(p["year"], "2024")
        self.assertEqual(p["customer_inn"], "5720003188")
        self.assertEqual(p["customer_kpp"], "572001001")
        self.assertEqual(p["okpd2"], "4339")
        self.assertEqual(p["kvr"], "243")
        self.assertIsNone(xml_events.parse_ikz("123"))

    def test_attachments_replaced(self):
        """ТЗ с той же подписью, но новой ссылкой — «заменён», а не «удалён + добавлен»; печатная форма не вложение."""
        _, _, v0 = load("notice_v0.xml")
        _, _, v1 = load("notice_v1.xml")
        a0, a1 = xml_events.attachments(v0), xml_events.attachments(v1)
        self.assertEqual(len(a0), 2)
        ch = xml_events.document_changes(a0, a1)
        self.assertEqual(ch["added"], [])
        self.assertEqual(ch["removed"], [])
        self.assertEqual(len(ch["replaced"]), 1)
        self.assertEqual(ch["replaced"][0]["after"]["url"], "https://zakupki.gov.ru/file/3")

    def test_canonical_org(self):
        """Каноническая строка организации: текущий год, мониторинг, активный статус, источник, меньший id."""
        rows = [
            {"id": 777, "year": 2026, "monitoring": 0, "status_id": 1, "source": "eis"},
            {"id": 779, "year": 2026, "monitoring": 0, "status_id": 1, "source": "eis"},
            {"id": 132, "year": 2026, "monitoring": 1, "status_id": 1, "source": None},
            {"id": 34, "year": 2025, "monitoring": 1, "status_id": 1, "source": None},
        ]
        self.assertEqual(xml_events.canonical_org(rows, 2026)["id"], 132)
        self.assertEqual(xml_events.canonical_org(rows[:2], 2026)["id"], 777)
        self.assertIsNone(xml_events.canonical_org([], 2026))

    def test_pick_by_ikz(self):
        """Повторная закупка с тем же ИКЗ: берётся последняя, опубликованная не позже события."""
        cards = [{"id": 1, "date_public": "2024-01-10"}, {"id": 2, "date_public": "2024-03-01"},
                 {"id": 3, "date_public": "2024-06-01"}]
        card, ambiguous = xml_events.pick_by_ikz(cards, "2024-04-15T10:00:00+03:00")
        self.assertEqual(card["id"], 2)
        self.assertTrue(ambiguous)


class AnalysisTests(unittest.TestCase):
    """Итоговый ``ai_analysis`` события."""

    def test_build_without_llm(self):
        """Без LLM: резюме по шаблону, риски правил, привязка, блок pipeline."""
        tag, _, v0 = load("notice_v0.xml")
        _, _, v1 = load("notice_v1.xml")
        facts = xml_events.extract_facts(tag, v1)
        changes = xml_events.diff(xml_events.strip_ignored(v0), xml_events.strip_ignored(v1))
        docs = xml_events.document_changes(xml_events.attachments(v0), xml_events.attachments(v1))
        event = xml_events.classify(tag, 1, True, changes, facts, docs)
        files = [{"file_id": 11, "source_url": "https://zakupki.gov.ru/file/1"},
                 {"file_id": 13, "source_url": "https://zakupki.gov.ru/file/3"}]
        ai = xml_events.build_xml_analysis(event=event, facts=facts, changes=changes, doc_changes=docs, files=files,
                                           link={"method": "ikz"}, rule_risk_list=xml_events.rule_risks(event, changes, facts),
                                           llm=None, previous={"id": 5, "eis_version": 0},
                                           pipeline={"status": "completed"}, model=None)
        self.assertEqual(ai["event"]["previous_xml_id"], 5)
        self.assertIn("НМЦК", ai["resume"])
        self.assertEqual(ai["documents"]["replaced"][0]["before_file_id"], 11)
        self.assertEqual(ai["documents"]["replaced"][0]["after_file_id"], 13)
        self.assertEqual(ai["link"]["method"], "ikz")
        self.assertIn("pipeline", ai)
        payload = xml_events.build_llm_payload(event, facts, changes, docs, files)
        self.assertLessEqual(len(payload["changes"]), xml_events.MAX_CHANGES_LLM)

    def test_llm_risks_filtered_by_catalog(self):
        """Риски модели вне каталога отбрасываются, уровни приводятся к 0–1."""
        out = xml_events.to_external_event_risks([
            {"rule_id": "CTR-001", "level": 70, "confidence": 0.9, "description": "d"},
            {"rule_id": "XXX-1", "level": 0.5, "confidence": 0.9},
        ])
        self.assertEqual([r["rule_id"] for r in out], ["CTR-001"])
        self.assertEqual(out[0]["level"], 0.7)


class CatalogTests(unittest.TestCase):
    """Согласованность каталога рисков и событий."""

    def test_codes_known(self):
        """Все разрешённые коды есть в каталоге, все события тегов существуют."""
        for code in risk_catalog.DOCUMENT_RISK_CODES + risk_catalog.EVENT_RISK_CODES + risk_catalog.VERSION_RISK_CODES:
            self.assertIn(code, risk_catalog.RISKS)
        for tag, code in risk_catalog.TAG_EVENTS.items():
            self.assertIn(code, risk_catalog.EVENTS, tag)

    def test_event_and_risk_codes_do_not_collide(self):
        """Коды событий не совпадают с кодами рисков (в концепции оба набора называются CTR-*)."""
        self.assertFalse(set(risk_catalog.EVENTS) & set(risk_catalog.RISKS))

    def test_prompt_placeholders(self):
        """Промпты и схемы содержат маркеры, которые подставляет код."""
        root = Path(__file__).resolve().parent.parent
        self.assertIn("RISK_CATALOG", (root / "prompts/risk_analysis_prompt.txt").read_text(encoding="utf-8"))
        self.assertIn('"RISK_CODES"', (root / "schemas/risk_analysis_schema.json").read_text(encoding="utf-8"))
        self.assertIn("EVENT_RISKS_KEYS", (root / "prompts/event_analyzer_prompt.txt").read_text(encoding="utf-8"))
        self.assertIn('"EVENT_RISKS_KEYS"', (root / "schemas/event_analyzer_schema.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
