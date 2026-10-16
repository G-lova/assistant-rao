"""Справочник форм сводного экспертного заключения («РАО Эксперт»).

Модуль читает лист «Все Закупки 44-фз» из файла «Отчеты и закупки поля.xlsx» и превращает
его в список полей по каждой форме (конкурс / аукцион / запрос котировок / единственный
поставщик, «Объект 6»). Ключи полей (``field2_1_1`` …) **не хардкодятся**: они берутся только
из Excel, потому что у разных форм и лет нумерация разная.

Из Excel берутся только ключи, явно записанные в файле. Дополнительно достраиваются
производные ключи, которых в Excel нет, но которые встречаются в ``expertise_expert_opinion7s.data``:
комментарии ``<ключ>_text``, родительский ключ подраздела (``field2_2_4``) и служебные ключи
:data:`SERVICE_FIELDS`.

Модуль не зависит от ``asyncpg`` и сети, поэтому полностью тестируется локально.
"""
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

# Служебные ключи заключения, которых нет в Excel (редактор заключения добавляет их сам).
SERVICE_FIELDS = {
    "rao_comment": "Комментарий РАО",
    "field1_2_unit": "Единица измерения аванса (руб./%)",
}

# Ключевые слова в названии формы → код способа закупки. Это не ключи полей, а идентификаторы форм.
METHOD_BY_KEYWORD = (
    ("конкурс", "competition"),
    ("аукцион", "auction"),
    ("котировок", "quotation"),
    ("ед поставщик", "single_supplier"),
)

# Способ закупки по checkType2 из queries/evaluate_docs_script.sql (44-ФЗ-способы).
METHOD_BY_CHECK_TYPE2 = {
    1: "competition", 2: "competition", 3: "competition",
    4: "auction", 5: "auction", 6: "auction",
    7: "quotation", 8: "quotation",
    11: "single_supplier",
}

# 223-ФЗ: в Excel две формы — «Новый (конкурс, аукцион, котировки)» и «Ед поставщик Новый». Запрос предложений (9, 10)
# проводится по тому же шаблону, что и конкурентные способы. «Иные способы» (12) и отчётные документы (13–15) формы не имеют.
METHOD_BY_CHECK_TYPE2_223 = {**{n: "competitive" for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)}, 11: "single_supplier"}
OBJECT_223_FORMS = 6          # «Документация о проведении закупки» — единственный объект, для которого в Excel есть формы 223-ФЗ

LAW_PREFIX = {"44-ФЗ": "44fz", "223-ФЗ": "223fz"}

# Объект 7 — «Отчетные документы по контракту/договору». Вид отчётных документов задаёт checkType2, а не способ закупки:
# 13 — НИР, 14 — поставка товара, 15 — выполнение работ/оказание услуг. Форма одна на оба закона (лист Excel «223-фз и 44-фз»).
OBJECT_REPORT = 7
METHOD_BY_CHECK_TYPE2_REPORT = {13: "nir", 14: "goods", 15: "works"}
# Начало названия листа Excel (названия обрезаны до 31 символа) → код вида отчётных документов.
REPORT_SHEETS = (("Сравнение НИР", "nir"), ("Сравнение Выполнение работ", "works"), ("Сравнение Поставка товаров", "goods"))

# Типы значений поля (pe_form_fields.value_kind)
KIND_PRESENCE = "presence"        # 1 / 0 / 2 — в наличии / отсутствует / не предусмотрено
KIND_COMPLIANCE = "compliance"    # 1 / 0 / 2 — соответствует / не соответствует / не применимо
KIND_SECTION = "section"          # итог подраздела (у ключа есть дочерние), 0/1 или bool
KIND_CHOICE = "choice"            # выбор из списка (метод обоснования НМЦК)
KIND_NUMBER = "number"
KIND_TEXT = "text"                # комментарии, вывод, заключение
KIND_BOOL = "bool"                # да/нет (формы объекта 7: «Представлена документация по приемке?»)
KIND_META = "meta"                # шифр, наименование, идентификационный код, состав документов

_KEY_RE = re.compile(r"^(field\d+(?:_\d+)*(?:_text)?|code|name|inn|documents)$")


@dataclass
class FormField:
    """Одно поле формы заключения.

    Attributes:
        form_code: Код формы, например ``44fz_competition_obj6``.
        field_key: Ключ поля в ``data`` (``field2_1_1``).
        ordinal: Порядковый номер поля в форме (порядок вывода).
        label: Название поля (текст критерия).
        section: Блок/раздел/подраздел, к которому относится поле.
        value_kind: Тип значения (``KIND_*``).
        check_level: Кто проверяет: 1 — код/XML, 2 — ИИ, 3 — эксперт; ``None`` — не проверяется.
        derived: ``True``, если ключа нет в Excel и он достроен по правилам модуля.
    """

    form_code: str
    field_key: str
    ordinal: int
    label: str
    section: Optional[str]
    value_kind: str
    check_level: Optional[int] = None
    derived: bool = False


def method_code_from_title(title: str, law: str = "44-ФЗ") -> Optional[str]:
    """Определяет код способа закупки по названию формы из Excel.

    Args:
        title: Название формы, например ``Конкурс новый 44-ФЗ`` или ``223-ФЗ Новый (конкурс, аукцион, котировки)``.
        law: ``44-ФЗ`` или ``223-ФЗ``. Для 223-ФЗ одна форма обслуживает несколько способов, поэтому она называется
            ``competitive`` (а не по первому слову названия), единственный поставщик — ``single_supplier``.

    Returns:
        ``competition`` / ``auction`` / ``quotation`` / ``single_supplier`` (44-ФЗ), ``competitive`` /
        ``single_supplier`` (223-ФЗ) или ``None``.
    """
    low = (title or "").lower()
    if law == "223-ФЗ":
        if "ед поставщик" in low or "единственн" in low:
            return "single_supplier"
        return "competitive" if any(w in low for w in ("конкурс", "аукцион", "котировк")) else None
    for word, code in METHOD_BY_KEYWORD:
        if word in low:
            return code
    return None


def build_form_code(law: str, method: str, object_code: int) -> str:
    """Собирает код формы.

    Args:
        law: ``44-ФЗ`` или ``223-ФЗ``.
        method: Код способа (``competition`` …).
        object_code: Код объекта экспертизы (5/6/7).

    Returns:
        Строка вида ``44fz_competition_obj6``.
    """
    return f"{LAW_PREFIX[law]}_{method}_obj{int(object_code)}"


def get_form_code(law: Optional[str], check_type2: Optional[int], object_code: Optional[int],
                  available: Optional[Iterable[str]] = None) -> Optional[str]:
    """Выбирает форму заключения по данным экспертизы.

    Использует те же поля, что уже отдаёт ``queries/evaluate_docs_script.sql``:
    ``law_reference``, ``checkType2``, ``object``.

    Args:
        law: ``44-ФЗ`` / ``223-ФЗ`` (``law_reference`` из SQL).
        check_type2: Код вида проверки (``checkType2``).
        object_code: Код объекта экспертизы (``object``).
        available: Коды форм, загруженные в справочник. Если задано и форма не загружена,
            возвращается ``None`` (форма пока не поддерживается).

    Returns:
        Код формы или ``None``, если форму определить нельзя или она не поддерживается.
    """
    try:
        obj = int(object_code)
        if obj == OBJECT_REPORT:        # отчётные документы: вид задаёт checkType2 13–15, способ закупки не важен
            table = METHOD_BY_CHECK_TYPE2_REPORT
        else:
            table = METHOD_BY_CHECK_TYPE2_223 if law == "223-ФЗ" else METHOD_BY_CHECK_TYPE2
        method = table.get(int(check_type2))
    except (TypeError, ValueError):
        return None
    if law not in LAW_PREFIX or not method:
        return None
    code = build_form_code(law, method, obj)
    if available is not None and code not in set(available):
        return None
    return code


def _cell(ws, row: int, col: int):
    """Значение ячейки (``str`` в нормализованном виде NFC без пробелов по краям или ``None``).

    Нормализация нужна, потому что в Excel часть названий (лист 223-ФЗ) записана с комбинируемыми знаками («и» + U+0306
    вместо «й»), и по таким названиям не работает ни поиск по шаблону, ни сопоставление правил.
    """
    if col < 1:
        return None
    value = ws.cell(row, col).value
    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value)).strip()
    return text or None


def _find_blocks(ws, header_row: int = 1) -> List[dict]:
    """Находит в строке заголовка блоки форм.

    Блок — это ячейка «Объект N» (колонка ключей) и название формы, которое стоит
    на 4 колонки левее (колонка A/G/M/S в листе «Все Закупки 44-фз»).

    Returns:
        Список словарей ``{title, object_code, key_col, label_col, first_col}``.
    """
    blocks = []
    for col in range(1, ws.max_column + 1):
        value = _cell(ws, header_row, col)
        match = re.match(r"^Объект\s+(\d+)", value or "")
        if not match:
            continue
        title = None
        for left in range(col - 1, 0, -1):
            title = _cell(ws, header_row, left)
            if title:
                break
        blocks.append({"title": title, "object_code": int(match.group(1)),
                       "key_col": col, "label_col": col - 1, "first_col": max(col - 4, 1)})
    return blocks


def classify(key: str, label: str, has_children: bool) -> tuple:
    """Определяет тип значения и уровень проверки поля.

    Args:
        key: Ключ поля.
        label: Название критерия.
        has_children: Есть ли у ключа дочерние (``field2_2_1`` → ``field2_2_1_1``).

    Returns:
        Пара ``(value_kind, check_level)``.
    """
    if key in ("code", "name", "inn", "documents"):
        return KIND_META, None
    if key in ("field3", "field4") or key.endswith("_text") or re.match(r"(?i)\s*комментарий", label or ""):
        return KIND_TEXT, 3
    if key in ("field1_1", "field1_2"):
        return KIND_NUMBER, 1
    if key.startswith("field1_"):
        return KIND_TEXT, 1
    if key.endswith("_0"):
        return KIND_CHOICE, 2
    if has_children:
        return KIND_SECTION, 2
    if key.startswith("field2_1_"):
        return KIND_PRESENCE, 1
    if re.search(r"(?i)наличи", label or "") and not re.search(r"(?i)соответстви", label or ""):
        return KIND_PRESENCE, 2
    return KIND_COMPLIANCE, 2


def _find_blocks_223(ws, header_row: int = 2) -> List[dict]:
    """Находит блоки форм на листе «Все Закупки 223-фз».

    В отличие от 44-ФЗ, здесь нет ячеек «Объект N»: название формы стоит во второй строке в первой колонке блока
    (A и G), колонка названий критериев — через три колонки справа, колонка ключей — через четыре. Блок начинается
    с ячейки, в которой название формы начинается с «223».

    Returns:
        Список словарей ``{title, object_code, key_col, label_col, first_col}`` (объект — :data:`OBJECT_223_FORMS`).
    """
    blocks = []
    for col in range(1, ws.max_column + 1):
        title = _cell(ws, header_row, col)
        if title and title.startswith("223"):
            blocks.append({"title": title, "object_code": OBJECT_223_FORMS, "key_col": col + 4,
                           "label_col": col + 3, "first_col": col})
    return blocks


def parse_forms_sheet(ws, law: str = "44-ФЗ") -> Dict[str, List[FormField]]:
    """Разбирает лист «Все Закупки 44-фз» или «Все Закупки 223-фз» в справочник полей.

    Args:
        ws: Лист ``openpyxl`` (``data_only=True``).
        law: ``44-ФЗ`` (блоки находятся по ячейкам «Объект N») или ``223-ФЗ`` (по названию формы, см. :func:`_find_blocks_223`).

    Returns:
        Словарь ``код формы → список FormField`` в порядке следования в Excel
        (вместе с производными ключами). Блоки без распознаваемого способа закупки пропускаются.
    """
    result: Dict[str, List[FormField]] = {}
    for block in (_find_blocks_223(ws) if law == "223-ФЗ" else _find_blocks(ws)):
        method = method_code_from_title(block["title"], law)
        if not method:
            continue
        form_code = build_form_code(law, method, block["object_code"])
        raw = []  # (key, label, section)
        block_name = section = subsection = None
        for row in range(2, ws.max_row + 1):
            first = _cell(ws, row, block["first_col"])
            second = _cell(ws, row, block["first_col"] + 1)
            third = _cell(ws, row, block["first_col"] + 2)
            if first:
                block_name, section, subsection = first, None, None
            if second:
                section, subsection = second, None
            if third:
                subsection = third
            key = _cell(ws, row, block["key_col"])
            label = _cell(ws, row, block["label_col"])
            if not key or not _KEY_RE.match(key):
                continue
            path = " / ".join(x for x in (block_name, section, subsection) if x)
            raw.append((key, label or key, path or None))
        result[form_code] = _finalize(form_code, raw)
    return result


def _finalize(form_code: str, raw: List[tuple]) -> List[FormField]:
    """Классифицирует поля и достраивает производные ключи.

    Производные ключи: родитель подраздела (``field2_2_4``, если есть ``field2_2_4_1``, но нет
    самого ``field2_2_4``), комментарии ``<ключ>_text`` для всех критериев раздела 2 и
    служебные поля :data:`SERVICE_FIELDS`.
    """
    keys = {k for k, _, _ in raw}
    seen, fields = set(), []

    def has_children(key: str) -> bool:
        return any(other.startswith(key + "_") and re.match(r"^\d", other[len(key) + 1:])
                   for other in keys)

    def add(key, label, section, derived=False, force_kind=None):
        if key in seen:
            return
        seen.add(key)
        kind, level = classify(key, label, has_children(key))
        if force_kind:
            kind, level = force_kind
        fields.append(FormField(form_code, key, len(fields) + 1, label, section, kind, level, derived))

    # родители подразделов, не записанные в Excel (например, field2_2_4)
    parents = {}
    for key, label, section in raw:
        match = re.match(r"^(field2_2_\d+)_\d+$", key)
        if match and match.group(1) not in keys:
            parents.setdefault(match.group(1), section)

    for key, label, section in raw:
        parent = re.match(r"^(field2_2_\d+)_\d+$", key)
        if parent and parent.group(1) in parents and parent.group(1) not in seen:
            add(parent.group(1), f"Итог подраздела ({parent.group(1)})", section, derived=True)
        add(key, label, section)

    for key, label, section in list(raw) + [(p, f"Итог подраздела ({p})", s) for p, s in parents.items()]:
        if re.match(r"^field2_[234]_", key) and not key.endswith("_text"):
            add(f"{key}_text", f"Комментарий: {label}", section, derived=True)
    for key, label in SERVICE_FIELDS.items():
        add(key, label, "Служебные", derived=True,
            force_kind=(KIND_TEXT, None))
    return fields


# ---- объект 7: отчётные документы ---------------------------------------------------------------------------------
# Ключи массивных полей: field1_3[0]['name'], field1_3[0]['field4_0'].q2; булевы поля помечены в Excel «(bool)».
_REPORT_KEY_RE = re.compile(r"^field\d+(?:_\d+)*(?:\[0\]\['\w+'\])?(?:\.\w+)?$")
_BOOL_MARK_RE = re.compile(r"\s*\(bool\)\s*$", re.I)
_BOOL_LABEL_RE = re.compile(r"(?i)\?\s*$|^не представляется возможным|^стоимостное замечание|^сроки приемки соответствуют"
                            r"|^в документе/акте о приемке")
_NUMBER_LABEL_RE = re.compile(r"(?i)количество|ключевая ставка|цена контракта|итоговая сумма|сумма |стоимость фактически|процент")


def is_report_form(form_code: Optional[str]) -> bool:
    """Форма относится к объекту 7 (отчётные документы по контракту/договору).

    Args:
        form_code: Код формы, например ``44fz_nir_obj7``.

    Returns:
        bool: ``True`` для форм объекта 7.
    """
    return bool(form_code) and form_code.endswith(f"_obj{OBJECT_REPORT}")


def classify_report(key: str, label: str, marked_bool: bool = False) -> tuple:
    """Определяет тип значения и уровень проверки поля формы объекта 7.

    Автоматически (уровень 1) готовится только раздел 1: перечень документов и таблица по работам/товарам
    (наименование, требования к составу отчётной документации, представленные документы). Всё остальное
    (сроки, замечания, неустойка, приёмка, выводы) — оценка эксперта (уровень 3).

    Args:
        key: Ключ поля без пометки «(bool)».
        label: Название поля.
        marked_bool: В Excel у ключа стоит пометка «(bool)».

    Returns:
        Пара ``(value_kind, check_level)``.
    """
    level = 1 if key == "field1_1" or key.startswith("field1_3[0]['") and "field4" not in key and "field3" not in key \
        and "field7" not in key else 3
    if marked_bool or _BOOL_LABEL_RE.search(label or ""):
        return KIND_BOOL, level
    if key.startswith("field6_1_") or _NUMBER_LABEL_RE.search(label or "") and not key.startswith("field1_"):
        return KIND_NUMBER, level
    return KIND_TEXT, level


def _parse_report_rows(ws) -> List[tuple]:
    """Строки листа объекта 7 в порядке Excel (с повторами ключей): ``(ключ, название, раздел, помечен_bool)``.

    Блок «Новые поля» находится по ячейке «Объект 7 (N)» в первых двух строках: колонка ключей — она, название
    критерия — слева от неё, подраздел — ещё на две колонки левее, раздел — на три.
    """
    key_col = None
    for row in (1, 2):
        for col in range(1, ws.max_column + 1):
            if re.match(r"^Объект\s+7", _cell(ws, row, col) or ""):
                key_col = col
                break
        if key_col:
            break
    if not key_col:
        return []
    first = key_col - 3
    rows, section, subsection = [], None, None
    for r in range(3, ws.max_row + 1):
        head, sub = _cell(ws, r, first), _cell(ws, r, first + 1)
        if head:
            section, subsection = head, None
        if sub:
            subsection = sub
        raw_key = _cell(ws, r, key_col)
        if not raw_key:
            continue
        marked = bool(_BOOL_MARK_RE.search(raw_key))
        key = _BOOL_MARK_RE.sub("", raw_key).strip()
        if not _REPORT_KEY_RE.match(key):
            continue
        label = _cell(ws, r, key_col - 1) or key
        rows.append((key, label, " / ".join(x for x in (section, subsection) if x) or None, marked))
    return rows


def report_duplicates(ws) -> Dict[str, List[str]]:
    """Ключи, которые в листе встречаются у нескольких критериев с разными названиями (вероятные опечатки в Excel).

    Args:
        ws: Лист вида отчётных документов.

    Returns:
        dict: ``ключ → названия критериев`` (только ключи с разными названиями).
    """
    labels: Dict[str, List[str]] = {}
    for key, label, _, _ in _parse_report_rows(ws):
        labels.setdefault(key, []).append(label)
    return {k: v for k, v in labels.items() if len(set(v)) > 1}


def parse_report_sheet(ws, method: str) -> Dict[str, List[FormField]]:
    """Разбирает лист вида отчётных документов (НИР / работы-услуги / поставка товаров) в формы обоих законов.

    Args:
        ws: Лист ``openpyxl`` (``data_only=True``).
        method: Вид отчётных документов (``nir``, ``works``, ``goods``).

    Returns:
        dict: ``44fz_<method>_obj7`` и ``223fz_<method>_obj7`` → одинаковые списки полей. Ключ, повторённый у
        нескольких критериев, остаётся за первым (повторы выводит :func:`report_duplicates`). Пусто, если блока нет.
    """
    rows = _parse_report_rows(ws)
    if not rows:
        return {}
    result: Dict[str, List[FormField]] = {}
    for law in LAW_PREFIX:
        code = build_form_code(law, method, OBJECT_REPORT)
        fields, seen = [], set()
        for key, label, section, marked in rows:
            if key in seen:
                continue
            seen.add(key)
            kind, level = classify_report(key, label, marked)
            fields.append(FormField(code, key, len(fields) + 1, label, section, kind, level, False))
        for key, label in (("rao_comment", SERVICE_FIELDS["rao_comment"]),):
            fields.append(FormField(code, key, len(fields) + 1, label, "Служебные", KIND_TEXT, None, True))
        result[code] = fields
    return result
