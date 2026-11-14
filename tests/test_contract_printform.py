"""Паспорт печатной формы контракта по XML контракта (``risk_monitoring.contract_printform``).

Печатная форма электронного контракта раньше разбиралась правилами извещения: номер, даты и суммы контракта
не находились (файл 29759, stage). Теперь, если XML контракта прочитан, паспорт строится из него.

XML ниже — синтетический, со структурой реального ``contract`` 44-ФЗ (схема 16.2): ``ns3:export/ns3:contract``,
несколько позиций (``products/product`` — список), поставщик-ИП, аванс, банковская гарантия, место поставки по ГАР.
"""
import asyncio
import json
import unittest

CONTRACT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ns3:export xmlns="http://zakupki.gov.ru/oos/types/1" xmlns:ns3="http://zakupki.gov.ru/oos/export/1">
  <ns3:contract schemeVersion="16.2">
    <id>1</id>
    <placementDate>2026-07-09T10:00:00+03:00</placementDate>
    <publishDate>2026-07-09T10:05:00+03:00</publishDate>
    <versionNumber>0</versionNumber>
    <foundation><fcsOrder><order>
      <notificationNumber>0123100000126000001</notificationNumber>
      <placingSingleCustomerText>Запрос котировок в электронной форме</placingSingleCustomerText>
      <purchaseCode>261234567890123400100100010010000244</purchaseCode>
    </order></fcsOrder></foundation>
    <customer><regNum>01231000001</regNum><fullName>ГБУ "ТЕСТОВЫЙ ЗАКАЗЧИК"</fullName>
      <inn>1234567890</inn><kpp>123401001</kpp></customer>
    <protocolDate>2026-07-06</protocolDate>
    <signDate>2026-07-09</signDate>
    <regNum>1123456789026000001</regNum>
    <number>0123100000126000001/1-ТЗ</number>
    <contractSubject>Поставка и монтаж стеклопакетов</contractSubject>
    <priceInfo><price>1000.00</price><currency><code>RUB</code><name>Российский рубль</name></currency></priceInfo>
    <advancePaymentSum><sumInPercents>30</sumInPercents><priceValue>300.00</priceValue></advancePaymentSum>
    <executionPeriod><startDate>2026-07-09</startDate><endDate>2026-09-30</endDate></executionPeriod>
    <enforcement><bankGuarantee><guaranteeAmount>300.00</guaranteeAmount></bankGuarantee></enforcement>
    <qualityGuaranteeInfo><providedPeriod><otherPeriodText>Гарантия 12 месяцев</otherPeriodText></providedPeriod>
    </qualityGuaranteeInfo>
    <deliveryPlaceInfo><byGARInfo><GARInfo><GARAddress>г. Тестоград, ул. Первая, д. 1</GARAddress></GARInfo>
      <deliveryPlace>корпус 2</deliveryPlace></byGARInfo></deliveryPlaceInfo>
    <products>
      <product><indexNum>1</indexNum><OKPD2><code>23.12.13.310</code></OKPD2><name>Стеклопакет</name>
        <type>PRODUCT</type><OKEI><code>055</code></OKEI><price>6.00</price><quantity>100</quantity><sum>600.00</sum>
      </product>
      <product><indexNum>2</indexNum><KTRU><code>43.32.10.110-00000001</code></KTRU><name>Монтаж</name>
        <type>WORK</type><OKEI><code>796</code></OKEI><price>40.00</price><quantity>10</quantity><sum>400.00</sum>
      </product>
    </products>
    <suppliersInfo><supplierInfo><individualPersonRFIndEntr>
      <EGRIPInfo><lastName>ИВАНОВ</lastName><firstName>ИВАН</firstName><middleName>ИВАНОВИЧ</middleName>
        <INN>123456789012</INN><address>г. Тестоград</address></EGRIPInfo>
      <otherInfo><isIP>true</isIP></otherInfo>
    </individualPersonRFIndEntr></supplierInfo></suppliersInfo>
    <addInformationFromClassifiers>
      <OKEI><code>055</code><nationalName>М2</nationalName></OKEI>
      <OKEI><code>796</code><nationalName>ШТ</nationalName></OKEI>
    </addInformationFromClassifiers>
    <currentContractStage>E</currentContractStage>
  </ns3:contract>
</ns3:export>
"""

ROW = {"id": 29759, "file_name": "Печатная форма электронного контракта.html", "xml_source_id": 77,
       "source_xml_tag": "contract", "owner_type": "contract"}


def body():
    """Нормализованное тело синтетического XML контракта."""
    from risk_monitoring import xml_events

    return xml_events.parse_xml(CONTRACT_XML.encode("utf-8"))[2]


class BuildTest(unittest.TestCase):
    """``contract_printform.build``."""

    def setUp(self):
        from risk_monitoring import contract_printform as cp

        self.cp = cp
        self.extraction, self.profile = cp.build(body(), {"type_decode": "Контракт", "group": "contract"}, ROW)
        self.raw = self.extraction["raw_data"]

    def test_numbers_and_dates(self):
        raw = self.raw
        self.assertEqual(raw["source"], "print_form_xml")
        self.assertEqual(raw["contract_number"]["value"], "0123100000126000001/1-ТЗ")
        self.assertEqual(raw["registry_number"], "1123456789026000001")
        self.assertEqual(raw["purchase_number"], "0123100000126000001")
        self.assertEqual(raw["ikz"], "261234567890123400100100010010000244")
        dates = {d["kind"]: d["value"] for d in raw["dates"]}
        self.assertEqual(dates["signing"], "2026-07-09")
        self.assertEqual(dates["contract_end"], "2026-09-30")
        self.assertEqual(dates["protocol"], "2026-07-06")

    def test_finances(self):
        money = {(f["kind"], f["currency"]): f["value"] for f in self.raw["finances"]}
        self.assertEqual(money[("contract_price", "RUB")], 1000.0)
        self.assertEqual(money[("advance", "RUB")], 300.0)
        self.assertEqual(money[("advance", "%")], 30.0)
        self.assertEqual(money[("security", "RUB")], 300.0)

    def test_parties_items_place(self):
        orgs = {o["role"]: o for o in self.raw["organizations"]}
        self.assertEqual(orgs["заказчик"]["inn"], "1234567890")
        self.assertEqual(orgs["поставщик"]["name"], "ИП ИВАНОВ ИВАН ИВАНОВИЧ")
        self.assertEqual(orgs["поставщик"]["inn"], "123456789012")
        # обе позиции (products/product — список, а не только первый элемент)
        self.assertEqual(self.raw["items_total"], 2)
        self.assertEqual([i["unit"] for i in self.raw["items"]], ["М2", "ШТ"])
        self.assertIn("КТРУ 43.32.10.110-00000001", self.raw["items"][1]["specifications"])
        self.assertEqual(self.raw["execution_location"]["address"], "г. Тестоград, ул. Первая, д. 1")
        self.assertEqual(self.raw["warranty"], "Гарантия 12 месяцев")

    def test_summary_and_profile(self):
        self.assertIn("Контракт № 0123100000126000001/1-ТЗ", self.raw["summary"])
        self.assertIn("1 000,00 руб", self.raw["summary"])
        self.assertEqual(self.profile["resume"], self.raw["summary"])
        self.assertTrue(any(c["name"] == "Цена контракта" for c in self.profile["key_conditions"]))
        self.assertLess(len(json.dumps(self.extraction, ensure_ascii=False)), 20000)

    def test_items_limited(self):
        b = body()
        b["products"]["product"] = b["products"]["product"] * 100
        raw = self.cp.build(b, {"group": "contract"}, ROW)[0]["raw_data"]
        self.assertEqual(len(raw["items"]), self.cp.MAX_ITEMS)
        self.assertEqual(raw["items_total"], 200)

    def test_is_contract_xml(self):
        self.assertTrue(self.cp.is_contract_xml(body()))
        self.assertFalse(self.cp.is_contract_xml({"commonInfo": {}}))
        self.assertFalse(self.cp.is_contract_xml(None))


class StaleTest(unittest.TestCase):
    """``contract_printform.is_stale``: старый паспорт контракта (по тексту) пересобирается, по XML — нет."""

    def test_stale(self):
        from risk_monitoring import contract_printform as cp

        old = {"raw_data": {"source": "print_form"}}
        self.assertTrue(cp.is_stale(old, ROW))
        self.assertFalse(cp.is_stale({"raw_data": {"source": "print_form_xml"}}, ROW))
        self.assertFalse(cp.is_stale(old, {**ROW, "xml_source_id": None}))
        notice = {**ROW, "source_xml_tag": "epNotificationEZK2020", "owner_type": "notice",
                  "file_name": "Печатная форма извещения.html"}
        self.assertFalse(cp.is_stale(old, notice))


class OwnerXmlTest(unittest.TestCase):
    """``AIFileAnalyzer._owner_xml_body``: тело из кэша прогона, иначе чтение по ``xml_meta.sql``; сбой — ``None``."""

    def analyzer(self):
        from risk_monitoring.ai_file_analyzer import AIFileAnalyzer

        a = AIFileAnalyzer.__new__(AIFileAnalyzer)
        a.storage_path = "s"
        a.parser = None
        return a

    def test_from_cache(self):
        a = self.analyzer()
        a.lookups = {29759: {"xml_id": 77}}
        a._run_cache().bodies[77] = {"regNum": "1"}
        self.assertEqual(asyncio.run(a._owner_xml_body(ROW)), {"regNum": "1"})

    def test_read_and_failure(self):
        a = self.analyzer()
        b = body()

        class Archives:
            """``ArchiveCache`` прогона."""

            async def read(self, url, entry):
                return CONTRACT_XML.encode("utf-8")

        async def query(name, **kw):
            return [{"id": 77, "archive_url": "u", "entry_name": "e"}]

        a._query = query
        a._run_cache().archives = Archives()
        self.assertEqual(asyncio.run(a._owner_xml_body(ROW))["regNum"], b["regNum"])

        bad = self.analyzer()

        async def broken(name, **kw):
            raise RuntimeError("БД недоступна")

        bad._query = broken
        bad._run_cache().archives = Archives()
        self.assertIsNone(asyncio.run(bad._owner_xml_body(ROW)))
        self.assertIsNone(asyncio.run(bad._owner_xml_body({"id": 1})))


class GuardedTest(unittest.TestCase):
    """``AIFileAnalyzer._guarded``: устаревший паспорт контракта пересобирается и при «completed» во внешней БД."""

    def run_guarded(self, stored_source):
        from risk_monitoring.ai_file_analyzer import AIFileAnalyzer

        a = AIFileAnalyzer.__new__(AIFileAnalyzer)
        a.force = False
        a.semaphore = asyncio.Semaphore(1)
        a.internal = {29759: {"status": "completed", "debug": False,
                              "ai_analysis": {"raw_data": {"source": stored_source}}}}
        calls = []

        async def analyze_file(row, previous, attempt):
            calls.append(row["id"])
            return {"file_id": row["id"], "status": "done"}

        async def reuse(file_id, stored):
            return {"file_id": file_id, "status": "reused"}

        a.analyze_file = analyze_file
        a._reuse_internal = reuse
        res = asyncio.run(a._guarded({**ROW, "rm_status": "completed"}, None))
        return res, calls

    def test_stale_rebuilt(self):
        res, calls = self.run_guarded("print_form")
        self.assertEqual(res["status"], "done")
        self.assertEqual(calls, [29759])

    def test_xml_passport_skipped(self):
        res, calls = self.run_guarded("print_form_xml")
        self.assertEqual(res["reason"], "already_analyzed")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
