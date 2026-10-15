"""Поле ``documents`` сводного ЭЗ: перечень документов экспертизы по названиям, указанным в самих документах.

Эксперт перечисляет документы так, как они названы внутри документа, с реквизитами (дата, номер, приложение), без
отнесения к типам: «Извещение о проведении открытого конкурса в электронной форме от 24.04.2024 №0311100030324000007»,
«Описание объекта закупки (Приложение 1)», «Протокол подведения итогов определения поставщика (подрядчика,
исполнителя) от 16.05.2024 №ИЭОК1».

Источники названия, по убыванию надёжности:

1. XML ЕИС (извещение, протоколы, разъяснения): вид документа по корневому элементу, номер и дата из XML.
2. Начало текста документа: модель называет название и реквизиты, значение принимается, только если его слова
   есть в самом начале документа (иначе ответ отбрасывается).
3. Первая содержательная строка с «заголовочным» словом (Приложение, Протокол, Описание, …).
4. Имя файла без расширения — последний резерв (в trace помечается).

Модуль не зависит от БД и сети; модель вызывается через переданную функцию ``llm_call``.
"""
import asyncio
import re
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from knowledge_store import facts as facts_mod

try:  # безопасный разбор XML (как в eis_notice)
    from defusedxml import ElementTree as ET
except ImportError:  # pragma: no cover
    import xml.etree.ElementTree as ET  # type: ignore

LlmCall = Callable[[List[dict], dict], Awaitable[str]]

HEAD_CHARS = 2500            # сколько символов начала документа видит модель
MAX_DOCS = 40                # длиннее перечень не строим
SKIP_FILE_RE = re.compile(r"^(ЭЗ_|view|eis_file|metadata)|\.pdf$", re.I)   # заключения экспертов и служебные файлы

# корневой элемент XML ЕИС → название документа (без номера и даты)
XML_TITLES = {
    "epNotificationEOK": "Извещение о проведении открытого конкурса в электронной форме",
    "epNotificationEF": "Извещение о проведении электронного аукциона",
    "epNotificationEZK": "Извещение о проведении запроса котировок в электронной форме",
    "epNotificationEP": "Извещение об осуществлении закупки у единственного поставщика",
    "epProtocolEOK2020Final": "Протокол подведения итогов определения поставщика (подрядчика, исполнителя)",
    "epProtocolEOK2020SecondSections": "Протокол рассмотрения и оценки вторых частей заявок на участие в открытом конкурсе в электронной форме",
    "epProtocolEOK2020FirstSections": "Протокол рассмотрения и оценки первых частей заявок на участие в открытом конкурсе в электронной форме",
    "epProtocolEOKSingleApp": "Протокол рассмотрения единственной заявки на участие в открытом конкурсе в электронной форме",
    "epClarificationDoc": "Разъяснение положений извещения об осуществлении закупки",
    "epProtocolCancel": "Извещение об отмене протокола",
}
# в перечень не включаем: итоги по лотам и проектам протоколов, не являющиеся документами закупки
XML_SKIP = ("fcsPlacementResult", "fcsProposalsResult", "epProtocolCancel")

TITLE_WORD_RE = re.compile(r"приложени|протокол|описани|обоснован|проект|требовани|порядок|извещени|разъяснени|"
                           r"техническ|инструкци|информационн|критери|положени|документаци|расчет|расчёт|смет", re.I)
DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
APPENDIX_RE = re.compile(r"приложени\w*\s*(?:№|N|No)?\s*(\d{1,2})", re.I)

TITLE_SCHEMA = {"type": "object", "properties": {"title": {"type": ["string", "null"], "maxLength": 300}},
                "required": ["title"], "additionalProperties": False}
TITLE_PROMPT = ("Ты помогаешь составить перечень документов экспертизы закупки. По началу документа и имени файла укажи "
                "НАЗВАНИЕ документа так, как оно записано в самом документе, вместе с реквизитами (дата, номер, номер "
                "приложения), если они там указаны. Не определяй тип документа, не переформулируй и не придумывай "
                "название: бери слова из текста. Если в тексте названия нет — верни null. Только JSON: {\"title\": ...}.")


def _local(tag: str) -> str:
    """Имя элемента без пространства имён."""
    return tag.rsplit("}", 1)[-1]


def _date_ru(value: Optional[str]) -> Optional[str]:
    """``2025-09-02T11:38:40+08:00`` → ``02.09.2025``."""
    match = DATE_RE.match(value or "")
    return f"{match.group(3)}.{match.group(2)}.{match.group(1)}" if match else None


def xml_info(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """Название документа ЕИС по XML: вид по корневому элементу, номер и дата из самого XML.

    Args:
        text: Текст XML.

    Returns:
        Optional[dict]: ``{"title", "stem", "version"}``, где ``title`` — ``«<Вид> от <дата> №<номер>»``
        (у извещения номер — номер закупки); ``None``, если это не XML ЕИС известного вида или документ
        не нужен в перечне (итоги по лотам и т.п.).
    """
    if not text or not text.lstrip().startswith("<?xml") and not text.lstrip().startswith("<"):
        return None
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except Exception:  # noqa: BLE001 — не XML или повреждён
        return None
    node = next((c for c in root if _local(c.tag) != "id"), root) if _local(root.tag) == "export" else root
    name = _local(node.tag)
    if name.startswith(XML_SKIP):
        return None
    stem = next((k for k in sorted(XML_TITLES, key=len, reverse=True) if name.startswith(k)), None)
    if not stem:
        return None

    def first(*names: str) -> Optional[str]:
        """Значение первого найденного элемента: имена перебираются по приоритету, а не по порядку в XML."""
        for wanted in names:
            for element in node.iter():
                if _local(element.tag) == wanted and (element.text or "").strip():
                    return element.text.strip()
        return None

    is_notice = stem.startswith("epNotification")
    number = first("purchaseNumber") if is_notice else first("docNumber", "requestNumber", "purchaseNumber")
    date = _date_ru(first("publishDTInEIS", "docPublishDate", "signDT", "protocolDate", "publishDate"))
    parts = [XML_TITLES[stem]]
    if date:
        parts.append(f"от {date}")
    if number:
        parts.append(f"№{number.lstrip('№').strip()}")
    version = first("versionNumber")
    return {"title": " ".join(parts), "stem": stem, "version": int(version) if version and version.isdigit() else 0}


def xml_title(text: Optional[str]) -> Optional[str]:
    """Название документа ЕИС по XML (см. :func:`xml_info`) или ``None``."""
    info = xml_info(text)
    return info["title"] if info else None


HEADING_START_RE = re.compile(r"^(?:приложени|протокол\s|описани|обоснован|проект\s|порядок\s|требовани|информационн|"
                              r"извещени|разъяснени|техническ\w+\s+задани|расч[её]т\s|инструкци)", re.I)


def heuristic_title(head: str) -> Optional[str]:
    """Первая строка начала документа, которая выглядит как его название (не длиннее 200 символов).

    Строки шаблона («№ ____ от «__» ____»), римские разделы («II. Критерии…») и строки с маленькой буквы не берутся:
    лучше имя файла, чем неверное название.
    """
    lines = [ln.strip() for ln in head.splitlines() if ln.strip()][:5]          # название — в самом начале документа
    for line in lines:
        bare_appendix = re.fullmatch(r"приложени\w*\s*(?:№|N)?\s*\d*\W*", line, re.I)      # «Приложение 1» без названия
        if 8 <= len(line) <= 200 and "__" not in line and not bare_appendix and HEADING_START_RE.match(line):
            line = re.sub(r"\s*\((?:далее|сокращ)[^)]*\)", "", line, flags=re.I)
            return re.sub(r"\s+", " ", line).strip(" .;:")
    return None


def title_in_head(title: str, head: str) -> bool:
    """Название опирается на текст документа: дословно или не менее 80% значимых слов (от 4 букв) есть в начале."""
    if facts_mod.find_quote(title, head):
        return True
    words = [w for w in re.findall(r"[а-яёa-z]{4,}", title.casefold())]
    low = head.casefold()
    return bool(words) and sum(w[:6] in low for w in words) / len(words) >= 0.8


async def title_by_model(head: str, filename: str, llm_call: LlmCall) -> Optional[str]:
    """Название документа от модели; принимается только если опирается на начало текста."""
    try:
        raw = await llm_call([{"role": "system", "content": TITLE_PROMPT},
                              {"role": "user", "content": f"Имя файла: {filename}\n\nНачало документа:\n{head}"}], TITLE_SCHEMA)
    except Exception:  # noqa: BLE001 — сбой модели не должен ронять сборку
        return None
    parsed = facts_mod.parse_json_any(raw)
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        parsed = parsed[0]
    title = str((parsed or {}).get("title") or "").strip() if isinstance(parsed, dict) else ""
    title = re.sub(r"\s+", " ", title).strip(" ;")
    return title if title and title_in_head(title, head) else None


def filename_stem(filename: Optional[str]) -> str:
    """Имя файла без расширения и служебных суффиксов («_1», «(2)»)."""
    stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", filename or "").strip()
    return re.sub(r"[\s_]*(?:\(\d+\)|_\d+)$", "", stem).strip() or "Документ"


async def document_titles(documents: Sequence[dict], llm_call: Optional[LlmCall]) -> List[Dict[str, Any]]:
    """Перечень документов по названиям из самих документов.

    Args:
        documents: Документы экспертизы (``filename``, ``doc_code``, ``text``).
        llm_call: Функция вызова LLM; ``None`` — только XML и эвристика.

    Returns:
        list[dict]: ``{"title", "source", "filename"}`` в порядке перечня: извещение, приложения по номеру,
        остальные по имени файла. ``source`` — ``xml`` / ``model`` / ``heading`` / ``filename``. Одинаковые
        названия (копии файлов) объединены.
    """
    docs = [d for d in documents if not SKIP_FILE_RE.search(str(d.get("filename") or ""))][:MAX_DOCS]
    sem = asyncio.Semaphore(6)

    async def one(doc: dict) -> Optional[Dict[str, Any]]:
        """Название одного документа по цепочке источников."""
        text = doc.get("text") or ""
        filename = doc.get("filename") or ""
        info = xml_info(text)
        if info:
            return {"title": info["title"], "source": "xml", "filename": filename, "stem": info["stem"],
                    "version": info["version"]}
        if text.lstrip().startswith("<?xml"):
            return None          # XML ЕИС, не входящий в перечень (итоги по лотам и т.п.)
        head = text[:HEAD_CHARS]
        if llm_call is not None and head.strip():
            async with sem:
                title = await title_by_model(head, filename, llm_call)
            if title:
                return {"title": title, "source": "model", "filename": filename}
        title = heuristic_title(head)
        if title:
            return {"title": title, "source": "heading", "filename": filename}
        return {"title": filename_stem(filename), "source": "filename", "filename": filename}

    found = [x for x in await asyncio.gather(*(one(d) for d in docs)) if x]
    # извещение в перечне одно: первая версия (с её датой публикации); изменения извещения отдельными строками не идут
    notices = [x for x in found if x.get("stem", "").startswith("epNotification")]
    if len(notices) > 1:
        keep = min(notices, key=lambda x: x["version"])
        found = [x for x in found if x not in notices or x is keep]
    # текстовый документ, повторяющий протокол из XML, не дублируем
    xml_heads = {re.sub(r"\W+", " ", x["title"]).casefold()[:45] for x in found if x["source"] == "xml"}
    found = [x for x in found if x["source"] == "xml" or re.sub(r"\W+", " ", x["title"]).casefold()[:45] not in xml_heads]
    unique, seen = [], set()
    for item in found:
        key = re.sub(r"\W+", " ", item["title"]).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(item)

    def order(item: Dict[str, Any]) -> Tuple[int, int, str]:
        """Извещение — первым, приложения — по номеру, остальные — по имени файла."""
        title = item["title"]
        if re.match(r"извещение о проведении|извещение об осуществлении", title, re.I):
            return 0, 0, title
        match = APPENDIX_RE.search(title) or APPENDIX_RE.search(item["filename"])
        return (1, int(match.group(1)), title) if match else (2, 0, item["filename"])

    return sorted(unique, key=order)


def format_titles(items: Sequence[Dict[str, Any]]) -> str:
    """Нумерованный перечень, как у экспертов: ``1. Название.`` на каждой строке."""
    return "\n".join(f"{i}. {re.sub(r'[.;]+$', '', item['title'])}." for i, item in enumerate(items, 1))
