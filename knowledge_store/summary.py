"""Сводное экспертное заключение (этап 5): сборка ``data`` из фактов хранилища знаний.

Принципы:

* ``data`` содержит **ровно ключи формы** из справочника ``pe_form_fields`` (ключи не хардкодятся);
  значение, которое нельзя подтвердить доказательством, не ставится (``None``) и остаётся эксперту;
* доказательства (документ, страница, цитата, пояснение модели) лежат отдельно в ``trace``;
* блоки III–IV (``field3``, ``field4``) формулирует LLM только по **проверенным** результатам;
* тексты документов повторно не читаются: используются факты ``pe_facts`` и сохранённые тексты
  (для проверки цитат).

Модуль не зависит от сети: LLM и построение фактов передаются снаружи.
"""
import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from configs.config import Config
from configs.logger import get_logger
from knowledge_store import eis_notice, facts as facts_mod, forms, repository as repo

logger = get_logger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "summary_blocks_prompt.txt"
RESULT_KINDS = (forms.KIND_PRESENCE, forms.KIND_COMPLIANCE)
TEXT_BLOCK_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 3000}},
    "required": ["text"],
}
BLOCK_TITLES = {"field3": "вывод", "field4": "заключение"}  # «Блок III. Вывод», «Блок IV. Заключение»

LlmCall = Callable[[List[dict], dict], Awaitable[str]]
FactsBuilder = Callable[[], Awaitable[Any]]


class SummaryError(Exception):
    """Сводное ЭЗ собрать нельзя; ``http_status`` и ``message`` пригодны для ответа API.

    Attributes:
        http_status: Предлагаемый HTTP-статус ответа (409 — не хватает данных, 422 — форма не поддерживается).
        message: Сообщение для пользователя.
    """

    http_status = 409

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NoDataError(SummaryError):
    """По экспертизе нет документов в хранилище знаний."""


class TextPurgedError(SummaryError):
    """Тексты документов уже очищены политикой хранения."""


class UnsupportedFormError(SummaryError):
    """Для экспертизы нет формы заключения в справочнике."""

    http_status = 422


class NoFactsError(SummaryError):
    """Факты ещё не построены."""


def _as_dict(value: Any) -> Optional[dict]:
    """JSONB из asyncpg приходит строкой — приводит к ``dict`` (``None`` для пустого/непонятного)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def normalize_result(value: Any) -> Optional[int]:
    """Приводит значение критерия к ``0``/``1``/``2`` (в данных встречаются и числа, и строки).

    Args:
        value: Значение из факта.

    Returns:
        Optional[int]: ``0``, ``1`` или ``2``; ``None``, если значение не распознано.
    """
    if isinstance(value, bool):
        return int(value)
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number in (0, 1, 2) else None


def evidence_ok(fact: dict, doc_text: Optional[str]) -> bool:
    """Проверяет доказательство факта (валидатор 5.4).

    Факты из XML извещения (``eis_xml``) считаются доказанными структурой XML. Для остальных ответ «1»
    требует цитаты, которая дословно есть в тексте документа; для «0»/«2» цитата необязательна, но
    если она указана, то тоже должна быть в тексте. Значение без подтверждения ставится в заключение как
    предложение модели (статус ``proposed`` в ``trace``) — см. :func:`assemble`.

    Args:
        fact: Строка ``pe_facts`` (``dict``) с уже разобранным ``value``.
        doc_text: Полный текст документа, на который ссылается факт (``None`` — документа нет).

    Returns:
        bool: ``True``, если значение можно поставить в заключение.
    """
    if fact.get("source") == facts_mod.SOURCE_EIS:
        return True
    quote = (fact.get("quote") or "").strip()
    if not quote:
        return fact["result"] != 1
    return bool(doc_text) and facts_mod.quote_in_text(quote, doc_text)


METHOD_STEMS = {"нормативн": "нормативный", "затратн": "затратный", "тарифн": "тарифный",
                "проектно-сметн": "проектно-сметный", "рыночн": "сопоставления рыночных цен"}


def used_nmck_method(documents: Dict[int, dict]) -> Tuple[Optional[str], Optional[str]]:
    """Метод расчёта НМЦК, указанный в документах («Используемый метод определения НМЦК…»).

    Args:
        documents: ``document_id`` → описание документа с полем ``text``.

    Returns:
        Tuple[Optional[str], Optional[str]]: ``(ключ метода из METHOD_STEMS, найденная строка)``; ``(None, None)``,
        если метод в текстах не назван.
    """
    for doc in documents.values():
        match = re.search(r"определения\s+Н\(?М?\)?ЦК[^\n]{0,300}", doc.get("text") or "", re.I)
        if not match:
            continue
        snippet = match.group(0)
        for stem in METHOD_STEMS:
            if stem in snippet.lower() or (stem == "рыночн" and "анализ рынка" in snippet.lower()):
                return stem, snippet.strip()
    return None, None


def apply_nmck_method_rule(fields: Sequence[dict], documents: Dict[int, dict], data: Dict[str, Any],
                           trace: Dict[str, Any]) -> None:
    """Критерии «расчёт НМЦК … методом» при другом применённом методе — «2» (не применимо).

    Методы взаимоисключающие, поэтому критерии про неприменённые методы получают ``2`` детерминированно
    (модель в этих случаях ошибочно ставит «0» и порождает ложные замечания). Критерий применённого метода
    остаётся за моделью. Изменяет ``data``/``trace`` на месте.

    Args:
        fields: Поля формы.
        documents: Документы экспертизы с текстами.
        data: ``data`` (изменяется на месте).
        trace: ``trace`` (изменяется на месте).
    """
    used, snippet = used_nmck_method(documents)
    if not used:
        return
    for field in fields:
        match = re.search(r"расчета\s+НМЦК\s+([\w-]+)\s+методом", field.get("label") or "", re.I)
        if not match or field["field_key"] not in data or field["value_kind"] not in RESULT_KINDS:
            continue          # у текстового поля `*_text` та же подпись, что у критерия — его не трогаем
        stem = next((k for k in METHOD_STEMS if match.group(1).lower().startswith(k)), None)
        if stem and stem != used:
            data[field["field_key"]] = 2
            trace[field["field_key"]] = {"status": "derived", "rule": f"в документах применён метод «{METHOD_STEMS[used]}»; "
                                         "другие методы расчёта НМЦК не применимы", "quote": snippet[:300]}


def section_result(children: Sequence[Optional[int]]) -> Optional[int]:
    """Итог подраздела по его критериям: есть «0» → ``0``; все решены → ``1``; иначе ``None``.

    Args:
        children: Значения дочерних критериев (``None`` — не решён).

    Returns:
        Optional[int]: Итог подраздела.
    """
    if any(v == 0 for v in children):
        return 0
    if children and all(v is not None for v in children):
        return 1
    return None


def numbered(items: Sequence[str]) -> str:
    """Нумерованный перечень («1. …»), как пишут эксперты в полях ``*_text``."""
    return "\n".join(f"{i}. {text}" for i, text in enumerate(items, 1))


def clean_comment(comment: Optional[str], filename: Optional[str] = None) -> Optional[str]:
    """Убирает из пояснения модели ссылки на «фрагменты» (их нумерация эксперту ничего не говорит).

    «В фрагменте 6 указано…» → «В документе «имя» указано…» (или «В документации указано…»).

    Args:
        comment: Пояснение модели.
        filename: Имя документа факта.

    Returns:
        Optional[str]: Очищенный текст.
    """
    if not comment:
        return comment
    name = f"«{filename}»" if filename else ""

    def repl(m: "re.Match") -> str:
        """«В фрагменте N» → «В документе «имя»»; «из фрагмента N» → «из документа «имя»»."""
        prep = m.group(1)
        if prep.lower() == "в":
            return f"{prep} {'документе ' + name if name else 'документации'}".strip()
        return f"{prep} {'документа ' + name if name else 'документации'}".strip()

    text = re.sub(r"(?i)\b(в|из)\s+фрагмент(?:е|ах|ов|а)\s*\d+(?:\s*(?:,|и|-)\s*\d+)*", repl, comment)
    text = re.sub(r"(?i)\bфрагмент(?:е|ах|ов|а)?\s*\d+", "документ", text)
    return text.strip()


def assemble(fields: Sequence[dict], fact_rows: Sequence[dict], documents: Dict[int, dict],
             procurement: Optional[dict] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Собирает ``data`` и ``trace`` по полям формы (без LLM).

    Args:
        fields: Поля формы (``pe_form_fields``), в порядке ``ordinal``.
        fact_rows: Факты экспертизы (``pe_facts``); для одного ключа берётся последний.
        documents: ``document_id`` → ``{"filename", "doc_code", "text"}``.
        procurement: Паспорт закупки (``nmck``, ``advance``) для числовых полей общих сведений.

    Returns:
        Tuple[dict, dict]: ``(data, trace)``. ``data`` содержит каждый ключ формы (``None`` — оставлено
        эксперту); ``trace[key]`` — ``status`` (``verified`` / ``unverified`` / ``no_fact`` / ``derived`` /
        ``from_passport``), источник, документ, страница, цитата, пояснение.
    """
    by_key: Dict[str, dict] = {}
    for row in fact_rows:
        value = _as_dict(row.get("value")) or {}
        by_key[row["fact_key"]] = {**row, "value_obj": value, "result": normalize_result(value.get("value")),
                                   "verified": bool(value.get("verified")), "comment": value.get("comment")}
    data: Dict[str, Any] = {f["field_key"]: None for f in fields}
    trace: Dict[str, Any] = {}
    kinds = {f["field_key"]: f["value_kind"] for f in fields}

    for field in fields:
        key, kind = field["field_key"], field["value_kind"]
        if kind not in RESULT_KINDS:
            continue
        fact = by_key.get(key)
        if not fact:
            trace[key] = {"status": "no_fact"}
            continue
        doc = documents.get(fact.get("document_id")) or {}
        entry = {"status": "unverified", "source": fact.get("source"), "document": doc.get("filename"),
                 "page": fact.get("page"), "quote": fact.get("quote"),
                 "comment": clean_comment(fact["comment"], doc.get("filename"))}
        result = fact["result"]
        proven = bool(fact["verified"]) and evidence_ok(fact, doc.get("text"))
        if result is None:
            pass
        elif fact.get("source") == facts_mod.SOURCE_EIS or (result == 1 and proven):
            data[key], entry["status"] = result, "verified"
        else:
            # значение без подтверждённой цитаты («отсутствует» нечем процитировать; «1» модель не смогла
            # подтвердить) ставится как предложение модели: эксперт видит его в trace и проверяет
            data[key], entry["status"] = result, ("verified" if proven else "proposed")
            if not proven and fact["value_obj"].get("model_quote"):
                entry["model_quote"] = fact["value_obj"]["model_quote"]
        if fact["value_obj"].get("error"):
            entry["error"] = fact["value_obj"]["error"]
        if result is None and fact["value_obj"].get("value") == 3:
            entry["note"] = "по найденным фрагментам определить нельзя — оставлено эксперту"
        trace[key] = entry

    apply_nmck_method_rule(fields, documents, data, trace)

    # итоги подразделов — из решённых дочерних критериев
    for field in fields:
        key = field["field_key"]
        if field["value_kind"] != forms.KIND_SECTION:
            continue
        children = [data[k] for k, kind in kinds.items()
                    if kind in RESULT_KINDS and k.startswith(key + "_") and k[len(key) + 1:len(key) + 2].isdigit()]
        data[key] = section_result(children)
        trace[key] = {"status": "derived", "rule": "0 — есть несоответствие; 1 — все критерии решены; иначе эксперту"}

    # комментарии `<ключ>_text` — в стиле экспертов: нумерованный перечень существа замечаний
    labels = {f["field_key"]: f.get("label") or "" for f in fields}

    def children(base: str) -> List[str]:
        """Критерии-«листья» раздела/подраздела по префиксу ключа."""
        return [k for k in data if kinds.get(k) in RESULT_KINDS and k.startswith(base + "_")
                and k[len(base) + 1:len(base) + 2].isdigit()]

    for key in list(data):
        if not (key.endswith("_text") and kinds.get(key) == forms.KIND_TEXT):
            continue
        base = key[:-5]
        if base in kinds and kinds[base] in RESULT_KINDS:
            if data.get(base) != 0:
                continue
            comment = trace.get(base, {}).get("comment")
            if comment:
                data[key] = numbered([comment])
                trace[key] = {"status": "derived", "rule": f"пояснение к {base}"}
            continue
        kids = children(base)
        if not kids:
            continue
        bad = [k for k in kids if data[k] == 0 and trace.get(k, {}).get("comment")]
        if bad:
            lines = [f"{eis_notice.criterion_number(labels.get(k, '')) or ''} {trace[k]['comment']}".strip() for k in bad]
            head = ("По результатам экспертной оценки соответствия информации, представленной в извещении, "
                    "необходимо обратить внимание на следующие пункты:\n") if base == "field2_1" else ""
            data[key] = head + numbered(lines)
            trace[key] = {"status": "derived", "rule": f"замечания критериев {base}_*"}
        elif base == "field2_1" and all(data[k] is not None for k in kids):
            data[key] = "Информация, представленная в извещении, соответствует требованиям действующего законодательства Российской Федерации."
            trace[key] = {"status": "derived", "rule": "по критериям блока 1 замечаний нет"}

    fill_general_info(fields, by_key, documents, procurement or {}, data, trace)
    return data, trace


def _number(text: Any) -> Optional[float]:
    """Число из строки вида ``1 234 567,89`` или ``1234567.89``; ``None``, если числа нет."""
    match = re.search(r"\d[\d\s\u00a0]*(?:[.,]\d+)?", str(text or ""))
    if not match:
        return None
    try:
        return float(re.sub(r"[\s\u00a0]", "", match.group()).replace(",", "."))
    except ValueError:
        return None


def _extraction_raw(doc: dict) -> dict:
    """``raw_data`` результата ``TypeDataExtractor`` документа (``{}``, если нет)."""
    raw = (_as_dict(doc.get("extraction")) or {}).get("raw_data")
    return raw if isinstance(raw, dict) else {}


def fill_general_info(fields: Sequence[dict], by_key: Dict[str, dict], documents: Dict[int, dict],
                      procurement: dict, data: Dict[str, Any], trace: Dict[str, Any]) -> None:
    """Заполняет общие сведения (блок «Экспертное заключение» и блок I), изменяя ``data``/``trace`` на месте.

    Источники по убыванию надёжности: XML извещения ЕИС (факты ``eis_xml``: идентификационный код,
    наименование объекта, НМЦК, аванс), паспорт закупки, данные ``TypeDataExtractor`` документов
    (предмет закупки, НМЦК) — последние помечаются ``proposed``. «Состав комплекта документации» собирается
    из загруженных документов. Финансирование в документах не определяется — остаётся эксперту.

    Args:
        fields: Поля формы.
        by_key: Факты по ключам (с разобранным ``value_obj``).
        documents: ``document_id`` → описание документа (``filename``, ``doc_code``, ``extraction``).
        procurement: Паспорт закупки.
        data: ``data`` (изменяется на месте).
        trace: ``trace`` (изменяется на месте).
    """
    kinds = {f["field_key"]: f["value_kind"] for f in fields}

    def xml_value(number: str) -> Tuple[Optional[str], Optional[str]]:
        """Значение и путь из доказательства факта XML по номеру критерия (``путь = значение``)."""
        for f in fields:
            if eis_notice.criterion_number(f.get("label", "")) != number:
                continue
            fact = by_key.get(f["field_key"])
            if fact and fact.get("source") == facts_mod.SOURCE_EIS and fact["result"] == 1 and " = " in (fact.get("quote") or ""):
                path, value = fact["quote"].split(" = ", 1)
                return value.strip(), path
        return None, None

    def put(key: str, value: Any, status: str, source: str, quote: Optional[str] = None) -> None:
        """Записывает значение, если ключ есть в форме и ещё не заполнен."""
        if key in data and data[key] is None and value not in (None, ""):
            data[key] = value
            trace[key] = {"status": status, "source": source, "quote": (quote or "")[:300] or None}

    docs = list(documents.values())
    notice_docs = sorted(docs, key=lambda d: d.get("doc_code") != "docIzvejenieFiles")

    # XML извещения
    name, _ = xml_value("1.11")
    put("name", name, "verified", facts_mod.SOURCE_EIS, name)
    code, _ = xml_value("1.8")
    put("inn", code, "verified", facts_mod.SOURCE_EIS, code)
    price, _ = xml_value("1.18")
    put("field1_1", _number(price), "verified", facts_mod.SOURCE_EIS, price)
    advance, path = xml_value("1.22")
    if advance and _number(advance) is not None:
        put("field1_2", _number(advance), "verified", facts_mod.SOURCE_EIS, advance)
        if "field1_2_unit" in data and data["field1_2_unit"] is None:
            data["field1_2_unit"] = "%" if "sumInPercents" in (path or "") else "руб."
            trace["field1_2_unit"] = {"status": "derived", "rule": "единица по полю XML извещения"}

    # паспорт закупки
    for key, column in (("field1_1", "nmck"), ("field1_2", "advance")):
        if key in data and data[key] is None and procurement.get(column) is not None:
            data[key] = float(procurement[column])
            trace[key] = {"status": "from_passport", "source": "pe_procurements"}

    # данные, извлечённые из документов (предложение модели)
    for doc in notice_docs:
        raw = _extraction_raw(doc)
        subject = (raw.get("procurement_subject") or {}).get("description")
        put("name", (subject or "").strip()[:500], "proposed", doc.get("filename") or "extraction", subject)
        for item in raw.get("finances") or []:
            if isinstance(item, dict) and re.search(r"нмцк|начальн", str(item.get("context") or ""), re.I):
                put("field1_1", _number(item.get("value")), "proposed", doc.get("filename") or "extraction",
                    (item.get("evidence") or {}).get("fragment") if isinstance(item.get("evidence"), dict) else None)

    # шифр документа (`code`) — реестровый номер закупки в ЕИС (19 цифр): из имени файла или текста извещения
    for doc in notice_docs + docs:
        match = (re.search(r"(?<!\d)(0\d{18})(?!\d)", doc.get("filename") or "")
                 or re.search(r"(?<!\d)(0\d{18})(?!\d)", (doc.get("text") or "")[:20000]))
        if match:
            put("code", match.group(1), "derived", doc.get("filename") or "notice", match.group(1))
            break

    # наименование и идентификационный код из текста печатной формы извещения (если XML нет)
    for doc in notice_docs:
        text = doc.get("text") or ""
        match = re.search(r"Наименование объекта закупки[\s:|\-–—]*([^\n|]{5,300})", text, re.I)
        if match:
            put("name", match.group(1).strip(), "proposed", doc.get("filename") or "notice", match.group(0).strip())
        match = re.search(r"(?<!\d)(\d{36})(?!\d)", text)
        if match:
            put("inn", match.group(1), "proposed", doc.get("filename") or "notice", match.group(1))

    # состав комплекта документации
    if "documents" in data and data["documents"] is None and docs:
        try:
            from evaluate_documents.type_data_extractor import DOCUMENT_TYPE_MAPPING as titles
        except Exception:  # noqa: BLE001 — справочник названий нужен только для подписи
            titles = {}
        lines, seen = [], set()
        for doc in docs:
            line = f"{titles.get(doc.get('doc_code'), doc.get('doc_code') or 'Документ')}: {doc.get('filename') or '—'}"
            if line not in seen:
                seen.add(line)
                lines.append(line)
        data["documents"] = "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))
        trace["documents"] = {"status": "derived", "rule": "список загруженных документов экспертизы"}

    # прочие мета/числовые/текстовые поля — только из подтверждённых фактов
    for key, fact in by_key.items():
        if key in data and kinds.get(key) in (forms.KIND_META, forms.KIND_NUMBER, forms.KIND_TEXT) \
                and data[key] is None and fact["verified"] and fact["value_obj"].get("value") not in (None, ""):
            data[key] = fact["value_obj"]["value"]
            trace[key] = {"status": "verified", "source": fact.get("source"), "quote": fact.get("quote")}


def collect_remarks(fields: Sequence[dict], data: Dict[str, Any], trace: Dict[str, Any]) -> List[dict]:
    """Список проверенных замечаний (критерии со значением ``0``) для блоков III–IV.

    Args:
        fields: Поля формы.
        data: Собранный ``data``.
        trace: Собранный ``trace``.

    Returns:
        list[dict]: ``{"field_key", "criterion", "comment", "quote"}`` в порядке полей формы.
    """
    remarks = []
    for field in fields:
        key = field["field_key"]
        if field["value_kind"] in RESULT_KINDS and data.get(key) == 0:
            item = trace.get(key, {})
            remarks.append({"field_key": key, "number": eis_notice.criterion_number(field["label"]) or "",
                            "criterion": field["label"], "comment": item.get("comment"),
                            "quote": (item.get("quote") or "")[:300]})
    return remarks


LAW_44FZ = ("Федерального закона от 05.04.2013 № 44-ФЗ «О контрактной системе в сфере закупок товаров, работ, услуг "
            "для обеспечения государственных и муниципальных нужд»")
PROCEDURES = (("competition", "открытого конкурса в электронной форме"), ("auction", "электронного аукциона"),
              ("quotation", "запроса котировок в электронной форме"), ("single", "закупки у единственного поставщика"))


def procedure_phrase(form_code: Optional[str]) -> Optional[str]:
    """Способ определения поставщика в родительном падеже по коду формы (``44fz_competition_obj6`` → «открытого конкурса…»)."""
    for marker, phrase in PROCEDURES:
        if marker in (form_code or ""):
            return phrase
    return None


def _title(label: str) -> str:
    """Название критерия без номера."""
    return re.sub(r"^\s*\d+(?:\.\d+)*\.?\s*", "", label or "").strip().rstrip(" .")


def fallback_block(key: str, remarks: Sequence[dict], stats: Dict[str, int], name: Optional[str] = None,
                   number: Optional[str] = None, procedure: Optional[str] = None) -> str:
    """Детерминированный текст блока III/IV по образцу экспертного заключения (если LLM недоступна).

    * ``field3`` («III. Вывод»): абзац об извещении («… соответствует требованиям законодательства[, за исключением: …]»)
      и перечень «Выявлены несоответствия и недостатки по критериям:» со строками ``номер «название». суть``.
    * ``field4`` («IV. Заключение»): «Извещение и документация о проведении <способ> для закупки № <номер> на <предмет>
      соответствуют требованиям законодательства, за исключением указанных несоответствий и недостатков.
      Заказчику рекомендуется: - …»; без замечаний — «… соответствуют …, целесообразно оформить документацию …».
      Самостоятелен, без ссылок на другие блоки.

    Args:
        key: ``field3`` или ``field4``.
        remarks: Проверенные замечания (``number``, ``criterion``, ``comment``).
        stats: Счётчики (``checked``, ``remarks``).
        name: Наименование объекта закупки.
        number: Реестровый номер закупки (шифр).
        procedure: Способ определения поставщика в родительном падеже.

    Returns:
        str: Текст блока.
    """
    num = f" № {number}" if number else ""
    if key == "field3":
        notice = [r for r in remarks if (r.get("number") or "").startswith("1.")]
        other = [r for r in remarks if r not in notice]
        if notice:
            items = "; ".join(((r.get("comment") or "").strip().rstrip(".") or _title(r["criterion"])) for r in notice)
            head = f"Информация, представленная в извещении{num}, соответствует требованиям законодательства, за исключением: {items}."
        else:
            head = f"Информация, представленная в извещении{num}, соответствует требованиям законодательства."
        if not other:
            return head + ("" if notice else " Документация о проведении закупки соответствует требованиям действующего законодательства Российской Федерации.")
        lines = [f"{r.get('number') or ''} «{_title(r['criterion'])}». {(r.get('comment') or '').strip()}".strip() for r in other]
        return head + "\nВыявлены несоответствия и недостатки по критериям:\n" + "\n".join(lines)
    subject = f"Извещение и документация о проведении {procedure} для закупки{num}" if procedure else f"Извещение и документация о проведении закупки{num}"
    if name:
        subject += f" на {name.strip().rstrip('.')}"
    if not remarks:
        return (f"{subject} соответствуют требованиям законодательства. На основании представленной документации "
                f"целесообразно оформить документацию и осуществить закупку в соответствии с требованиями {LAW_44FZ}.")
    todo = "\n".join(f"- устранить выявленное несоответствие по критерию {r.get('number') or _title(r['criterion'])};" for r in remarks)
    return (f"{subject} соответствуют требованиям законодательства, за исключением указанных несоответствий и недостатков.\n"
            f"Заказчику рекомендуется:\n{todo.rstrip(';')}.")


async def write_blocks(fields: Sequence[dict], data: Dict[str, Any], trace: Dict[str, Any],
                       llm_call: Optional[LlmCall], form_code: Optional[str] = None) -> Dict[str, Any]:
    """Заполняет ``field3`` и ``field4`` (если они есть в форме): LLM видит только проверенные результаты.

    При отсутствии замечаний LLM не вызывается. Сбой или пустой ответ модели заменяется
    детерминированным текстом; это отмечается в ``trace``.

    Args:
        fields: Поля формы.
        data: ``data`` (изменяется на месте).
        trace: ``trace`` (изменяется на месте).
        llm_call: Функция ``(messages, schema) -> str`` или ``None`` (тогда используется шаблон).
        form_code: Код формы — по нему определяется способ закупки для заключения.

    Returns:
        dict: Счётчики ``{"checked", "remarks"}``, использованные при формулировке.
    """
    remarks = collect_remarks(fields, data, trace)
    checked = sum(1 for f in fields if f["value_kind"] in RESULT_KINDS and data.get(f["field_key"]) is not None)
    stats = {"checked": checked, "remarks": len(remarks)}
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    procedure = procedure_phrase(form_code)
    for key in ("field3", "field4"):
        if key not in data:
            continue
        text, how = None, "template"
        if llm_call is not None and remarks:
            payload = {"блок": BLOCK_TITLES[key], "объект закупки": data.get("name"), "номер закупки": data.get("code"),
                       "способ определения поставщика": procedure, "счётчики": stats, "замечания": remarks}
            messages = [{"role": "system", "content": prompt},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
            try:
                parsed = json.loads(await llm_call(messages, TEXT_BLOCK_SCHEMA))
                candidate = (parsed.get("text") or "").strip() if isinstance(parsed, dict) else ""
                if candidate:
                    text, how = candidate, "llm"
            except Exception as e:  # noqa: BLE001 — блок не должен ронять сборку
                logger.warning(f"knowledge_store: блок {key} сформирован по шаблону: {type(e).__name__}")
        data[key] = text or fallback_block(key, remarks, stats, data.get("name"), data.get("code"), procedure)
        trace[key] = {"status": "generated", "method": how, "based_on": [r["field_key"] for r in remarks]}
    return stats


def validate(data: Dict[str, Any], fields: Sequence[dict]) -> List[str]:
    """Проверяет, что ``data`` соответствует форме: только её ключи и допустимые типы значений.

    Args:
        data: Собранный ``data``.
        fields: Поля формы.

    Returns:
        list[str]: Найденные проблемы (пусто, если всё в порядке).
    """
    problems = []
    kinds = {f["field_key"]: f["value_kind"] for f in fields}
    for key in data:
        if key not in kinds:
            problems.append(f"ключ вне формы: {key}")
    for key, kind in kinds.items():
        if key not in data:
            problems.append(f"нет ключа формы: {key}")
            continue
        value = data[key]
        if value is None:
            continue
        if kind in RESULT_KINDS and value not in (0, 1, 2):
            problems.append(f"{key}: недопустимое значение {value!r}")
        if kind == forms.KIND_SECTION and value not in (0, 1):
            problems.append(f"{key}: недопустимый итог подраздела {value!r}")
        if kind == forms.KIND_TEXT and not isinstance(value, str):
            problems.append(f"{key}: ожидается текст")
    return problems


async def precheck(conn, expertise_id: int) -> Dict[str, Any]:
    """Проверяет, что сводное ЭЗ можно строить: есть документы, тексты не очищены, форма поддерживается.

    Args:
        conn: Соединение ``asyncpg``.
        expertise_id: ID экспертизы.

    Returns:
        dict: ``{"form_code", "passport", "documents"}``.

    Raises:
        NoDataError: Документов в хранилище нет («сначала /evaluate-documents»).
        TextPurgedError: Все тексты очищены политикой хранения («перезапустите /evaluate-documents»).
        UnsupportedFormError: Для экспертизы нет формы в справочнике.
    """
    docs = await repo.get_documents_brief(conn, expertise_id)
    if not docs:
        raise NoDataError("По экспертизе нет данных в хранилище знаний — сначала выполните /evaluate-documents")
    if not any(d["has_text"] for d in docs):
        raise TextPurgedError("Тексты документов удалены по сроку хранения — перезапустите /evaluate-documents")
    passport = await repo.get_procurement(conn, expertise_id)
    if not passport:
        raise NoDataError("Нет паспорта закупки — сначала выполните /evaluate-documents")
    code = forms.get_form_code(passport["law"], passport["check_type2"], passport["object_code"],
                               available=await repo.list_form_codes(conn))
    if not code:
        raise UnsupportedFormError(
            f"Форма заключения не поддерживается ({passport['law']}, checkType2={passport['check_type2']}, "
            f"объект {passport['object_code']})")
    await repo.set_form_code(conn, expertise_id, code)
    return {"form_code": code, "passport": passport, "documents": docs}


async def generate_summary(conn, expertise_id: int, llm_call: Optional[LlmCall],
                           build_facts: Optional[FactsBuilder] = None, rebuild_facts: bool = False) -> Dict[str, Any]:
    """Собирает и сохраняет сводное ЭЗ экспертизы.

    Args:
        conn: Соединение ``asyncpg``.
        expertise_id: ID экспертизы.
        llm_call: Функция вызова LLM для блоков III–IV (``None`` — только шаблонные тексты).
        build_facts: Корутина-фабрика построения фактов; вызывается, если фактов ещё нет (или ``rebuild_facts``).
        rebuild_facts: Пересчитать факты перед сборкой, даже если они уже есть.

    Returns:
        dict: ``summary_id``, ``form_code``, ``status`` (``draft`` / ``needs_review``), ``data``
        (ровно ключи формы), ``trace`` (доказательства отдельно), ``stats``, ``problems``.

    Raises:
        SummaryError: См. :func:`precheck`; :class:`NoFactsError`, если фактов нет и построить их нечем.
    """
    ready = await precheck(conn, expertise_id)
    code = ready["form_code"]
    fact_rows = [dict(r) for r in await repo.get_facts(conn, expertise_id)]
    if (not fact_rows or rebuild_facts) and build_facts is not None:
        await build_facts()
        fact_rows = [dict(r) for r in await repo.get_facts(conn, expertise_id)]
    if not fact_rows:
        raise NoFactsError("Факты по экспертизе ещё не построены — повторите запрос позже")

    fields = [dict(r) for r in await repo.get_form_fields(conn, code)]
    documents = {d["id"]: {"filename": d["filename"], "doc_code": d["doc_code"], "text": d["text_full"],
                           "extraction": d.get("extraction")}
                 for d in ready["documents"]}
    data, trace = assemble(fields, fact_rows, documents, ready["passport"])
    stats = await write_blocks(fields, data, trace, llm_call, code)
    problems = validate(data, fields)
    if problems:
        logger.error(f"knowledge_store: сводное ЭЗ {expertise_id} не соответствует форме: {problems[:5]}")
    unresolved = [f["field_key"] for f in fields
                  if f["value_kind"] in RESULT_KINDS and data.get(f["field_key"]) is None]
    proposed = [k for k, v in trace.items() if isinstance(v, dict) and v.get("status") == "proposed"]
    status = "needs_review" if unresolved or proposed or problems else "draft"
    stats.update({"fields": len(fields), "unresolved": len(unresolved), "proposed": len(proposed)})
    trace["_summary"] = {"unresolved": unresolved, "proposed": proposed, "problems": problems}
    async with conn.transaction():
        summary_id = await repo.insert_summary(conn, expertise_id, code, data, trace, status)
        await repo.touch_expiry(conn, expertise_id, Config.PE_TEXT_RETENTION_DAYS)
    logger.info(f"knowledge_store: сводное ЭЗ {expertise_id} ({code}): {status}, {stats}")
    return {"summary_id": summary_id, "form_code": code, "status": status, "data": data, "trace": trace,
            "stats": stats, "problems": problems}
