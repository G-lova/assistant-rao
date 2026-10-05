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

# Способ закупки по checkType2 из queries/evaluate_docs_script.sql (только 44-ФЗ-способы).
METHOD_BY_CHECK_TYPE2 = {
    1: "competition", 2: "competition", 3: "competition",
    4: "auction", 5: "auction", 6: "auction",
    7: "quotation", 8: "quotation",
    11: "single_supplier",
}

LAW_PREFIX = {"44-ФЗ": "44fz", "223-ФЗ": "223fz"}

# Типы значений поля (pe_form_fields.value_kind)
KIND_PRESENCE = "presence"        # 1 / 0 / 2 — в наличии / отсутствует / не предусмотрено
KIND_COMPLIANCE = "compliance"    # 1 / 0 / 2 — соответствует / не соответствует / не применимо
KIND_SECTION = "section"          # итог подраздела (у ключа есть дочерние), 0/1 или bool
KIND_CHOICE = "choice"            # выбор из списка (метод обоснования НМЦК)
KIND_NUMBER = "number"
KIND_TEXT = "text"                # комментарии, вывод, заключение
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


def method_code_from_title(title: str) -> Optional[str]:
    """Определяет код способа закупки по названию формы из Excel.

    Args:
        title: Название формы, например ``Конкурс новый 44-ФЗ``.

    Returns:
        ``competition`` / ``auction`` / ``quotation`` / ``single_supplier`` или ``None``.
    """
    low = (title or "").lower()
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
        method = METHOD_BY_CHECK_TYPE2.get(int(check_type2))
        obj = int(object_code)
    except (TypeError, ValueError):
        return None
    if law not in LAW_PREFIX or not method:
        return None
    code = build_form_code(law, method, obj)
    if available is not None and code not in set(available):
        return None
    return code


def _cell(ws, row: int, col: int):
    """Значение ячейки (``str`` без пробелов по краям или ``None``)."""
    if col < 1:
        return None
    value = ws.cell(row, col).value
    if value is None:
        return None
    text = str(value).strip()
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
    if key in ("field3", "field4") or key.endswith("_text"):
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


def parse_forms_sheet(ws) -> Dict[str, List[FormField]]:
    """Разбирает лист «Все Закупки 44-фз» в справочник полей.

    Args:
        ws: Лист ``openpyxl`` (``data_only=True``).

    Returns:
        Словарь ``код формы → список FormField`` в порядке следования в Excel
        (вместе с производными ключами). Блоки без распознаваемого способа закупки пропускаются.
    """
    result: Dict[str, List[FormField]] = {}
    for block in _find_blocks(ws):
        method = method_code_from_title(block["title"])
        if not method:
            continue
        form_code = build_form_code("44-ФЗ", method, block["object_code"])
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
