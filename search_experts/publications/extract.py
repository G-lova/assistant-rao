"""Разбор источника публикаций эксперта в записи публикаций.

* **Файл** (анкета, прямая ссылка, облако): текст (общий документ ``ai_documents``) → модель определяет вид документа
  (статья, книга, список трудов, прочее) и извлекает записи; список трудов длиннее одного запроса разбирается частями.
  Результат кэшируется в ``ai_document_analysis`` по документу и версии промпта.
* **DOI**: метаданные OpenAlex (название, авторы, год, журнал, аннотация, ключевые слова), при отказе — Crossref.
* **Страница журнала / репозитория**: мета-теги ``citation_*`` (Highwire Press: OJS, КиберЛенинка, большинство
  журнальных платформ) и ``DC.*``; DOI со страницы дополняет аннотацию из OpenAlex.

Запись публикации — словарь :func:`normalize_publication`.
"""
import datetime
import hashlib
import html
import json
import re
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Tuple

from configs.logger import get_logger
from search_experts.publications.links import extract_doi

logger = get_logger(__name__)

ANALYSIS_TYPE = "expert_publications"
PUB_TYPES = ("article", "monograph", "textbook", "conference", "patent", "other")
DOC_KINDS = ("article", "book", "list", "other")
MAX_ABSTRACT = 3000
EMBED_ABSTRACT = 1500

SYSTEM_PROMPT = """Ты разбираешь документ из анкеты эксперта: статью, монографию, учебник, список научных трудов или другой документ.
Определи вид документа (doc_kind) и извлеки публикации:
- article — текст или первая страница одной статьи (тезисов, главы): одна запись — название, авторы, год, журнал или сборник,
  ключевые слова, аннотация (как в тексте, до 1500 символов), DOI, УДК;
- book — монография, учебник, учебное пособие (титульный лист, выходные данные): одна запись, abstract — аннотация книги;
- list — список научных трудов: запись на каждую позицию — название (без авторов и выходных данных), авторы, год,
  журнал или издательство, тип; keywords и abstract не заполняй;
- other — диплом, сертификат, справка, резюме без перечня публикаций и т. п.: publications пустой.
Только то, что есть в тексте, ничего не придумывай. Название — на языке оригинала. Авторы — как в тексте («Иванов И.И.»).
pub_type: article (статья), monograph, textbook (учебник, пособие), conference (тезисы, доклад), patent, other."""

LIST_PROMPT = """Это продолжение списка научных трудов эксперта (часть {part}). Извлеки все позиции этой части по тем же правилам:
название, авторы, год, журнал или издательство, тип. doc_kind — list."""

SCHEMA = {
    "type": "object",
    "properties": {
        "doc_kind": {"type": "string", "enum": list(DOC_KINDS)},
        "publications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "authors": {"type": "array", "items": {"type": "string"}},
                    "year": {"type": ["integer", "null"]},
                    "venue": {"type": ["string", "null"]},
                    "pub_type": {"type": "string", "enum": list(PUB_TYPES)},
                    "keywords": {"type": ["string", "null"]},
                    "abstract": {"type": ["string", "null"]},
                    "doi": {"type": ["string", "null"]},
                    "udk": {"type": ["string", "null"]},
                },
                "required": ["title"],
            },
        },
    },
    "required": ["doc_kind", "publications"],
}


def prompt_version() -> str:
    """Версия промпта разбора файла: смена промпта или схемы инвалидирует кэш ``ai_document_analysis``.

    Returns:
        str: ``pub-`` + 12 символов хэша.
    """
    h = hashlib.sha256((SYSTEM_PROMPT + LIST_PROMPT + json.dumps(SCHEMA, sort_keys=True)).encode("utf-8"))
    return "pub-" + h.hexdigest()[:12]


def _clean(value: Any, limit: Optional[int] = None) -> Optional[str]:
    """Строка без лишних пробелов и HTML-тегов; пустая → ``None``."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value if v)
    text = re.sub(r"<[^>]+>", " ", html.unescape(str(value)))
    text = re.sub(r"\s+", " ", text).strip()
    if not text or text.lower() in ("null", "none", "nan", "-"):
        return None
    return text[:limit] if limit else text


def _year(value: Any) -> Optional[int]:
    """Год публикации из числа или строки (``2023``, ``"2023-05-01"``); вне 1950…текущий+1 → ``None``."""
    if value is None:
        return None
    m = re.search(r"(19|20)\d{2}", str(value))
    if not m:
        return None
    year = int(m.group(0))
    return year if 1950 <= year <= datetime.date.today().year + 1 else None


def normalize_publication(raw: Dict[str, Any], default_type: str = "article") -> Optional[Dict[str, Any]]:
    """Приводит запись публикации к виду таблицы ``expert_publications``.

    Args:
        raw: Ответ модели / OpenAlex / мета-тегов.
        default_type: Тип, если не указан (``monograph`` для файлов из «Монографий»).

    Returns:
        Optional[Dict[str, Any]]: ``title``, ``year``, ``venue``, ``pub_type``, ``authors`` (список или ``None``),
        ``keywords``, ``abstract``, ``doi``, ``udk``; ``None`` — нет названия (короче 5 символов).
    """
    if not isinstance(raw, dict):
        return None
    title = _clean(raw.get("title"), 1000)
    if not title or len(title) < 5:
        return None
    authors = raw.get("authors")
    if isinstance(authors, str):
        authors = [a for a in re.split(r"[;,](?![^()]*\))", authors)]
    authors = [a for a in (_clean(x, 200) for x in (authors or [])) if a] or None
    pub_type = str(raw.get("pub_type") or default_type).lower()
    doi = raw.get("doi")
    doi = extract_doi(str(doi)) if doi else None
    return {
        "title": title,
        "year": _year(raw.get("year")),
        "venue": _clean(raw.get("venue"), 500),
        "pub_type": pub_type if pub_type in PUB_TYPES else "other",
        "authors": authors[:50] if authors else None,
        "keywords": _clean(raw.get("keywords"), 1000),
        "abstract": _clean(raw.get("abstract"), MAX_ABSTRACT),
        "doi": doi,
        "udk": _clean(raw.get("udk"), 100),
    }


def _title_key(title: str) -> str:
    """Ключ сравнения названий: буквы и цифры в нижнем регистре."""
    return re.sub(r"[\W_]+", "", title.lower().replace("ё", "е"))[:200]


def dedupe(publications: Iterable[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """Убирает повторы (то же название) и ограничивает число записей.

    Из повторов остаётся первая запись, пустые поля дополняются из следующих.

    Args:
        publications: Нормализованные записи.
        limit: Максимум записей.

    Returns:
        List[Dict[str, Any]]: Записи без повторов.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for p in publications:
        key = _title_key(p["title"])
        if key in out:
            for k, v in p.items():
                if out[key].get(k) in (None, "", []) and v not in (None, "", []):
                    out[key][k] = v
        elif len(out) < limit:
            out[key] = dict(p)
    return list(out.values())


def split_text(text: str, chunk_chars: int, max_chunks: int) -> List[str]:
    """Режет текст на части по абзацам (часть не длиннее ``chunk_chars``), не больше ``max_chunks`` частей.

    Args:
        text: Текст документа.
        chunk_chars: Максимум символов в части.
        max_chunks: Максимум частей.

    Returns:
        List[str]: Части текста.
    """
    text = (text or "").strip()
    if len(text) <= chunk_chars:
        return [text] if text else []
    parts, current = [], ""
    for para in re.split(r"\n\s*\n|\n(?=\s*\d+[.)]\s)", text):
        while len(para) > chunk_chars:
            if current:
                parts.append(current)
                current = ""
            parts.append(para[:chunk_chars])
            para = para[chunk_chars:]
        if len(current) + len(para) + 2 > chunk_chars:
            parts.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
        if len(parts) >= max_chunks:
            break
    if current and len(parts) < max_chunks:
        parts.append(current)
    return [p for p in parts if p.strip()][:max_chunks]


def embed_text(pub: Dict[str, Any]) -> str:
    """Текст публикации для эмбеддинга: название, ключевые слова, аннотация (до 1500 символов), журнал.

    Args:
        pub: Запись публикации.

    Returns:
        str: Текст.
    """
    parts = [pub.get("title"), pub.get("keywords"), (pub.get("abstract") or "")[:EMBED_ABSTRACT], pub.get("venue")]
    return ". ".join(p.strip().rstrip(".") for p in parts if p and str(p).strip())


def text_sha(text: str) -> str:
    """SHA-256 текста эмбеддинга (пересчёт только при изменении текста)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- DOI: OpenAlex / Crossref

def openalex_abstract(inverted: Optional[Dict[str, List[int]]]) -> Optional[str]:
    """Восстанавливает аннотацию из инвертированного индекса OpenAlex (слово → позиции).

    Args:
        inverted: ``abstract_inverted_index``.

    Returns:
        Optional[str]: Текст аннотации.
    """
    if not inverted:
        return None
    words = sorted((pos, word) for word, positions in inverted.items() for pos in positions)
    return " ".join(w for _, w in words) or None


def from_openalex(work: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Запись публикации из ответа OpenAlex ``/works/doi:…``.

    Args:
        work: JSON работы.

    Returns:
        Optional[Dict[str, Any]]: Нормализованная запись или ``None``.
    """
    source = ((work.get("primary_location") or {}).get("source") or {})
    keywords = [k.get("display_name") for k in (work.get("keywords") or []) if isinstance(k, dict)]
    if not keywords:
        keywords = [t.get("display_name") for t in (work.get("topics") or [])[:3] if isinstance(t, dict)]
    otype = str(work.get("type") or "")
    return normalize_publication({
        "title": work.get("title") or work.get("display_name"),
        "authors": [((a.get("author") or {}).get("display_name")) for a in (work.get("authorships") or [])],
        "year": work.get("publication_year"),
        "venue": source.get("display_name"),
        "pub_type": {"book": "monograph", "book-chapter": "monograph", "proceedings-article": "conference"}.get(otype, "article"),
        "keywords": keywords,
        "abstract": openalex_abstract(work.get("abstract_inverted_index")),
        "doi": work.get("doi"),
    })


def from_crossref(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Запись публикации из ответа Crossref ``/works/{doi}`` (поле ``message``).

    Args:
        message: JSON работы.

    Returns:
        Optional[Dict[str, Any]]: Нормализованная запись или ``None``.
    """
    def first(v):
        return v[0] if isinstance(v, list) and v else v

    issued = ((message.get("issued") or message.get("published") or {}).get("date-parts") or [[None]])[0]
    ctype = str(message.get("type") or "")
    return normalize_publication({
        "title": first(message.get("title")),
        "authors": [" ".join(x for x in (a.get("family"), a.get("given")) if x) for a in (message.get("author") or [])],
        "year": issued[0] if issued else None,
        "venue": first(message.get("container-title")) or message.get("publisher"),
        "pub_type": {"book": "monograph", "monograph": "monograph", "book-chapter": "monograph",
                     "proceedings-article": "conference"}.get(ctype, "article"),
        "keywords": message.get("subject"),
        "abstract": message.get("abstract"),
        "doi": message.get("DOI"),
    })


# ---------------------------------------------------------------- страница журнала: мета-теги

class _MetaParser(HTMLParser):
    """Собирает ``<meta name|property=… content=…>`` страницы (повторяющиеся имена — списком)."""

    def __init__(self):
        """Создаёт пустой словарь мета-тегов."""
        super().__init__(convert_charrefs=True)
        self.meta: Dict[str, List[str]] = {}

    def handle_starttag(self, tag, attrs):
        """Запоминает мета-тег с непустым ``content``."""
        if tag.lower() != "meta":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        name = (a.get("name") or a.get("property") or "").strip().lower()
        if name and a.get("content", "").strip():
            self.meta.setdefault(name, []).append(a["content"].strip())


def from_html_meta(page: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Запись публикации из мета-тегов страницы (``citation_*``, ``DC.*``).

    Описание страницы (``description`` / ``og:description``) как аннотация не берётся: на сайтах журналов это
    обычно описание журнала, а не статьи.

    Args:
        page: HTML страницы.

    Returns:
        Tuple[Optional[dict], Optional[str]]: Запись (``None`` — на странице нет названия статьи) и ссылка на PDF
        (``citation_pdf_url``).
    """
    parser = _MetaParser()
    try:
        parser.feed(page)
    except Exception:  # noqa: BLE001 — битая разметка: берём то, что успели собрать
        pass
    m = parser.meta

    def one(*names):
        for n in names:
            if m.get(n):
                return m[n][0]
        return None

    title = one("citation_title", "dc.title", "eprints.title")
    if not title:
        return None, None
    authors = m.get("citation_author") or m.get("dc.creator") or m.get("eprints.creators_name")
    # общий meta keywords — часто ключевые слова сайта, а не статьи: только citation_keywords / DC.subject
    keywords = m.get("citation_keywords") or m.get("dc.subject")
    pub = normalize_publication({
        "title": title,
        "authors": authors,
        "year": one("citation_publication_date", "citation_date", "citation_year", "citation_online_date", "dc.date"),
        "venue": one("citation_journal_title", "citation_conference_title", "citation_book_title", "citation_publisher",
                     "dc.source", "dc.publisher"),
        "keywords": "; ".join(keywords) if keywords else None,
        "abstract": one("citation_abstract", "dc.description", "eprints.abstract"),
        "doi": one("citation_doi", "dc.identifier"),
        "pub_type": "conference" if one("citation_conference_title") else "article",
    })
    return pub, one("citation_pdf_url")


# ---------------------------------------------------------------- разбор файла моделью

async def parse_text_with_llm(llm, text: str, file_name: Optional[str], collection: str,
                              chunk_chars: int, max_chunks: int, max_tokens: int) -> Dict[str, Any]:
    """Разбирает текст файла моделью: вид документа и записи публикаций.

    Первая часть текста определяет вид документа; для списка трудов разбираются и остальные части.

    Args:
        llm: ``risk_monitoring.llm_json.LLMJson``.
        text: Текст файла.
        file_name: Имя файла (подсказка модели).
        collection: ``publication`` | ``monograph`` (тип по умолчанию).
        chunk_chars: Символов в части.
        max_chunks: Максимум частей.
        max_tokens: Лимит ответа.

    Returns:
        Dict[str, Any]: ``{"doc_kind", "publications": [нормализованные записи], "parts"}``.
    """
    default_type = "monograph" if collection == "monograph" else "article"
    chunks = split_text(text, chunk_chars, max_chunks)
    if not chunks:
        return {"doc_kind": "other", "publications": [], "parts": 0}
    head = f"Файл: {file_name or 'без имени'}\nРаздел анкеты: {'монографии' if collection == 'monograph' else 'публикации'}\n\n"
    first = await llm.ask(SYSTEM_PROMPT, head + chunks[0], schema=SCHEMA, max_tokens=max_tokens, list_key="publications")
    kind = first.get("doc_kind") if first.get("doc_kind") in DOC_KINDS else "other"
    raw = list(first.get("publications") or [])
    parts = 1
    if kind == "list":
        for i, chunk in enumerate(chunks[1:], start=2):
            more = await llm.ask(SYSTEM_PROMPT + "\n\n" + LIST_PROMPT.format(part=i), chunk, schema=SCHEMA,
                                 max_tokens=max_tokens, list_key="publications")
            raw.extend(more.get("publications") or [])
            parts += 1
    elif kind in ("article", "book"):
        raw = raw[:1]
    else:
        raw = []
    pubs = [p for p in (normalize_publication(r, "monograph" if kind == "book" else default_type) for r in raw) if p]
    return {"doc_kind": kind, "publications": pubs, "parts": parts}
