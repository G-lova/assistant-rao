"""Поиск предыдущей версии файла через сравнение XML-документов ЕИС.

Файлы закупки приходят как вложения XML (``attachments``) и печатная форма самого XML
(``printFormInfo`` / ``printForm``). Новая редакция документа — это новая версия XML, в которой
вложение заменено (другая ссылка, то же имя или описание). Поэтому предыдущая версия файла ищется так:

1. **XML файла**: ``risk_monitoring_files.xml_source_id``; если привязки нет — XML с тем же номером
   (``owner_number`` = номер закупки или контракта), той же версией и группой (извещение / контракт),
   в чьих вложениях есть ссылка файла.
2. **Предыдущая версия XML** — ``xml_previous.sql`` (тот же документ с меньшей версией).
3. **Diff вложений** (``xml_events.document_changes``):

   * файл заменён (``replaced``) → предыдущая версия — файл со ссылкой ``before``;
   * ссылка есть в обеих версиях (``unchanged``) → файл не менялся;
   * файл добавлен (``added``) → предыдущей версии нет, документ новый;
   * печатная форма → печатная форма предыдущего XML (по её ссылке).

4. Файл предыдущей версии в ``risk_monitoring_files`` — по ``source_url``, иначе по ``xml_source_id``
   предыдущего XML и имени файла.

Чистые функции модуля покрывают тесты ``tests/test_rm_quality.py``; запросы к БД и чтение архивов —
в ``AIFileAnalyzer.fetch_previous_versions``.
"""
import os
import re
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlparse

from risk_monitoring import doc_kind, xml_events


def _int(v: Any) -> Optional[int]:
    """Целое из значения БД (``2``, ``"2"``, ``2.0``) или ``None``."""
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def url_key(url: Optional[str]) -> str:
    """Ключ ссылки для сравнения: хост без ``www``, путь и отсортированные параметры, без регистра.

    Ссылки ЕИС одного файла могут отличаться схемой и порядком параметров (``uid``, ``regNumber``).

    Args:
        url: Ссылка.

    Returns:
        str: Ключ (пустая строка для пустой ссылки).
    """
    if not url:
        return ""
    p = urlparse(url.strip())
    host = (p.netloc or "").lower().removeprefix("www.")
    query = "&".join(f"{k.lower()}={v[0]}" for k, v in sorted(parse_qs(p.query).items()) if v)
    return f"{host}{p.path.rstrip('/').lower()}?{query}"


def name_key(name: Optional[str]) -> str:
    """Ключ имени файла: без расширения, регистра и лишних пробелов.

    Args:
        name: Имя файла.

    Returns:
        str: Ключ.
    """
    base = os.path.splitext((name or "").strip())[0] if re.search(r"\.[A-Za-z0-9]{2,5}$", name or "") else (name or "")
    return re.sub(r"\s+", " ", base).strip().lower()


def print_form_urls(body: Optional[Dict[str, Any]]) -> List[str]:
    """Ссылки на печатную форму XML (``printFormInfo.url``, ``printForm.url``, ``extPrintFormInfo.url``).

    Args:
        body: Нормализованное тело XML.

    Returns:
        List[str]: Ссылки без повторов.
    """
    out: List[str] = []

    def visit(node: Any, key: str = "") -> None:
        """Рекурсивный обход: url под узлом, имя которого содержит printForm."""
        if isinstance(node, dict):
            if "printform" in key.lower() and isinstance(node.get("url"), str) and node["url"] not in out:
                out.append(node["url"])
            for k, v in node.items():
                visit(v, k)
        elif isinstance(node, list):
            for v in node:
                visit(v, key)

    visit(body or {})
    return out


def locate(file_row: Dict[str, Any], atts: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Вложение XML, соответствующее файлу: по ссылке, иначе по имени файла.

    Args:
        file_row: Строка ``risk_monitoring_files`` (``source_url``, ``file_name``).
        atts: Вложения XML (``xml_events.attachments``).

    Returns:
        Optional[Dict[str, Any]]: Вложение или ``None``.
    """
    atts = list(atts)
    key = url_key(file_row.get("source_url"))
    if key:
        hit = next((a for a in atts if url_key(a.get("url")) == key), None)
        if hit:
            return hit
    name = name_key(file_row.get("file_name"))
    hits = [a for a in atts if name and name_key(a.get("file_name")) == name]
    return hits[0] if len(hits) == 1 else None


def contains(body: Optional[Dict[str, Any]], file_row: Dict[str, Any]) -> bool:
    """Есть ли файл среди вложений или печатных форм XML (проверка кандидата при поиске по номеру).

    Args:
        body: Нормализованное тело XML.
        file_row: Строка файла.

    Returns:
        bool: ``True``, если ссылка файла найдена.
    """
    key = url_key(file_row.get("source_url"))
    if not key or not body:
        return False
    urls = [a["url"] for a in xml_events.attachments(body)] + print_form_urls(body)
    return any(url_key(u) == key for u in urls)


def previous_ref(file_row: Dict[str, Any], body: Dict[str, Any], prev_body: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Как найти предыдущую версию файла по diff вложений двух версий XML.

    Args:
        file_row: Строка файла (``file_name``, ``source_url``).
        body: Текущая версия XML.
        prev_body: Предыдущая версия XML (``None`` — её нет).

    Returns:
        Dict[str, Any]: ``match`` — ``print_form`` / ``replaced`` / ``unchanged`` / ``added`` /
        ``no_previous_xml`` / ``not_in_xml``; ``urls`` — ссылки предыдущей версии файла;
        ``file_name`` — имя файла в предыдущей версии; ``print_form`` — признак печатной формы.
    """
    is_print = doc_kind.is_print_form(file_row) or any(
        url_key(u) == url_key(file_row.get("source_url")) for u in print_form_urls(body) if file_row.get("source_url"))
    if prev_body is None:
        return {"match": "no_previous_xml", "urls": [], "print_form": is_print}
    if is_print:
        return {"match": "print_form", "urls": print_form_urls(prev_body), "print_form": True}
    atts = xml_events.attachments(body)
    att = locate(file_row, atts)
    if att is None:
        return {"match": "not_in_xml", "urls": [], "print_form": False}
    changes = xml_events.document_changes(xml_events.attachments(prev_body), atts)
    for r in changes["replaced"]:
        if url_key(r["after"]["url"]) == url_key(att["url"]):
            return {"match": "replaced", "urls": [r["before"]["url"]], "file_name": r["before"].get("file_name"),
                    "print_form": False}
    if any(url_key(a["url"]) == url_key(att["url"]) for a in changes["added"]):
        return {"match": "added", "urls": [], "print_form": False}
    return {"match": "unchanged", "urls": [att["url"]], "file_name": att.get("file_name"), "print_form": False}


def pick_previous(ref: Dict[str, Any], current: Dict[str, Any], candidates: Iterable[Dict[str, Any]],
                  prev_xml_id: Optional[int]) -> Optional[Dict[str, Any]]:
    """Файл предыдущей версии среди строк ``risk_monitoring_files``.

    Порядок: по ссылке из ``ref``; затем файл предыдущего XML с тем же именем (``ref.file_name`` или
    именем текущего файла); для печатной формы — печатная форма предыдущего XML.

    Args:
        ref: Результат ``previous_ref``.
        current: Текущий файл.
        candidates: Строки ``files_by_ref.sql``.
        prev_xml_id: id предыдущего XML.

    Returns:
        Optional[Dict[str, Any]]: Строка файла или ``None``.
    """
    if ref["match"] in ("added", "not_in_xml", "no_previous_xml"):
        return None
    cur_id = _int(current["id"])
    pool = [c for c in candidates if _int(c["id"]) != cur_id]
    keys = {url_key(u) for u in ref.get("urls") or []}
    by_url = [c for c in pool if keys and url_key(c.get("source_url")) in keys]
    if by_url:
        return max(by_url, key=lambda c: _int(c.get("eis_version")) or 0)
    same_xml = [c for c in pool if prev_xml_id is not None and _int(c.get("xml_source_id")) == int(prev_xml_id)]
    if ref.get("print_form"):
        forms = [c for c in same_xml if doc_kind.is_print_form(c)]
        return forms[0] if len(forms) == 1 else None
    names = {name_key(ref.get("file_name")), name_key(current.get("file_name"))} - {""}
    by_name = [c for c in same_xml if name_key(c.get("file_name")) in names]
    return by_name[0] if len(by_name) == 1 else None


def choose_xml(file_row: Dict[str, Any], candidates: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Кандидаты XML для файла без ``xml_source_id`` (найдены по ``owner_number``), лучшие первыми.

    Отбор: группа XML совпадает с владельцем файла (извещение / контракт); версия равна версии файла,
    затем ближайшая меньшая. Окончательно кандидат подтверждается ``contains`` после чтения XML.

    Args:
        file_row: Строка файла (``owner_type``, ``eis_version``).
        candidates: Строки ``xml_by_owner.sql``.

    Returns:
        List[Dict[str, Any]]: Отсортированные кандидаты.
    """
    owner = (file_row.get("owner_type") or "").lower()
    version = _int(file_row.get("eis_version"))
    out = []
    for c in candidates:
        group = doc_kind.xml_group(c.get("tag") or c.get("xml_source_type"))
        if owner in ("notice", "contract") and group not in (owner, "other"):
            continue
        v = _int(c.get("eis_version"))
        if version is not None and v is not None and v > version:
            continue
        out.append(c)
    def rank(c: Dict[str, Any]):
        """Сначала та же версия, затем ближайшая меньшая, затем XML той же группы, что владелец."""
        v = _int(c.get("eis_version"))
        same = version is not None and v == version
        group_ok = doc_kind.xml_group(c.get("tag") or c.get("xml_source_type")) == owner
        return (not same, -(v if v is not None else -1), not group_ok)
    return sorted(out, key=rank)
