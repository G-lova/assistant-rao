"""Схемы извлечения: значения допускают null, цитата генерируется до значения, набор полей прежний.

Формат ответа ``/evaluate-documents`` и ``raw_data`` не меняется: те же ключи, отсутствующие значения после
объединения блоков выглядят как раньше (пустые строки / ``None`` из шаблона), а не как «None»-строки.
"""
import asyncio
import json
import unittest
from unittest import mock

from evaluate_documents.evidence_check import (
    drop_nulls, inn_valid, kpp_valid, ogrn_valid, verify_raw_data,
)
from tests.stubs import stubbed_import

SCHEMAS = ("schemas/type_data_extractor_schema.json", "schemas/data_extractor_schema.json")

# Набор полей raw_data до изменения схемы — формат ответа должен остаться прежним
RAW_DATA_FIELDS = {
    '.contract_number', '.contract_number.evidence', '.contract_number.value', '.dates', '.dates[].context',
    '.dates[].evidence', '.dates[].value', '.document_name', '.document_name.confidence', '.document_name.value',
    '.execution_location', '.execution_location.address', '.execution_location.evidence',
    '.execution_location.special_conditions', '.finances', '.finances[].context', '.finances[].currency',
    '.finances[].evidence', '.finances[].value', '.items', '.items[].evidence', '.items[].name', '.items[].quantity',
    '.items[].specifications', '.items[].unit', '.law_references', '.law_references[].evidence',
    '.law_references[].value', '.nmck_method', '.nmck_method.assessment', '.nmck_method.evidence',
    '.nmck_method.method', '.organizations', '.organizations[].address', '.organizations[].evidence',
    '.organizations[].inn', '.organizations[].kpp', '.organizations[].name', '.organizations[].ogrn',
    '.organizations[].role', '.other_data', '.other_data[].evidence', '.other_data[].value', '.planned_timeline',
    '.planned_timeline.end_date', '.planned_timeline.evidence', '.planned_timeline.milestones',
    '.planned_timeline.start_date', '.planned_timeline.total_duration', '.procurement_subject',
    '.procurement_subject.description', '.procurement_subject.evidence', '.reporting_documentation',
    '.reporting_documentation[].deadline', '.reporting_documentation[].document_name',
    '.reporting_documentation[].evidence', '.reporting_documentation[].format',
    '.reporting_documentation[].frequency', '.summary',
}


def _paths(node, path=""):
    """Пути полей схемы (без вложенностей evidence)."""
    out = set()
    if not isinstance(node, dict):
        return out
    if node.get("type") == "array" and isinstance(node.get("items"), dict):
        return _paths(node["items"], path + "[]")
    for key, child in (node.get("properties") or {}).items():
        out.add(f"{path}.{key}")
        if key != "evidence":
            out |= _paths(child, f"{path}.{key}")
    return out


def _facts(raw_data):
    """Объекты фактов схемы: (путь, схема объекта) для всех, у кого есть evidence."""
    for key, node in raw_data["properties"].items():
        target = node.get("items") if node.get("type") == "array" else node
        if isinstance(target, dict) and "evidence" in (target.get("properties") or {}):
            yield key, target


class SchemaTest(unittest.TestCase):
    """Свойства обеих схем извлечения."""

    def test_same_fields_as_before(self):
        """Набор полей raw_data не изменился (ответ эндпоинта прежний); верхний уровень тот же."""
        for path in SCHEMAS:
            schema = json.load(open(path, encoding="utf-8"))
            top = {x for x in _paths(schema["properties"]["raw_data"]) if x.count(".") <= 2}
            self.assertEqual(top, RAW_DATA_FIELDS, path)
        self.assertEqual(list(json.load(open(SCHEMAS[0]))["properties"]), ["type_compliance", "readability", "raw_data"])
        self.assertEqual(list(json.load(open(SCHEMAS[1]))["properties"]), ["readability", "raw_data"])

    def test_evidence_first_and_fragment_first(self):
        """В каждом факте evidence — первое поле, в evidence первая и единственная обязательная — fragment."""
        for path in SCHEMAS:
            raw = json.load(open(path, encoding="utf-8"))["properties"]["raw_data"]
            for key, obj in _facts(raw):
                self.assertEqual(next(iter(obj["properties"])), "evidence", f"{path}: {key}")
                ev = obj["properties"]["evidence"]
                ev = ev["items"] if ev.get("type") == "array" else ev
                self.assertEqual(next(iter(ev["properties"])), "fragment", f"{path}: {key}")
                self.assertEqual(ev["required"], ["fragment"], f"{path}: {key}")

    def test_values_nullable(self):
        """Все значения фактов допускают null (модель не обязана придумывать ИНН или метод НМЦК)."""
        for path in SCHEMAS:
            raw = json.load(open(path, encoding="utf-8"))["properties"]["raw_data"]
            for key, obj in _facts(raw):
                for field, node in obj["properties"].items():
                    if field == "evidence" or node.get("type") in ("array", "object"):
                        continue
                    self.assertIn("null", node["type"], f"{path}: {key}.{field}")
            self.assertEqual(raw["properties"]["summary"]["type"], "string")
            self.assertNotEqual(raw["properties"]["contract_number"]["properties"]["value"].get("default"), "")


class RequisitesTest(unittest.TestCase):
    """Контрольные числа реквизитов."""

    def test_inn(self):
        """Настоящие ИНН проходят, заглушки — нет."""
        for good in ("7021000043", "7017464476", "500100732259"):
            self.assertTrue(inn_valid(good), good)
        for bad in ("6301000000", "7001000000", "1410000000", "70171386200", "", None, "абв"):
            self.assertFalse(inn_valid(bad), bad)

    def test_ogrn_kpp(self):
        """ОГРН с контрольным числом, КПП по формату."""
        self.assertTrue(ogrn_valid("1027000867068"))
        self.assertFalse(ogrn_valid("1027000000000"))
        self.assertTrue(kpp_valid("701701001"))
        self.assertTrue(kpp_valid("7701AB001"))
        self.assertFalse(kpp_valid("70170100"))

    def test_invalid_inn_in_text_dropped(self):
        """ИНН есть в тексте, но не проходит контрольное число (ошибка OCR или заглушка) — значение пустое."""
        out, dropped = verify_raw_data({"organizations": [{"name": "ООО Альфа", "inn": "1410000000"}]},
                                       "ООО Альфа ИНН 1410000000")
        self.assertEqual(out["organizations"][0]["inn"], "")
        self.assertTrue(dropped)


class DropNullsTest(unittest.TestCase):
    """null из ответа модели."""

    def test_drop_nulls(self):
        """null-поля убираются; записи списка без значения — целиком; прочее не трогается."""
        chunk = {
            "type_compliance": {"status": "allow", "detected_type": "docContractDoWorkFiles", "issues": []},
            "raw_data": {
                "document_name": {"value": "Договор", "confidence": 0.9},
                "contract_number": {"evidence": {"fragment": "", "page": None}, "value": None},
                "finances": [{"evidence": {"fragment": "цена 100"}, "value": 100.0, "currency": None},
                             {"evidence": {"fragment": "—"}, "value": None, "context": "НМЦК"}],
                "organizations": [{"evidence": {"fragment": "ООО Альфа"}, "name": "ООО Альфа", "inn": None}],
                "nmck_method": {"evidence": {"fragment": ""}, "method": None, "assessment": None},
            },
        }
        out = drop_nulls(chunk)
        raw = out["raw_data"]
        self.assertEqual(raw["contract_number"], {"evidence": {"fragment": ""}})
        self.assertEqual(raw["finances"], [{"evidence": {"fragment": "цена 100"}, "value": 100.0}])
        self.assertEqual(raw["organizations"], [{"evidence": {"fragment": "ООО Альфа"}, "name": "ООО Альфа"}])
        self.assertEqual(raw["nmck_method"], {"evidence": {"fragment": ""}})
        self.assertEqual(out["type_compliance"], chunk["type_compliance"])


class MergeShapeTest(unittest.TestCase):
    """Объединение блоков с null-ответом даёт прежнюю форму raw_data без строк «None»."""

    def test_merge_after_nulls(self):
        """Ключи raw_data — как в шаблоне объединения; «None» нигде нет."""
        chunk = {
            "type_compliance": {"status": "allow", "detected_type": "docContractDoWorkFiles", "issues": []},
            "readability": {"status": "allow", "issues": [], "readability_score": 0.9, "main_language": "ru",
                            "language_confidence": 0.9},
            "raw_data": {
                "document_name": {"value": "Договор № 1", "confidence": 0.9},
                "summary": "Договор подряда.",
                "contract_number": {"evidence": {"fragment": "Договор № 1"}, "value": "1"},
                "nmck_method": {"evidence": {"fragment": ""}, "method": None, "assessment": None},
                "organizations": [{"evidence": {"fragment": "ООО Альфа"}, "name": "ООО Альфа", "role": "подрядчик",
                                   "inn": None, "kpp": None, "ogrn": None, "address": None}],
                "law_references": [{"evidence": [{"fragment": "223-ФЗ"}], "value": None}],
                "dates": [], "finances": [],
            },
        }
        reply = mock.Mock()
        reply.choices = [mock.Mock(message=mock.Mock(content="Договор подряда."))]
        client = mock.Mock()
        client.chat.completions.create = mock.AsyncMock(return_value=reply)
        with stubbed_import("evaluate_documents.type_data_extractor") as tde:
            extractor = tde.TypeDataExtractor(client, "m")
            merged = asyncio.run(extractor.merge_chunk_results([drop_nulls(chunk)], "docContractDoWorkFiles", 7,
                                                               "Договор.docx"))
        raw = merged["raw_data"]
        for key in ("document_name", "summary", "contract_number", "dates", "finances", "nmck_method",
                    "organizations", "law_references", "procurement_subject", "items", "planned_timeline",
                    "reporting_documentation", "execution_location", "other_data"):
            self.assertIn(key, raw)
        self.assertNotIn("None", json.dumps(raw, ensure_ascii=False))
        self.assertEqual(raw["contract_number"]["value"], "1")
        self.assertEqual(raw["law_references"], [])
        self.assertEqual(raw["organizations"][0]["name"], "ООО Альфа")


if __name__ == "__main__":
    unittest.main()
