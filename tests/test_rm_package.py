"""Анализ закупки/контракта, организаций и типовые положения 223-ФЗ (без БД и модели)."""
import asyncio
import json
import unittest
from datetime import date

from risk_monitoring import contract_package as cp
from risk_monitoring import org_profile, settings, typal_clauses

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
        self.assertEqual(out["registry"]["matches"][0]["black_list_id"], 3)
        self.assertEqual(out["profile"]["zone"], "red")
        self.assertGreater(out["profile"]["delta"], 0)
        self.assertEqual(out["passport"]["stage"], "contract")
        self.assertIn("Закупка № 0354100008624000011", cp.default_resume(out["passport"], out["profile"], out["risks"], []))

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
        """6 из 8 контрактов у одного поставщика, 5 из 8 с одной заявкой → BEH-005, BEH-001; повтор DOC-004 → HIST-001."""
        cs = [_contract(i, single_app_flag="true" if i < 5 else "false") for i in range(6)] + \
             [_contract(10, supplier="3333333333"), _contract(11, supplier="4444444444")]
        sec = org_profile.customer_section(cs, [], settings)
        codes = {r["rule_id"] for r in sec["risks"]}
        self.assertTrue({"BEH-005", "BEH-001", "HIST-001"} <= codes)
        self.assertEqual(sec["top_suppliers"][0]["contracts"], 6)
        self.assertEqual(sec["metrics"]["repeated_risks"], {"DOC-004": 8})

    def test_small_sample_no_behaviour(self):
        """Меньше ORG_MIN_CONTRACTS закупок — поведенческих рисков нет."""
        sec = org_profile.customer_section([_contract(1, single_app_flag="true")], [], settings)
        self.assertFalse([r for r in sec["risks"] if r["rule_id"].startswith("BEH")])

    def test_supplier_registry_and_terminations(self):
        """Поставщик в реестре сейчас → REG-001; 2 расторжения из 4 → BEH-002; концентрация выключена настройкой."""
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

        an._query = query
        res = asyncio.run(an.analyze_ids([26, 27]))
        self.assertEqual([r["status"] for r in res], ["done", "not_found"])
        self.assertEqual(an.patched, [("risk_monitoring_contracts", 26)])
        self.assertEqual(an.saved, [("contract", 26, True)])
        h = res[0]["ai_analysis"]["pipeline"]["input_hash"]
        data["contract_cards.sql"] = [dict(CARD, rm_status="completed", prev_hash=h,
                                           prev_score=res[0]["ai_analysis"]["profile"]["score"])]
        self.assertEqual(asyncio.run(an.analyze_ids([26]))[0]["status"], "unchanged")


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
