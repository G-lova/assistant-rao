"""Находки риска и их цитаты (13.11.2026): цитата на каждую находку, одна цитата — одна находка."""
import unittest

from risk_monitoring import passport


def _risk(fid, i, text, frag=None):
    """Риск модели DOC-010 из файла ``fid`` с одной цитатой."""
    return {"rule_id": "DOC-010", "title": "t", "level": 0.3, "confidence": 0.9, "source": "ai_file",
            "description": text, "evidence": [{"src": f"file:{fid}", "fragment": frag or f"ГОСТ {1000 + i}-80 в пункте {i} файла {fid}"}]}


class FindingsQuotesTest(unittest.TestCase):
    """Находки DOC-010 закупки 0346100001325000019: 59 находок, 20 цитат."""

    def test_every_finding_has_quote(self):
        """59 разных находок в трёх файлах — у каждой своя цитата (раньше цитат было 20)."""
        lists = [[_risk(fid, i, f"Ссылка на недействующий ГОСТ {1000 + i}-80 (файл {fid})") for i in range(n)]
                 for fid, n in ((1, 25), (2, 20), (3, 14))]
        m = passport.merge_risks(*lists)[0]
        self.assertEqual(len(m["findings"]), 59)
        self.assertEqual({e["finding_no"] for e in m["evidence"]}, set(range(1, 60)))

    def test_same_quote_one_finding(self):
        """Та же цитата, другая формулировка — одна находка, обе цитаты (разные файлы) при ней."""
        frag = "ГОСТ 1000-80 «Общие требования» в пункте 4.2"
        m = passport.merge_risks([_risk(1, 0, "Ссылка на недействующий ГОСТ 1000-80", frag)],
                                 [_risk(2, 0, "Указан отменённый ГОСТ 1000-80", frag)])[0]
        self.assertEqual(len(m["findings"]), 1)
        self.assertEqual(len(m["evidence"]), 2)

    def test_short_quote_not_merged(self):
        """Короткая цитата (меньше 15 символов) не склеивает разные находки."""
        m = passport.merge_risks([_risk(1, 0, "Ошибка в коде ОКПД2", "п. 3")],
                                 [_risk(1, 1, "Ссылка на ГОСТ без года", "п. 3")])[0]
        self.assertEqual(len(m["findings"]), 2)

    def test_remerge_keeps_numbers(self):
        """Повторное объединение (файл → паспорт → закупка) не меняет число находок и цитат."""
        lists = [[_risk(1, i, f"Ссылка {i}") for i in range(30)]]
        once = passport.merge_risks(*lists)
        twice = passport.merge_risks(once)[0]
        self.assertEqual(len(twice["findings"]), 30)
        self.assertEqual(len({e["finding_no"] for e in twice["evidence"]}), 30)


if __name__ == "__main__":
    unittest.main()
