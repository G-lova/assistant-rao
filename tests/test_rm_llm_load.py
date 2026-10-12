"""Тесты снижения числа вызовов модели в риск-мониторинге: события XML (значимые изменения, пакет на
закупку), печатные формы без модели, анализ только изменений новой редакции документа.
"""
import asyncio
import json
import types
import unittest

from risk_monitoring import xml_events


def ch(path, old="a", new="b"):
    """Изменение в формате ``xml_events.diff``."""
    return {"path": path, "label": xml_events.label_for(path), "old": old, "new": new, "change": "modified"}


class EventDecisionTest(unittest.TestCase):
    """``llm_decision``: модель только для значимых изменений, важных событий и рисков правил."""

    SERVICE = [
        ch("commonInfo.plannedPublishDate"),
        ch("attachmentsInfo.attachmentInfo[fileName=ТЗ.docx].url"),
        ch("notificationInfo.customerRequirementsInfo.customerRequirementInfo.applicationGuarantee.account.creditOrgName"),
        ch("notificationInfo.customerRequirementsInfo.customerRequirementInfo.contractGuarantee.account.corrAccountNumber"),
    ]

    def test_service_only(self):
        """Ссылки вложений, реквизиты счетов, даты публикации — шаблон без модели."""
        event = {"code": "PUR-002", "importance": "medium", "first_version": False}
        self.assertEqual(xml_events.llm_decision(event, self.SERVICE, []), (False, "service_changes_only"))

    def test_significant(self):
        """Перенос окончания подачи заявок, рабочие дни вместо календарных, новые требования — модель."""
        event = {"code": "PUR-002", "importance": "medium", "first_version": False}
        for path in ("notificationInfo.procedureInfo.collectingInfo.endDT",
                     "notificationInfo.customerRequirementsInfo.customerRequirementInfo.contractConditionsInfo"
                     ".contractExecutionPaymentPlan.contractExecutionTermsInfo.relativeTermsInfo.termDayType",
                     "notificationInfo.requirementsInfo.requirementInfo[#1a2b].preferenseRequirementInfo.shortName"):
            self.assertEqual(xml_events.llm_decision(event, self.SERVICE + [ch(path)], []),
                             (True, "significant_changes"), path)

    def test_first_version_and_rules(self):
        """Первая версия — шаблон; риск правил или важное событие — модель."""
        first = {"code": "CON-001", "importance": "medium", "first_version": True}
        self.assertEqual(xml_events.llm_decision(first, [], []), (False, "first_version_template"))
        self.assertTrue(xml_events.llm_decision(first, [], [{"rule_id": "FIN-013"}])[0])
        self.assertTrue(xml_events.llm_decision({"importance": "critical"}, [], [])[0])

    def test_payload_has_significant_first(self):
        """В пакет для модели идут значимые изменения, служебные — нет."""
        sig = ch("notificationInfo.procedureInfo.collectingInfo.endDT")
        payload = xml_events.build_llm_payload({"code": "PUR-002"}, {}, self.SERVICE + [sig], {}, [])
        self.assertEqual([c["path"] for c in payload["changes"]], [sig["path"]])


class FakeLLM:
    """Модель-заглушка: отвечает на пакет событий, считает вызовы; может «потерять» событие."""

    def __init__(self, drop=None):
        """``drop`` — event_id, который модель пропустит в пакетном ответе."""
        self.calls = []
        self.drop = drop

    async def create(self, **kw):
        """Ответ в формате ``chat.completions``."""
        self.calls.append(kw)
        user = kw["messages"][1]["content"]
        body = json.loads(user.split("\n\n", 1)[1])
        if "events" in body:
            events = [{"event_id": e["event_id"], "event_summary": f"соб {e['event_id']}", "significance": 0.5,
                       "confidence": 0.9, "risks": [{"rule_id": "CTR-005", "title": "", "description": "срок",
                                                     "level": 0.5, "confidence": 0.9, "verification_needed": []}]}
                      for e in body["events"] if e["event_id"] != self.drop]
            text = json.dumps({"events": events})
        else:
            text = json.dumps({"event_summary": "одно", "significance": 0.4, "confidence": 0.8,
                               "risks": [{"rule_id": "XXX-999"}]})
        msg = types.SimpleNamespace(content=text)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def event_analyzer(fake):
    """``EventAnalyzer`` с моделью-заглушкой (ответ без потока — ``call_llm`` принимает и такой)."""
    from risk_monitoring.event_analyzer import EventAnalyzer

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=fake.create)))
    return EventAnalyzer(client, "m")


class EventBatchTest(unittest.TestCase):
    """Пакетный анализ событий одной закупки."""

    def test_batch_one_call(self):
        """Три события одной закупки — один запрос; лишние коды рисков отбрасываются."""
        fake = FakeLLM()
        ea = event_analyzer(fake)
        items = [{"event_id": i, "payload": {"event": {"code": "CON-008"}, "changes": []}} for i in (11, 12, 13)]
        out = asyncio.run(ea.analyze_batch(items, "0373"))
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(sorted(out), [11, 12, 13])
        self.assertEqual(out[12]["risks"][0]["rule_id"], "CTR-005")
        self.assertIn("event_id", json.dumps(fake.calls[0]["extra_body"]["guided_json"]))

    def test_missing_event_fallback(self):
        """Событие, пропущенное в пакетном ответе, разбирается отдельным запросом."""
        fake = FakeLLM(drop=12)
        ea = event_analyzer(fake)
        items = [{"event_id": i, "payload": {"n": i}} for i in (11, 12)]
        out = asyncio.run(ea.analyze_batch(items, "0373"))
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(out[12]["event_summary"], "одно")
        self.assertEqual(out[12]["risks"], [])

    def test_batches_split(self):
        """Пакеты не больше BATCH_MAX_EVENTS событий."""
        from risk_monitoring.event_analyzer import BATCH_MAX_EVENTS, EventAnalyzer

        items = [{"event_id": i, "payload": {"x": i}} for i in range(BATCH_MAX_EVENTS + 2)]
        self.assertEqual([len(b) for b in EventAnalyzer.batches(items)], [BATCH_MAX_EVENTS, 2])

    def test_group_by_purchase(self):
        """``XmlEventAnalyzer._llm_by_purchase``: две закупки — два запроса на 4 события."""
        from risk_monitoring.xml_event_analyzer import XmlEventAnalyzer

        fake = FakeLLM()
        xa = XmlEventAnalyzer.__new__(XmlEventAnalyzer)
        xa.event_analyzer = event_analyzer(fake)
        ctxs = [{"xml_id": i, "row": {"risk_monitoring_contract_id": c, "eis_version": v}, "payload": {"i": i}}
                for i, c, v in ((1, 7, 1), (2, 7, 2), (3, 8, 0), (4, 8, 1))]
        out = asyncio.run(xa._llm_by_purchase(ctxs))
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(sorted(out), [1, 2, 3, 4])


class FileModesTest(unittest.TestCase):
    """``AIFileAnalyzer.analyze_file``: сколько запросов к модели в режимах full / incremental / print_form."""

    BASE = "\n".join(f"Пункт {i}. Поставщик обязан выполнить обязательство номер {i} в установленный контрактом срок."
                      for i in range(1, 15))

    def setUp(self):
        """Заглушки тяжёлых модулей экспертизы и анализатор с подсчётом вызовов."""
        import sys
        from unittest import mock

        utils = types.ModuleType("configs.utils")
        utils.split_large_text = lambda text, max_chunk_size=0: [text]
        tde = types.ModuleType("evaluate_documents.type_data_extractor")
        tde.ALLOWED_DOC_TYPES = ["docOpusObjectZacupFiles", "docIzvejenieFiles", "docDopMaterialsFiles"]
        tde.DOCUMENT_TYPE_MAPPING = {"docOpusObjectZacupFiles": "Описание объекта закупки", "docIzvejenieFiles": "Извещение"}
        self.patch = mock.patch.dict(sys.modules, {"configs.utils": utils, "evaluate_documents.type_data_extractor": tde})
        self.patch.start()

        from risk_monitoring.ai_file_analyzer import AIFileAnalyzer
        from risk_monitoring.document_risk_analyzer import DocumentRiskAnalyzer
        from risk_monitoring.version_diff import DocumentDiffAnalyzer

        self.calls = {"extract": 0, "type": 0, "profile": 0, "risk": [], "diff": 0}
        test = self

        async def risk_create(**kw):
            """Модель рисков: находит «штраф 50 %» и «КлериГель Супер», если они есть в переданном тексте."""
            text = kw["messages"][1]["content"]
            test.calls["risk"].append(text)
            risks = []
            for frag, code in (("штраф в размере 50 %", "CTR-002"), ("КлериГель Супер", "DOC-002"),
                               ("обязательство номер 3", "DOC-006")):
                if frag in text:
                    risks.append({"code": code, "title": "т", "severity": "medium", "explanation": "признак риска",
                                  "evidence": {"fragment": frag, "confidence": 0.9}, "verification_needed": []})
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(
                content=json.dumps({"summary": "с", "risks": risks})))])

        rclient = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=risk_create)))
        an = AIFileAnalyzer.__new__(AIFileAnalyzer)
        an.env, an.storage_path, an.force, an.recompute, an.push_results = "dev", "", False, False, False
        an.model, an.extraction_model = "big", "vl"
        an.risk_analyzer = DocumentRiskAnalyzer(rclient, "big")
        an.differ = DocumentDiffAnalyzer(rclient, "big")
        an.versions = {"doc_type": "t1", "data_extraction": "e1", "document_profile": "p1",
                       "risk_analysis": an.risk_analyzer.prompt_version, "version_diff": "d1"}

        class Ext:
            async def extract_data_from_document(self, chunks, name):
                test.calls["extract"] += 1
                return {"raw_data": {"summary": "кратко", "dates": []}}

        class Typ:
            async def detect_document_type(self, content, filename):
                test.calls["type"] += 1
                return {"detected_type": "docOpusObjectZacupFiles", "confidence": 0.95}

            def detect_document_type_by_name(self, name):
                return None

        class Prof:
            async def profile(self, *a, **k):
                test.calls["profile"] += 1
                return {"resume": "паспорт", "confidence": 0.8}

        async def differ(diff, name, doc_type):
            test.calls["diff"] += 1
            return {"summary": "изменён пункт", "changes": [], "risk_relevant": True, "significance": 0.4}

        an.differ.analyze = differ
        an.extractor, an.type_detector, an.profiler = Ext(), Typ(), Prof()
        self.store = {}

        class Repo:
            async def get_analysis(self, doc, kind, ver, model):
                r = test.store.get((doc, kind, ver, model))
                return {"result": r} if r is not None else None

            async def save_analysis(self, doc, kind, ver, model, result, compact, conf):
                test.store[(doc, kind, ver, model)] = result

            async def get_link(self, env, file_id):
                return {"document_id": 1, "content_sha256": "old"} if file_id == 10 else None

            async def get_document(self, doc_id):
                return {"text": test.prev_text}

        an.repo = Repo()
        self.text = self.BASE

        async def load(file_id, url, name):
            return 2, test.text, "new", 100

        async def no(*a, **k):
            return False

        async def fin(file_id, doc_id, ai):
            return ai

        an._load_document, an._ensure_embeddings, an._finalize = load, no, fin
        self.an = an

    def tearDown(self):
        """Снимает заглушки модулей."""
        self.patch.stop()

    def row(self, name="ТЗ.docx"):
        """Строка files_meta."""
        return {"id": 20, "file_name": name, "url": "u", "owner_type": "notice", "fz_type": 44, "eis_version": 1}

    def test_full(self):
        """Новый документ без прошлой редакции: извлечение, тип, паспорт, риски — по одному запросу."""
        ai = asyncio.run(self.an.analyze_file(self.row(), None))
        c = self.calls
        self.assertEqual((c["extract"], c["type"], c["profile"], len(c["risk"]), c["diff"]), (1, 1, 1, 1, 0))
        self.assertEqual(ai["pipeline"]["mode"], "full")

    def test_incremental(self):
        """Новая редакция (схожесть ≥ 0,9): данные, тип, паспорт — из прошлой; риски — только в изменённом абзаце."""
        from risk_monitoring import doc_kind
        from risk_monitoring.llm_json import prompt_version

        self.prev_text = self.BASE + "\nПункт 15. Неустойка начисляется по ставке ЦБ."
        self.text = self.BASE.replace("Пункт 7.", "Пункт 7. Взимается штраф в размере 50 % цены.") + \
            "\nПункт 15. Неустойка начисляется по ставке ЦБ."
        allowed = doc_kind.allowed_codes("docOpusObjectZacupFiles")
        rv = f"{self.an.versions['risk_analysis']}:{prompt_version(allowed)}"
        old_risk = {"code": "DOC-006", "category": "DOC", "severity": "medium", "title": "т", "explanation": "э",
                    "evidence": {"fragment": "обязательство номер 3", "confidence": 0.9}}
        gone_risk = {"code": "AI-003", "category": "AI", "severity": "low", "title": "т", "explanation": "э",
                     "evidence": {"fragment": "фраза, которой больше нет", "confidence": 0.9}}
        self.store.update({
            (1, "data_extraction", "e1", "vl"): {"raw_data": {"summary": "прошлая", "dates": [
                {"value": "2026-01-01", "context": "срок", "evidence": {"fragment": "фраза, которой больше нет"}}]}},
            (1, "doc_type", "t1", "vl"): {"detected_type": "docOpusObjectZacupFiles", "confidence": 0.95},
            (1, "document_profile", "p1", "big"): {"resume": "паспорт прошлой", "confidence": 0.8},
            (1, "risk_analysis", rv, "big"): {"risks": [old_risk, gone_risk], "risk_score": 10},
        })
        prev = {"id": 10, "eis_version": 0, "content_sha256": "old", "match": "replaced", "rule_ids": '["DOC-006","AI-003"]'}
        ai = asyncio.run(self.an.analyze_file(self.row(), prev))
        c = self.calls
        self.assertEqual((c["extract"], c["type"], c["profile"], c["diff"]), (0, 0, 0, 1))
        self.assertEqual(len(c["risk"]), 1)
        self.assertIn("штраф в размере 50 %", c["risk"][0])
        self.assertNotIn("Пункт 12.", c["risk"][0])                       # неизменённые абзацы не отправляются
        self.assertEqual(ai["pipeline"]["mode"], "incremental")
        codes = sorted(r["rule_id"] for r in ai["risks"] if "ai_file" in r.get("sources", []))
        self.assertEqual(codes, ["CTR-002", "DOC-006"])                   # перенесён + найден; AI-003 снят
        self.assertEqual(ai["raw_data"]["dates"], [])                     # устаревшая дата снята
        self.assertEqual(ai["version_changes"]["matched_by"], "replaced")

    def test_print_form(self):
        """Печатная форма извещения: без извлечения, типа и паспорта моделью; в модель — только строки с брендом."""
        self.text = "\n".join([
            "Печатная форма", "Извещение о проведении запроса котировок в электронной форме для закупки №0372100039726000019",
            "Общая информация", "Номер извещения", "0372100039726000019", "Наименование объекта закупки",
            "Поставка реактивов для нужд лаборатории", "Информация о процедуре закупки",
            "Дата и время окончания срока подачи заявок", "25.05.2026 10:00", "Условия контракта",
            "Начальная (максимальная) цена контракта", "1026336.64 РОССИЙСКИЙ РУБЛЬ", "Объект закупки",
            "Питательная среда для выращивания микроорганизмов КлериГель Супер", "Агар бактериологический",
            "Молекулярный вес, г/моль", "≥ 203"] + [f"Строка описания требований номер {i} к участникам закупки"
                                                     for i in range(10)])
        ai = asyncio.run(self.an.analyze_file(self.row("print_form_notice.html"), None))
        c = self.calls
        self.assertEqual((c["extract"], c["type"], c["profile"]), (0, 0, 0))
        self.assertEqual(len(c["risk"]), 1)
        self.assertIn("КлериГель Супер", c["risk"][0])
        self.assertNotIn("Агар", c["risk"][0])
        self.assertEqual([r["rule_id"] for r in ai["risks"]], ["DOC-002"])
        self.assertEqual(ai["pipeline"]["mode"], "print_form")
        self.assertEqual(ai["raw_data"]["finances"][0]["value"], 1026336.64)


if __name__ == "__main__":
    unittest.main()
