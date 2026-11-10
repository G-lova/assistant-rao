"""Нормативные акты: разбор по структуре (legal_acts), проверка ссылок модели (legal_refs), положения заказчиков
(orderClause, typal_clauses.parse_order_clause_xml). Тексты — сокращённые фрагменты сводных редакций."""
import asyncio
import unittest

from risk_monitoring import legal_acts, legal_refs, typal_clauses

LAW = """5 апреля 2013 года
N 44-ФЗ
РОССИЙСКАЯ ФЕДЕРАЦИЯ
ФЕДЕРАЛЬНЫЙ ЗАКОН
О КОНТРАКТНОЙ СИСТЕМЕ
В СФЕРЕ ЗАКУПОК
Принят
Государственной Думой
Глава 1. ОБЩИЕ ПОЛОЖЕНИЯ
Статья 21. Утратила силу с 1 октября 2019 года. - Федеральный закон от 01.05.2019 N 71-ФЗ.
Статья 96. Обеспечение исполнения контракта
1. Заказчиком в извещении об осуществлении закупки
устанавливается требование обеспечения исполнения контракта.
(в ред. Федерального закона от 02.07.2021 N 360-ФЗ)
КонсультантПлюс: примечание.
Положения ч. 6 ст. 96 применяются с учетом ...
6. Размер обеспечения устанавливается от одной второй процента до тридцати про-
центов начальной (максимальной) цены контракта, за исключением случаев, если:
1) контрактом предусмотрена выплата аванса;
2) аванс превышает тридцать процентов;
12
6.1. Утратил силу.
"""

DECREE = """ПРАВИТЕЛЬСТВО РОССИЙСКОЙ ФЕДЕРАЦИИ
ПОСТАНОВЛЕНИЕ
от 30 августа 2017 г. N 1042
ОБ УТВЕРЖДЕНИИ ПРАВИЛ
ОПРЕДЕЛЕНИЯ РАЗМЕРА ШТРАФА
Правительство Российской Федерации постановляет:
1. Утвердить прилагаемые Правила.
2. Признать утратившим силу постановление.
Утверждены
постановлением Правительства
Российской Федерации
от 30 августа 2017 г. N 1042
ПРАВИЛА
ОПРЕДЕЛЕНИЯ РАЗМЕРА ШТРАФА
1. Настоящие Правила устанавливают порядок определения размера штрафа.
3. За каждый факт неисполнения обязательств размер штрафа устанавливается:
а) 10 процентов цены контракта (этапа), если цена не превышает 3 млн. рублей;
б) 5 процентов цены контракта (этапа);
"""

ORDER_OWN = """<?xml version="1.0" encoding="UTF-8"?>
<ns2:orderClause xmlns="http://zakupki.gov.ru/223fz/types/1" xmlns:ns2="http://zakupki.gov.ru/223fz/orderClause/1">
 <ns2:body><ns2:item><guid>g</guid><ns2:orderClauseData>
  <guid>g</guid>
  <customer><mainInfo><fullName>ФЕДЕРАЛЬНОЕ ГОСУДАРСТВЕННОЕ УНИТАРНОЕ ПРЕДПРИЯТИЕ "ЦУИ"</fullName>
   <inn>7710107720</inn><kpp>771701001</kpp><okopfName>Федеральные государственные унитарные предприятия</okopfName>
  </mainInfo></customer>
  <status>P</status><version>3</version><name>Положение о закупке товаров, работ, услуг</name>
  <registrationNumber>1250295981</registrationNumber>
  <modificationDescription>Приказ № 68-ОД/2026 от 10.11.2026</modificationDescription>
  <approveDate>2026-03-16</approveDate><effectiveDate>2026-11-10</effectiveDate>
  <purchaseMethod><purchaseMethodCode>696083</purchaseMethodCode><purchaseMethodName>Запрос котировок</purchaseMethodName>
   <digitalPurchase>true</digitalPurchase><submissionApplicationDate>7</submissionApplicationDate><workDays>true</workDays>
  </purchaseMethod>
  <publicationDateTime>2026-11-10T14:39:05+03:00</publicationDateTime>
  <attachments>
   <document><fileName>Положение редакция на 10.11.2026.docx</fileName><description>Положение редакция</description>
    <url>https://zakupki.gov.ru/223/filestore/public/1.0/download/fz223/file.html?uid=A</url><contentUid>A</contentUid></document>
   <document><fileName>Приказ №68-ОД-2026 от 10.11.2026.pdf</fileName><description>Приказ</description>
    <url>https://zakupki.gov.ru/223/filestore/public/1.0/download/fz223/file.html?uid=B</url><contentUid>B</contentUid></document>
  </attachments>
  <paymentPeriod>7</paymentPeriod>
 </ns2:orderClauseData></ns2:item></ns2:body>
</ns2:orderClause>""".encode("utf-8")

ORDER_JOINED = """<?xml version="1.0" encoding="UTF-8"?>
<ns2:orderClause xmlns="http://zakupki.gov.ru/223fz/types/1" xmlns:ns2="http://zakupki.gov.ru/223fz/orderClause/1">
 <ns2:body><ns2:item><guid>g</guid><ns2:orderClauseData>
  <customer><mainInfo><fullName>АО "ВТЕ ЮГО-ВОСТОК"</fullName><inn>7732523241</inn><kpp>772101001</kpp></mainInfo></customer>
  <version>33</version><name>Положение о закупке товаров, работ, услуг</name>
  <registrationNumber>1150076900</registrationNumber>
  <headAgency><mainInfo><fullName>АО "МОСВОДОКАНАЛ"</fullName><inn>7701984274</inn><kpp>770101001</kpp></mainInfo></headAgency>
  <headRegistrationNumber>1130012228</headRegistrationNumber>
  <numberDecision>11/2026</numberDecision><dateDecision>2026-11-02</dateDecision>
  <textDecision>Решение о присоединении к Положению о закупке АО "Мосводоканал"</textDecision>
  <publicationDateTime>2026-11-10T13:35:14+03:00</publicationDateTime>
  <attachments>
   <document><fileName>Приложение N 1.docx</fileName><description>Приложение 1 к Положению о закупках</description>
    <url>u1</url><contentUid>1</contentUid></document>
   <document><fileName>Изменения в Положение о закупке.pdf</fileName><description>Изменения</description>
    <url>u2</url><contentUid>2</contentUid></document>
   <document><fileName>Положение о закупках.docx</fileName><description>Положение о закупках.docx</description>
    <url>u3</url><contentUid>3</contentUid></document>
   <document><fileName>Решение.docx</fileName><description>Решение.docx</description><url>u4</url><contentUid>4</contentUid></document>
  </attachments>
 </ns2:orderClauseData></ns2:item></ns2:body>
</ns2:orderClause>""".encode("utf-8")


class LawParseTest(unittest.TestCase):
    """Закон: статьи, части, пункты; пометки КонсультантПлюс, номера страниц и переносы не попадают в текст."""

    def setUp(self):
        """Разбор фрагмента 44-ФЗ с датой редакции из имени файла."""
        self.act = legal_acts.parse_act(LAW, "Федеральный закон от 05.04.2013 N 44-ФЗ (ред. от 04.08.2026).pdf")
        self.by_addr = {n["address"]: n for n in self.act["norms"]}

    def test_identity(self):
        """Ключ, вид (закон, а не кодекс из-за слова «кодекс» в тексте) и дата редакции."""
        self.assertEqual((self.act["act_key"], self.act["kind"], self.act["edition_date"], self.act["edition_source"]),
                         ("44-ФЗ", "law", "2026-08-04", "stated"))

    def test_structure_and_cleanup(self):
        """«ч. 6 ст. 96» с пунктами; перенос «про-центов» склеен; примечание и «(в ред. …)» убраны."""
        part6 = self.by_addr["ч. 6 ст. 96 44-ФЗ"]
        self.assertIn("тридцати процентов", part6["text"])
        self.assertEqual(part6["heading"], "Обеспечение исполнения контракта")
        self.assertIn("п. 2 ч. 6 ст. 96 44-ФЗ", self.by_addr)
        text = " ".join(n["text"] for n in self.act["norms"])
        self.assertNotIn("в ред.", text)
        self.assertNotIn("КонсультантПлюс", text)
        self.assertNotIn(" 12 ", self.by_addr["п. 2 ч. 6 ст. 96 44-ФЗ"]["text"] + " ")

    def test_lost_force(self):
        """Утратившие силу статья и часть отмечены и не идут в чанки эмбеддингов."""
        self.assertTrue(self.by_addr["ст. 21 44-ФЗ"]["lost_force"])
        self.assertTrue(self.by_addr["ч. 6.1 ст. 96 44-ФЗ"]["lost_force"])
        chunks = legal_acts.chunks_for_embedding(self.act["norms"])
        self.assertFalse(any(c["address"].startswith("ст. 21 ") for c in chunks))
        self.assertTrue(any(c["text"].startswith("ч. 6 ст. 96 44-ФЗ (Обеспечение") for c in chunks))

    def test_edition_given_wins(self):
        """Явная дата редакции важнее имени файла; без даты — предупреждающий источник."""
        self.assertEqual(legal_acts.identify(LAW, "x (ред. от 04.08.2026).pdf", "2026-09-01")["edition_date"],
                         "2026-09-01")
        self.assertEqual(legal_acts.identify(LAW)["edition_source"], "act_date")

    def test_same_number_other_year(self):
        """«223-ФЗ» другого года не выдаёт себя за закон о закупках."""
        other = LAW.replace("5 апреля 2013 года\nN 44-ФЗ", "13 июля 2015 года\nN 223-ФЗ")
        self.assertEqual(legal_acts.identify(other)["act_key"], "223-ФЗ от 2015-07-13")


class DecreeParseTest(unittest.TestCase):
    """Постановление: пункт 3 Правил отделён от пункта самого постановления."""

    def test_segments(self):
        """Сегмент «Правила …», подпункты буквой; адрес «пп. "а" п. 3 Правил ПП-1042»."""
        act = legal_acts.parse_act(DECREE)
        self.assertEqual((act["act_key"], act["kind"]), ("ПП-1042", "decree"))
        addrs = [n["address"] for n in act["norms"]]
        self.assertIn("п. 2 ПП-1042", addrs)
        self.assertIn('пп. "а" п. 3 Правил ПП-1042', addrs)
        seg = {n["segment"] for n in act["norms"] if n["segment"]}
        self.assertEqual(seg, {"Правила определения размера штрафа"})


class RefsTest(unittest.TestCase):
    """Разбор ссылок модели и проверка по нормам."""

    @classmethod
    def setUpClass(cls):
        """Нормы фрагментов 44-ФЗ и ПП 1042."""
        cls.law = legal_acts.parse_act(LAW)["norms"]
        cls.decree = legal_acts.parse_act(DECREE)["norms"]

    def test_parse(self):
        """Несколько ссылок, акт по упоминанию и по умолчанию, НК по номеру статьи."""
        refs = legal_refs.parse_refs("ч. 6 ст. 96 Федерального закона № 44-ФЗ; пп. «а» п. 3 Правил, утв. ПП РФ № 1042")
        self.assertEqual([(r["act"], r.get("article"), r.get("part"), r.get("point"), r.get("sub_letter")) for r in refs],
                         [("44-ФЗ", "96", "6", None, None), ("ПП-1042", None, None, "3", "а")])
        self.assertEqual(legal_refs.parse_refs("ст. 34", "44-ФЗ")[0]["act"], "44-ФЗ")
        self.assertEqual(legal_refs.parse_refs("п. 3 ст. 164 НК РФ")[0]["act"], "НК")
        self.assertEqual(legal_refs.parse_refs("ст. 11 НК РФ")[0]["act"], "НК-1")
        self.assertEqual(legal_refs.parse_refs("ст. 34 и ст. 94 44-ФЗ")[1]["article"], "94")
        self.assertEqual(legal_refs.parse_refs(None), [])

    def test_check(self):
        """Найдена, нет части, утратила силу; пункт Правил, а не постановления."""
        ref = legal_refs.parse_refs("ч. 6 ст. 96 44-ФЗ")[0]
        self.assertEqual(legal_refs.check_ref(ref, "law", self.law)["address"], "ч. 6 ст. 96 44-ФЗ")
        bad = legal_refs.check_ref(legal_refs.parse_refs("ч. 99 ст. 96 44-ФЗ")[0], "law", self.law)
        self.assertEqual((bad["status"], bad["reason"]), ("not_found", "нет части 99"))
        self.assertEqual(legal_refs.check_ref(legal_refs.parse_refs("ст. 21 44-ФЗ")[0], "law", self.law)["status"],
                         "lost_force")
        res = legal_refs.check_ref(legal_refs.parse_refs("п. 3 Правил ПП № 1042")[0], "decree", self.decree)
        self.assertEqual(res["address"], "п. 3 Правил ПП-1042")
        self.assertEqual(legal_refs.summarize([{"status": "found"}, {"status": "not_found"}]), "not_found")

    def test_checker_annotate(self):
        """LawChecker: law_check только у рисков с проверяемой ссылкой; незагруженный акт — без пометки."""
        law, decree = self.law, self.decree

        class FakeActs:
            """Редакции 44-ФЗ и ПП 1042 без базы."""

            async def edition_on(self, key, on=None):
                """Редакция по ключу."""
                return {"44-ФЗ": {"id": 1, "kind": "law", "edition_date": "2026-08-04"},
                        "ПП-1042": {"id": 2, "kind": "decree", "edition_date": "2017-08-30"}}.get(key)

            async def norms(self, act_id):
                """Нормы редакции."""
                return law if act_id == 1 else decree

        checker = legal_refs.LawChecker.__new__(legal_refs.LawChecker)
        checker.acts, checker._editions, checker._norms = FakeActs(), {}, {}
        risks = [{"law": "ч. 99 ст. 96"}, {"law": "п. 3 Правил ПП № 1042"}, {"law": "ст. 309 ГК РФ"}, {"law": None}]
        counts = asyncio.run(checker.annotate(risks, fz=44))
        self.assertEqual(risks[0]["law_check"]["status"], "not_found")
        self.assertEqual(risks[1]["law_check"]["refs"][0]["address"], "п. 3 Правил ПП-1042")
        self.assertNotIn("law_check", risks[2])
        self.assertEqual(counts, {"not_found": 1, "found": 1, "unknown_act": 1})

    def test_passport_keeps_law_check(self):
        """law_check проходит во внешний риск."""
        from risk_monitoring import passport

        out = passport.to_external_risks([{"code": "FIN-007", "severity": "medium", "title": "t", "explanation": "e",
                                           "law": "ч. 99 ст. 96", "law_check": {"status": "not_found"}}], fz=44)
        self.assertEqual(out[0]["law_check"], {"status": "not_found"})


class OrderClauseTest(unittest.TestCase):
    """Положения заказчиков (orderClause): собственное и присоединение."""

    def test_own(self):
        """Собственное: дата применения — effectiveDate, способы закупки, файл «Положение …» — position."""
        c = typal_clauses.parse_clause_xml(ORDER_OWN)[0]
        self.assertEqual((c["clause_type"], c["registration_number"], c["version"], c["use_for_base_date"],
                          c["approve_date"], c["head_registration_number"]),
                         ("customer", "1250295981", 3, "2026-11-10", "2026-03-16", None))
        self.assertEqual(c["customers"][0]["inn"], "7710107720")
        self.assertEqual(c["customers"][0]["agency_kind"], "unitary")
        self.assertEqual(c["purchase_methods"][0]["application_days"], "7")
        self.assertEqual([f["kind"] for f in c["files"]], ["position", "order"])
        self.assertFalse(c["data"]["joined"])

    def test_joined(self):
        """Присоединение: головная организация, решение, дата применения — дата решения; приложения отдельно."""
        c = typal_clauses.parse_clause_xml(ORDER_JOINED)[0]
        self.assertEqual((c["head_registration_number"], c["use_for_base_date"]), ("1130012228", "2026-11-02"))
        self.assertEqual(c["data"]["head"]["inn"], "7701984274")
        self.assertEqual(c["data"]["decision"]["number"], "11/2026")
        self.assertEqual([f["kind"] for f in c["files"]], ["appendix", "order", "position", "order"])
        self.assertEqual(typal_clauses.position_for(c["files"], None)["file_name"], "Положение о закупках.docx")

    def test_typal_still_parsed(self):
        """Диспетчер: не положение — ошибка."""
        with self.assertRaises(ValueError):
            typal_clauses.parse_clause_xml(b"<contract><versionNumber>1</versionNumber></contract>")


if __name__ == "__main__":
    unittest.main()
