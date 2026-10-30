"""Анализ закупки/контракта, организаций и типовые положения 223-ФЗ (без БД и модели)."""
import asyncio
import json
import unittest
from unittest import mock
from datetime import date

from risk_monitoring import contract_package as cp
from configs import settings
from risk_monitoring import org_profile, typal_clauses

DATASET = {
    "owner": {"region": "34", "zak_inn": "3437500183", "zak_kpp": "343701001", "zak_name_krat": "ГБПОУ «МППК»"},
    "supplier": {"post_inn": "3245021003", "post_kpp": "324501001", "post_name_krat": "ООО «НИКА»",
                 "post_stat": "субъект малого предпринимательства"},
    "izvejenie": {"izvech": "0354100008624000011", "nmck": "12100920", "vidz_code": "EAB20",
                  "vidz": "Электронный аукцион", "dt": "2024-11-12T17:04:17+03:00"},
    "protocol": {"app_count": 1, "single_app_flag": "true", "win_discount_pct": 10.5},
    "ispolnenie": {"contr_zena": "10830323.4"},
    "contract_details": {"num_contr": "1572000318824000011", "contr_data": "2024-12-02", "predmet": "Ремонт кровли"},
    "contract": {"okpd2_code": "43.39", "mod_reason_code": None},
    "procedure": {"term_code": None},
}
CARD = {"id": 26, "number": "0354100008624000011", "fz_type": "44", "organisation_id": 7, "dataset": json.dumps(DATASET)}


class ContractPackageTest(unittest.TestCase):
    """Сводный анализ закупки."""

    def test_rules_to_codes(self):
        """Правило внешней системы — флаг 0/1: «1» → риск каталога с этим показателем, «0» — нет."""
        risks = cp.rule_values_risks({"pz_p5": 1, "pz_p1": 0, "zc_p1": "1", "unknown": 1})
        self.assertEqual({r["rule_id"] for r in risks}, {"FIN-003", "PROC-006"})
        self.assertEqual(risks[0]["evidence"][0]["src"][:5], "rule:")

    def test_registry_window(self):
        """Реестр 19.28 действует два года с date_begin; КПП в записи сверяется, если указан."""
        black = [{"id": 1, "inn": "3245021003", "kpp": "", "date_begin": "2023-01-10"},
                 {"id": 2, "inn": "3245021003", "kpp": "999999999", "date_begin": "2024-06-01"}]
        sup = {"inn": "3245021003", "kpp": "324501001"}
        self.assertEqual([m["id"] for m in cp.registry_matches(black, sup, date(2024, 12, 2))], [1])
        self.assertEqual(cp.registry_matches(black, sup, date(2025, 1, 11)), [])

    def test_file_risks_and_analyst(self):
        """Отклонённый аналитиком риск не учитывается, подтверждённый — отмечен; заменённая редакция пропущена."""
        files = [{"id": 1, "file_name": "ТЗ.docx", "risks": json.dumps([
                     {"rule_id": "DOC-002", "level": 0.6, "confidence": 0.9, "evidence": [{"fragment": "Bosch"}]},
                     {"rule_id": "DOC-004", "level": 0.6, "confidence": 0.9}])},
                 {"id": 2, "file_name": "ТЗ_старое.docx", "superseded_by": {"file_id": 1},
                  "risks": [{"rule_id": "DOC-005", "level": 0.9, "confidence": 0.9}]}]
        risks, confirmed = cp.file_risks(files, {(1, "DOC-004"): 2, (1, "DOC-002"): 1})
        self.assertEqual([r["rule_id"] for r in risks], ["DOC-002"])
        self.assertEqual(confirmed, {"DOC-002"})
        self.assertEqual(risks[0]["evidence"][0]["src"], "file:1")

    def test_latest_xml(self):
        """Риски событий — по последней версии документа ЕИС."""
        xmls = [{"id": 10, "xml_tag": "epNotificationEF2020", "purchase_number": "1", "eis_version": 0,
                 "risks": [{"rule_id": "FIN-013", "level": 0.6, "confidence": 1}]},
                {"id": 11, "xml_tag": "epNotificationEF2020", "purchase_number": "1", "eis_version": 1, "risks": []}]
        self.assertEqual(cp.event_risks(xmls), [])

    def test_consistency(self):
        """Цена контракта в документе не совпадает с ЕИС — DOC-009; совпадает с точностью 1 % — нет."""
        p = cp.build_passport(CARD, DATASET, [])
        bad = [{"id": 5, "file_name": "контракт.pdf", "detected_type": "docContractDoWorkFiles",
                "finances": [{"value": 9000000, "context": "Цена контракта"}, {"value": 12100920, "context": "НМЦК"}]}]
        good = [dict(bad[0], finances=[{"value": 10830323.4, "context": "Цена договора"}])]
        self.assertEqual(cp.consistency(p, bad)[0]["field"], "contract_price")
        self.assertEqual(cp.consistency(p, good), [])

    def test_completeness(self):
        """Неполный комплект проверяется только у конкурентной закупки с файлами."""
        p = cp.build_passport(CARD, DATASET, [])
        comp = cp.completeness(p, [{"detected_type": "docProjContractFiles"}])
        self.assertEqual(comp["missing"], ["Описание объекта закупки", "Обоснование НМЦК"])
        self.assertFalse(cp.completeness(p, [])["checked"])
        ep = dict(p, method_code="EZTP20", method="Закупка, осуществляемая в соответствии с частью 12 статьи 93")
        self.assertEqual(cp.completeness(ep, [{"detected_type": "docProjContractFiles"}])["reason"], "noncompetitive")

    def test_build_and_score(self):
        """Сборка: одна заявка (PROC-001), правило, реестр; балл, зона, изменение к прошлому."""
        black = [{"id": 3, "inn": "3245021003", "kpp": "", "date_begin": "2024-01-10", "deal_number": "5-1/24"}]
        out = cp.build_contract_analysis(card=CARD, files=[], xmls=[], values={"pz_p5": 1}, black=black,
                                         statuses={}, previous_score=20.0, settings=settings)
        codes = {r["rule_id"] for r in out["risks"]}
        self.assertEqual(codes, {"PROC-001", "FIN-003", "REG-001"})
        self.assertEqual(out["registry"][0]["black_list_id"], 3)
        self.assertEqual(out["profile"]["zone"], "red")
        self.assertGreater(out["profile"]["delta"], 0)
        self.assertEqual(out["passport"]["stage"], "contract")
        self.assertIn("Закупка № 0354100008624000011", cp.default_resume(out["passport"], out["profile"], out["risks"], []))

    def test_favourite_supplier(self):
        """Поставщик закупки — крупнейший у заказчика (4 из 8) → BEH-006; не крупнейший или мало данных — нет."""
        out = cp.build_contract_analysis(card=CARD, files=[], xmls=[], values=None, black=[], statuses={},
                                         settings=settings, supplier_counts={"3245021003": 4, "1": 2, "2": 2})
        self.assertIn("BEH-006", {r["rule_id"] for r in out["risks"]})
        self.assertEqual(out["customer_supplier"]["share"], 0.5)
        p = out["passport"]
        self.assertFalse(cp.favourite_supplier(p, {"3245021003": 3, "1": 5}, settings)["favourite"])
        self.assertFalse(cp.favourite_supplier(p, {"3245021003": 3, "1": 1}, settings)["favourite"])
        self.assertIsNone(cp.favourite_supplier(p, None, settings))

    def test_competitive(self):
        """Ч. 12 ст. 93 (EZTP20) — неконкурентная; способ неизвестен — ``None``."""
        self.assertFalse(cp.competitive("EZTP20", ""))
        self.assertTrue(cp.competitive("EZKB20", "Запрос котировок"))
        self.assertIsNone(cp.competitive(None, ""))


def _contract(i, supplier="1111111111", customer="2222222222", **kw):
    """Строка закупки как из customer_contracts.sql."""
    base = {"id": i, "nmck": "1000", "contract_price": "900", "method_code": "EAP20", "method": "Электронный аукцион",
            "single_app_flag": "false", "app_count": 3, "win_discount_pct": 10, "contract_number": str(i),
            "contract_date": "2026-05-01", "supplier_inn": supplier, "supplier_name": "ООО " + supplier,
            "customer_inn": customer, "customer_name": "ГБУ " + customer, "risk_codes": json.dumps(["DOC-004"])}
    base.update(kw)
    return base


class OrgProfileTest(unittest.TestCase):
    """Аналитика заказчика и поставщика."""

    def test_customer_concentration_and_competition(self):
        """6 из 8 контрактов у одного поставщика, 5 из 8 с одной заявкой → BEH-006, BEH-001; повтор DOC-004 → HIST-001."""
        cs = [_contract(i, single_app_flag="true" if i < 5 else "false") for i in range(6)] + \
             [_contract(10, supplier="3333333333"), _contract(11, supplier="4444444444")]
        sec = org_profile.customer_section(cs, [], settings)
        codes = {r["rule_id"] for r in sec["risks"]}
        self.assertTrue({"BEH-006", "BEH-001", "HIST-001"} <= codes)
        self.assertEqual(sec["top_suppliers"][0]["contracts"], 6)
        self.assertEqual(sec["metrics"]["repeated_risks"], {"DOC-004": 8})

    def test_small_sample_no_behaviour(self):
        """Меньше ORG_MIN_CONTRACTS закупок — поведенческих рисков нет."""
        sec = org_profile.customer_section([_contract(1, single_app_flag="true")], [], settings)
        self.assertFalse([r for r in sec["risks"] if r["rule_id"].startswith("BEH")])

    def test_supplier_registry_and_terminations(self):
        """Поставщик в реестре сейчас → REG-001; 2 расторжения из 4 → BEH-002; «любимый поставщик» — риск заказчика, не поставщика."""
        cs = [_contract(i, term_code="1" if i < 2 else None) for i in range(4)]
        black = [{"id": 9, "inn": "1111111111", "date_begin": date.today().isoformat(), "deal_number": "1"}]
        sec = org_profile.supplier_section(cs, black, "1111111111", settings)
        codes = {r["rule_id"] for r in sec["risks"]}
        self.assertEqual(codes, {"REG-001", "BEH-002"})
        self.assertEqual(sec["registry"][0]["black_list_id"], 9)
        self.assertEqual(sec["metrics"]["top_customer_share"], 1.0)


TYPAL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ns2:typalOrderClause xmlns="http://zakupki.gov.ru/223fz/types/1" xmlns:ns2="http://zakupki.gov.ru/223fz/typalOrderClauseList/1">
 <header><guid>g</guid></header>
 <ns2:body><ns2:item><guid>i</guid><ns2:typalOrderClauseData>
  <ns2:creator><mainInfo><fullName>КОМИТЕТ</fullName><inn>3444200423</inn><region>ВОЛГОГРАДСКАЯ ОБЛАСТЬ</region></mainInfo></ns2:creator>
  <ns2:name>Типовое положение о закупке товаров, работ, услуг </ns2:name>
  <ns2:registrationNumber>1190005321</ns2:registrationNumber>
  <ns2:approveDate>2019-06-10T00:00:00</ns2:approveDate>
  <ns2:useForBaseDate>2026-03-28T00:00:00</ns2:useForBaseDate>
  <ns2:version>48</ns2:version>
  <ns2:purchaseMethod><ns2:purchaseMethodCode>405161</ns2:purchaseMethodCode><ns2:purchaseMethodName>Аукцион в электронной форме</ns2:purchaseMethodName><ns2:digitalPurchase>true</ns2:digitalPurchase></ns2:purchaseMethod>
  <ns2:customerInfo><mainInfo><fullName>ГБПОУ КОЛЛЕДЖ</fullName><inn>3437500183</inn><kpp>343701001</kpp><budgetAgencyType>SUBJECT</budgetAgencyType></mainInfo></ns2:customerInfo>
  <ns2:customerInfo><mainInfo><fullName>ГАУ ЦЕНТР</fullName><inn>3443089275</inn><kpp>344301001</kpp><autonomousAgencyType>SUBJECT</autonomousAgencyType></mainInfo></ns2:customerInfo>
  <ns2:attachments>
   <document><fileName>Приложение 3 Перечень ГАУ 28.01.2026.doc</fileName><description>Приложение 3 Перечень ГАУ</description><url>https://zakupki.gov.ru/223/clause/public/download/download.html?id=1</url><contentUid>A</contentUid></document>
   <document><fileName>ТИПОВОЕ ПОЛОЖЕНИЕ О ЗАКУПКЕ ГАУ ВО 28.01.2026.doc</fileName><description>ГАУ</description><url>https://zakupki.gov.ru/223/clause/public/download/download.html?id=2</url><contentUid>B</contentUid></document>
   <document><fileName>ТИПОВОЕ ПОЛОЖЕНИЕ О ЗАКУПКЕ ГБУ ВО 28.11.2024.doc</fileName><description>ГБУ</description><url>https://zakupki.gov.ru/223/clause/public/download/download.html?id=3</url><contentUid>C</contentUid></document>
   <document><fileName>1н 28.01.26_изменения в Приказ 9н.PDF</fileName><description>изменения</description><url>https://zakupki.gov.ru/223/clause/public/download/download.html?id=4</url><contentUid>D</contentUid></document>
  </ns2:attachments>
  <ns2:paymentPeriod>7</ns2:paymentPeriod>
 </ns2:typalOrderClauseData></ns2:item></ns2:body>
</ns2:typalOrderClause>""".encode("utf-8")


class TypalClauseTest(unittest.TestCase):
    """Разбор typalOrderClause и нарезка положения."""

    def test_parse(self):
        """Версия, даты, заказчики с типом учреждения, файлы по видам."""
        c = typal_clauses.parse_typal_xml(TYPAL_XML)[0]
        self.assertEqual((c["registration_number"], c["version"], c["use_for_base_date"]), ("1190005321", 48, "2026-03-28"))
        self.assertEqual([x["agency_kind"] for x in c["customers"]], ["budget", "autonomous"])
        self.assertEqual([(f["kind"], f["agency_kind"]) for f in c["files"]],
                         [("list", "autonomous"), ("position", "autonomous"), ("position", "budget"), ("order", None)])
        self.assertTrue(c["purchase_methods"][0]["electronic"])

    def test_position_for(self):
        """Положение заказчика — по типу учреждения; при двух положениях и неизвестном типе — None."""
        files = typal_clauses.parse_typal_xml(TYPAL_XML)[0]["files"]
        self.assertIn("ГБУ", typal_clauses.position_for(files, "budget")["file_name"])
        self.assertIn("ГАУ", typal_clauses.position_for(files, "autonomous")["file_name"])
        self.assertIsNone(typal_clauses.position_for(files, None))

    def test_not_typal(self):
        """Другой документ ЕИС — ValueError."""
        with self.assertRaises(ValueError):
            typal_clauses.parse_typal_xml(b"<contract><versionNumber>1</versionNumber></contract>")

    def test_split_points(self):
        """Чанки по пунктам с номером первого пункта, не длиннее лимита."""
        text = "ТИПОВОЕ ПОЛОЖЕНИЕ\n1. Общие положения\n1.1. Настоящее Положение разработано.\n" + \
               "9.3. Обеспечение заявки " + "условие " * 400 + "\n10.1. Требования к участникам."
        chunks = typal_clauses.split_points(text, limit=800)
        self.assertTrue(all(len(c["text"]) <= 800 for c in chunks))
        self.assertIn("9.3", [c["point"] for c in chunks])
        self.assertEqual(chunks[-1]["point"], "10.1")


class FakeAnalyzerTest(unittest.TestCase):
    """ContractAnalyzer с подменёнными запросами: запись, хранение у нас, пропуск без изменений."""

    def _analyzer(self):
        """Анализатор без внешних зависимостей."""
        try:
            from risk_monitoring.contract_analyzer import ContractAnalyzer
        except ImportError as e:
            raise unittest.SkipTest(f"нет зависимостей: {e}")
        an = ContractAnalyzer.__new__(ContractAnalyzer)
        an.env, an.force, an.push_results, an.llm, an.model, an.stats = "dev", False, True, None, None, {"llm_resume": 0}
        an.analyze_documents, an.documents, an.http_manager, an.environment, an.use_llm = False, {}, None, "dev", False
        an.patched, an.saved = [], []

        class Api:
            async def patch_ai_analysis(s, table, rid, ai):
                an.patched.append((table, rid))

        class Repo:
            async def get_rm_results(s, env, kind, ids):
                return {}

            async def save_rm_result(s, env, kind, eid, ai, pushed, err=None):
                an.saved.append((kind, eid, pushed))

        an.risk_api, an.repo = Api(), Repo()
        return an

    def test_flow(self):
        """Готовая закупка записывается; та же закупка с тем же хэшем — unchanged."""
        an = self._analyzer()
        data = {"contract_cards.sql": [dict(CARD)], "contract_files.sql": [], "contract_xml.sql": [],
                "contract_rules.sql": [{"card_id": 26, "rule_values": json.dumps({"pz_p5": 1})}],
                "black_list.sql": []}

        async def query(name, head=(), **lists):
            return [dict(r) for r in data.get(name, [])]

        this_year = date.today().year
        data["org_rows.sql"] = [{"id": 7, "inn": "3437500183", "kpp": "343701001", "year": this_year}]
        data["org_group.sql"] = [{"id": 7, "inn": "3437500183", "kpp": "343701001", "year": this_year},
                                 {"id": 8, "inn": "3437500183", "kpp": "343701001", "year": this_year - 1}]
        data["customer_supplier_counts.sql"] = [{"organisation_id": 7, "supplier_inn": "3245021003", "contracts": 3},
                                                {"organisation_id": 8, "supplier_inn": "3245021003", "contracts": 2},
                                                {"organisation_id": 8, "supplier_inn": "1", "contracts": 2}]
        an._query = query
        res = asyncio.run(an.analyze_ids([26, 27]))
        fav = res[0]["ai_analysis"]["customer_supplier"]
        self.assertEqual((fav["supplier_contracts"], fav["total"], fav["favourite"]), (5, 7, True))
        self.assertEqual([r["status"] for r in res], ["done", "not_found"])
        self.assertEqual(an.patched, [("risk_monitoring_contracts", 26)])
        self.assertEqual(an.saved, [("contract", 26, True)])
        h = res[0]["ai_analysis"]["pipeline"]["input_hash"]
        data["contract_cards.sql"] = [dict(CARD, rm_status="completed", prev_hash=h,
                                           prev_score=res[0]["ai_analysis"]["profile"]["score"])]
        self.assertEqual(asyncio.run(an.analyze_ids([26]))[0]["status"], "unchanged")


    def test_missing_documents_analyzed(self):
        """Файл и XML без результата во внешней БД и у нас анализируются (файлы — до XML), затем данные перечитываются."""
        import sys
        import types
        from unittest import mock

        an = self._analyzer()
        an.analyze_documents = True
        calls = []
        state = {"done": False}

        class FA:
            def __init__(self, *a, **k):
                self.stats = {}

            async def analyze_ids(self, ids):
                calls.append(("files", ids))
                state["done"] = True
                return [{"file_id": 5, "status": "done", "ai_analysis": {"resume": "r"}}]

        class XA:
            def __init__(self, *a, **k):
                self.stats = {}

            async def analyze_ids(self, ids):
                calls.append(("xml", ids, dict(self.file_results)))
                return [{"xml_id": 9, "status": "done"}]

        async def query(name, head=(), **lists):
            st = "completed" if state["done"] else None
            return {"contract_cards.sql": [dict(CARD)],
                    "contract_files.sql": [{"id": 5, "card_id": 26, "file_name": "ТЗ.docx", "rm_status": st}],
                    "contract_xml.sql": [{"id": 9, "card_id": 26, "xml_tag": "epNotificationEF2020", "rm_status": st}]}.get(name, [])

        an._query = query
        mods = {"risk_monitoring.ai_file_analyzer": types.SimpleNamespace(AIFileAnalyzer=FA),
                "risk_monitoring.xml_event_analyzer": types.SimpleNamespace(XmlEventAnalyzer=XA)}
        with mock.patch.dict(sys.modules, mods):
            res = asyncio.run(an.analyze_ids([26]))
        self.assertEqual(calls, [("files", [5]), ("xml", [9], {5: {"resume": "r"}})])
        self.assertEqual(an.documents["files"], {"done": 1})
        self.assertEqual(res[0]["ai_analysis"]["package"]["files_analyzed"], 1)

    def test_org_flow(self):
        """OrgAnalyzer: строки организации по годам → каноническая строка, секции заказчика и поставщика."""
        try:
            from risk_monitoring.org_analyzer import OrgAnalyzer
        except ImportError as e:
            raise unittest.SkipTest(f"нет зависимостей: {e}")
        an = OrgAnalyzer.__new__(OrgAnalyzer)
        an.env, an.push_results, an.llm, an.model, an.repo = "dev", False, None, None, None
        an.roles, an.period_months, an.stats = ("customer", "supplier"), 12, {"llm_resume": 0}
        this_year = date.today().year
        orgs = [{"id": 1, "year": this_year - 1, "inn": "2222222222", "kpp": "1", "name": "ГБУ", "monitoring": 1},
                {"id": 2, "year": this_year, "inn": "2222222222", "kpp": "1", "name": "ГБУ", "monitoring": 1}]
        cust = [dict(_contract(i, customer="2222222222"), organisation_id=1 if i % 2 else 2) for i in range(6)]
        sup = [_contract(20, supplier="2222222222")]

        async def query(name, head=(), **lists):
            return {"org_rows.sql": [o for o in orgs if o["id"] in lists.get("ids", [])], "org_group.sql": orgs,
                    "customer_contracts.sql": cust, "supplier_contracts.sql": sup}.get(name, [])

        an._query = query
        res = asyncio.run(an.analyze_ids([1, 2, 3]))
        self.assertEqual([r.get("canonical_id") for r in res], [2, 2, None])
        self.assertEqual(res[2]["status"], "not_found")
        ai = res[1]["ai_analysis"]
        self.assertEqual(ai["customer"]["metrics"]["contracts"], 6)
        self.assertEqual(ai["supplier"]["metrics"]["contracts"], 1)
        self.assertTrue(ai["customer"]["resume"])


if __name__ == "__main__":
    unittest.main()


class ScoreCalibrationTest(unittest.TestCase):
    """Балл с затуханием и весом непроверенных рисков модели (закупка 1387, 20.10.2026: было 99,6)."""

    def test_many_medium_risks_not_red(self):
        """5 рисков модели по 0,6×0,85 и 4 правила по 0,4 → жёлтая зона, не 99,6."""
        ai = [{"rule_id": c, "level": 0.6, "confidence": 0.85, "sources": ["ai_file"]}
              for c in ("DOC-006", "DOC-007", "DOC-005", "DOC-010", "DOC-003")]
        rules = [{"rule_id": c, "level": 0.4, "confidence": 1.0, "sources": ["rule"]}
                 for c in ("FIN-004", "FIN-006", "FIN-008", "CTR-002")]
        prof = cp.profile(ai + rules, decay=settings.SCORE_DECAY, ai_weight=settings.AI_UNCONFIRMED_WEIGHT)
        self.assertEqual(prof["zone"], "yellow")
        self.assertLess(prof["score"], 70)

    def test_single_violation_red(self):
        """Один серьёзный подтверждённый признак (реестр 19.28, 0,9) — красная зона."""
        prof = cp.profile([{"rule_id": "REG-001", "level": 0.9, "confidence": 1.0, "sources": ["registry"]}])
        self.assertEqual(prof["zone"], "red")


class RuleCoveredTest(unittest.TestCase):
    """Риски модели по кодам с правилом внешней системы уступают правилу (закупка 1399, 20.10.2026)."""

    def test_drop_rule_covered(self):
        """pz_p2 = 0 → FIN-005 модели снят; pz_p3 = 1 → FIN-006 модели остаётся подтверждением (уровень правила);
        DOC-003 и подтверждённый аналитиком — как есть."""
        risks = [{"rule_id": "FIN-005", "level": 0.7}, {"rule_id": "FIN-006", "level": 0.7, "description": "нет"},
                 {"rule_id": "DOC-003", "level": 0.5}]
        out = cp.drop_rule_covered(risks, {"pz_p2": 0, "pz_p3": 1})
        self.assertEqual([r["rule_id"] for r in out], ["FIN-006", "DOC-003"])
        self.assertEqual((out[0]["level"], out[0]["corroborates_rule"]), (0.0, "pz_p3"))
        self.assertEqual(len(cp.drop_rule_covered(risks, {"pz_p2": 0}, confirmed={"FIN-005"})), 3)
        self.assertEqual(len(cp.drop_rule_covered(risks, None)), 3)

    def test_rule_decision_by_law(self):
        """Правила написаны для 44-ФЗ: при 223-ФЗ и для показателя «на проверке» правило не решает — находка ИИ стоит."""
        self.assertEqual(cp.rule_decision("FIN-005", {"pz_p2": 0}, 44), "not_fired")
        self.assertEqual(cp.rule_decision("FIN-005", {"pz_p2": "1"}, "44"), "fired")
        self.assertIsNone(cp.rule_decision("FIN-005", {"pz_p2": 0}, 223))
        self.assertIsNone(cp.rule_decision("FIN-005", {"pz_p2": 0}, 44, suspect=("pz_p2",)))
        self.assertIsNone(cp.rule_decision("DOC-003", {"pz_p2": 0}, 44))
        kept = cp.drop_rule_covered([{"rule_id": "FIN-005", "level": 0.7}], {"pz_p2": 0}, fz=223)
        self.assertEqual(kept[0]["level"], 0.7)

    def test_merge_corroboration(self):
        """Правило + модель: один риск, уровень правила, цитата модели в доказательствах, источник «rule»."""
        from risk_monitoring import passport
        rule = {"rule_id": "FIN-006", "level": 0.2, "confidence": 1.0, "source": "rule", "description": "Сработал pz_p3",
                "evidence": [{"src": "rule:pz_p3", "value": 1}]}
        ai = cp.drop_rule_covered([{"rule_id": "FIN-006", "level": 0.8, "confidence": 0.9, "source": "ai_file",
                                    "evidence": [{"src": "file:7", "fragment": "обеспечение не устанавливается"}]}],
                                  {"pz_p3": 1}, fz=44)
        with mock.patch.object(settings, "RISK_SOURCE_LABELS", "simple"):
            (m,) = passport.merge_risks([rule], ai)
        self.assertEqual((m["level"], m["source"], m["description"]), (0.2, "rule", "Сработал pz_p3"))
        self.assertTrue(m["corroborated_by_ai"])
        self.assertEqual(len(m["evidence"]), 2)


class CustomerQualityTest(unittest.TestCase):
    """Сводный показатель заказчика по «Модели сводной аналитики»: Псвод = Σ (Σ k·П) × 100 × Кблок."""

    def _c(self, i, codes=(), levels=(), score=10, **kw):
        """Закупка с рисками ``codes`` уровней ``levels``."""
        return _contract(i, score=score, risk_codes=json.dumps(list(codes)), risk_levels=json.dumps(list(levels)),
                         risk_confidences=json.dumps([1.0] * len(codes)),
                         risk_sources=json.dumps([["rule"]] * len(codes)), **kw)

    def test_clean_customer_is_green(self):
        """Нет рисков — 100, зелёная зона, балл риска 0."""
        sec = org_profile.customer_section([self._c(i) for i in range(3)], [], settings)
        self.assertEqual(sec["quality_index"], 100.0)
        self.assertEqual((sec["score"], sec["zone"]), (0.0, "green"))

    def test_red_contract_lowers_index(self):
        """Закупка в красной зоне (прогон 02.07.2026, филиал СПбГЭУ) снижает показатель; расчёт по блокам виден."""
        cs = [self._c(1, ("DOC-002", "CTR-005", "CTR-014"), (0.8, 0.7, 0.7), score=71.2)]
        sec = org_profile.customer_section(cs, [], settings)
        b = sec["score_model"]["blocks"]
        self.assertLess(sec["quality_index"], 85)
        self.assertLess(b["conclusion"]["quality"], 100)
        self.assertLess(b["execution"]["quality"], 100)
        self.assertEqual(b["acceptance"]["quality"], 100)
        p22 = next(x for x in b["execution"]["indicators"] if x["code"] == "P22")
        self.assertAlmostEqual(p22["value"], 0.3, places=3)
        # Псвод = 0,2·Пзак + 0,5·Пис + 0,3·Пприем
        expect = round(0.2 * b["conclusion"]["quality"] + 0.5 * b["execution"]["quality"] + 0.3 * 100, 1)
        self.assertAlmostEqual(sec["quality_index"], expect, delta=0.11)

    def test_no_contract_blocks_not_rated(self):
        """Без заключённых контрактов реализация и приёмка не оцениваются — показатель по заключению."""
        cs = [self._c(1, ("DOC-002",), (0.5,), contract_number=None, contract_price=None)]
        m = org_profile.customer_section(cs, [], settings)["score_model"]
        self.assertEqual(m["rated_blocks"], ["conclusion"])
        self.assertEqual(m["quality_index"], m["blocks"]["conclusion"]["quality"])

    def test_violation_weighs_more(self):
        """Нарушение весит больше риска того же уровня."""
        from risk_monitoring import risk_catalog
        code = next(k for k, v in risk_catalog.RISKS.items() if v.get("kind") == "violation")
        risk = org_profile._stage_weight({"rule_id": "CTR-005", "level": 0.5, "confidence": 1.0}, settings)
        self.assertGreater(org_profile._stage_weight({"rule_id": code, "level": 0.5, "confidence": 1.0}, settings), risk)

    def test_mapping_valid(self):
        """Коды показателей есть в каталоге, не повторяются; веса показателей блока в сумме 1, блоков — 1."""
        from risk_monitoring import risk_catalog
        codes = [c for b in settings.CUSTOMER_QUALITY_BLOCKS for *_, cs in b["indicators"] for c in cs]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertTrue(all(c in risk_catalog.RISKS for c in codes))
        for b in settings.CUSTOMER_QUALITY_BLOCKS:
            self.assertAlmostEqual(sum(k for _, _, k, _ in b["indicators"]), 1.0)
        self.assertAlmostEqual(sum(b["weight"] for b in settings.CUSTOMER_QUALITY_BLOCKS), 1.0)

    def test_no_analyzed(self):
        """Нет проанализированных закупок — показатель не считается, балл прежний (поведенческий)."""
        sec = org_profile.customer_section([_contract(i) for i in range(3)], [], settings)
        self.assertIsNone(sec["score_model"]["quality_index"])
        self.assertNotIn("quality_index", sec)


class RuleExclusionsTest(unittest.TestCase):
    """Перепроверка сработавших показателей по документам: исключение понижает риск, но не снимает его."""

    def setUp(self):
        """Справочник и тексты закупки."""
        from risk_monitoring import rule_exclusions as rx
        self.rx = rx
        self.texts = {7: "5. Порядок оплаты.\nАвансовые платежи подлежат казначейскому сопровождению в соответствии "
                         "со ст. 242.23 БК РФ.\n\n6. Гарантии. Гарантийный срок 12 месяцев."}

    def test_fired_and_fragments(self):
        """Берутся только сработавшие показатели из справочника; фрагменты — абзацы с ключевыми словами."""
        self.assertEqual(self.rx.fired_with_exclusions({"pz_p4": 1, "pz_p5": 0, "pz_p6": 1}), ["pz_p4"])
        self.assertEqual(self.rx.fired_with_exclusions({"pz_p4": 1}, suspect=("pz_p4",)), [])
        frags = self.rx.fragments(self.texts, {7: "Проект контракта.docx"}, ["казначейск"], 5000)
        self.assertEqual(len(frags), 1)
        self.assertIn("казначейскому сопровождению", frags[0]["text"])

    def test_verify_and_apply(self):
        """Исключение с цитатой из текста — уровень × 0,3; выдуманная цитата — unclear, риск как есть."""
        cfg = self.rx.load_config()
        checks = self.rx.verify([{"indicator": "pz_p4", "status": "excluded", "exclusion_id": "treasury",
                                  "reason": "казначейское сопровождение", "file_id": 7,
                                  "quote": "Авансовые платежи подлежат казначейскому сопровождению"},
                                 {"indicator": "pz_p5", "status": "excluded", "reason": "x", "quote": "нет такого"}],
                                ["pz_p4", "pz_p5"], self.texts, cfg)
        self.assertEqual(checks["pz_p4"]["status"], "excluded")
        self.assertEqual(checks["pz_p4"]["evidence"]["file_id"], 7)
        self.assertEqual(checks["pz_p5"]["status"], "unclear")
        risks = [{"rule_id": "FIN-008", "level": 0.2, "confidence": 1.0, "evidence": [{"src": "rule:pz_p4"}]},
                 {"rule_id": "FIN-003", "level": 0.2, "confidence": 1.0, "evidence": [{"src": "rule:pz_p5"}]}]
        self.assertEqual(self.rx.apply_checks(risks, checks, settings), 1)
        self.assertAlmostEqual(risks[0]["level"], 0.06)
        self.assertEqual(risks[0]["confidence"], settings.RULE_EXCLUDED_CONFIDENCE)
        self.assertEqual(risks[0]["rule_check"]["status"], "excluded")
        self.assertEqual((risks[1]["level"], risks[1]["rule_check"]["status"]), (0.2, "unclear"))

    def test_check_rules_one_request(self):
        """Один запрос к модели на закупку по всем сработавшим показателям с исключениями."""
        calls = []

        class FakeLLM:
            """Модель с фиксированным ответом."""

            async def ask(self, system, user, schema=None, max_tokens=0, list_key=None):
                """Ответ по pz_p4."""
                calls.append(user)
                return {"checks": [{"indicator": "pz_p4", "status": "confirmed", "reason": "сопровождения нет"}]}

        out = asyncio.run(self.rx.check_rules(FakeLLM(), {"pz_p4": 1, "pz_p3": 1}, self.texts, {7: "Контракт"}, settings))
        self.assertEqual(len(calls), 1)
        self.assertEqual(out["pz_p4"]["status"], "confirmed")
        self.assertEqual(out["pz_p3"]["status"], "unclear")
