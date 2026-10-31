"""Нормализация и классификация ссылок на публикации из анкеты эксперта (users.linkPublication / linkMonographs).

Значение поля — JSON-массив строк (``[]``, ``[null]``, ``["https://…"]``). Ссылки исправляются (префикс PDF-расширения
Chrome, DOI, записанный как домен, ``www.`` без схемы) и относятся к типу, от которого зависит способ разбора:

* ``doi`` — DOI в ссылке (doi.org, издательства, Scopus с ``doi=`` в адресе) → метаданные из OpenAlex / Crossref;
* ``file`` — прямая ссылка на PDF / DOC → скачивание и разбор как файла анкеты;
* ``cloud`` — Яндекс.Диск, Google Drive, Облако Mail → скачивание через ``CloudStorageParser``;
* ``journal`` — сайт журнала, репозиторий вуза, КиберЛенинка → мета-теги ``citation_*`` страницы;
* ``elibrary``, ``scopus``, ``profile`` (ResearchGate, Google Scholar, ORCID, ИСТИНА, WoS), ``library`` (РГБ, НЭБ, ЭБС,
  магазины) — по умолчанию не обрабатываются (``settings.EXPERT_PUB_LINK_TYPES``).
"""
import json
import re
from typing import Any, List, Optional
from urllib.parse import unquote, urlparse

# Префикс адреса, который добавляет PDF-просмотрщик Chrome (Adobe Acrobat): «chrome-extension://efaid…/https://…»
_CHROME_PDF_PREFIX = re.compile(r"^(?:chrome-extension|https?)://efaidnbmnnnibpcajpcglclefindmkaj/", re.I)
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s?#&\"'<>]+)", re.I)
_FILE_EXT = (".pdf", ".doc", ".docx", ".rtf", ".djvu", ".odt")

_ELIBRARY = ("elibrary.ru",)
_SCOPUS = ("scopus.com",)
_PROFILES = ("researchgate.net", "scholar.google", "orcid.org", "istina.msu.ru", "webofscience.com", "publons.com",
             "academia.edu", "semanticscholar.org")
_CLOUDS = ("disk.yandex", "yadi.sk", "drive.google.com", "docs.google.com", "cloud.mail.ru")
_LIBRARIES = ("rsl.ru", "rusneb.ru", "biblioclub.ru", "lanbook.com", "litres.ru", "urait.ru", "znanium.", "directmedia.ru",
              "ozon.ru", "labirint.ru", "book.ru", "iprbookshop.ru")


def parse_link_list(value: Any) -> List[str]:
    """Разбирает поле ссылок анкеты в список непустых строк.

    Args:
        value: JSON-строка массива, список или ``None``. Не-JSON строка считается одной ссылкой.

    Returns:
        List[str]: Строки без ``null`` и пустых значений, в исходном порядке.
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return [text]
    if not isinstance(value, (list, tuple)):
        value = [value]
    out = []
    for item in value:
        if item is None:
            continue
        s = str(item).strip()
        if s and s.lower() not in ("null", "none", "nan"):
            out.append(s)
    return out


def normalize_url(raw: Optional[str]) -> Optional[str]:
    """Приводит ссылку к виду, пригодному для запроса и сравнения.

    Исправляет префикс PDF-просмотрщика Chrome, DOI без ``doi.org`` (``https://10.26617/…``, ``10.1007/…``), адрес
    без схемы (``www.…``, ``doi.org/…``); убирает пробелы и завершающую пунктуацию, хост — в нижнем регистре.

    Args:
        raw: Строка из анкеты.

    Returns:
        Optional[str]: Нормализованный URL или ``None``, если в строке нет адреса.
    """
    if not raw:
        return None
    s = str(raw).strip().strip("\"'<>").rstrip(".,;)")
    s = _CHROME_PDF_PREFIX.sub("", s)
    m = re.match(r"^(?:https?://)?(?:dx\.)?(?:doi\.org/)?(10\.\d{4,9}/\S+)$", s, re.I)
    if m:
        return "https://doi.org/" + m.group(1)
    if not re.match(r"^https?://", s, re.I):
        if re.match(r"^(www\.)?[\w-]+(\.[\w-]+)+(/|$)", s):
            s = "https://" + s
        else:
            return None
    parsed = urlparse(s)
    if not parsed.netloc or "." not in parsed.netloc:
        return None
    return parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower()).geturl()


def host_of(url: str) -> str:
    """Хост ссылки без ``www.``.

    Args:
        url: Нормализованный URL.

    Returns:
        str: Хост в нижнем регистре.
    """
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def extract_doi(url: Optional[str]) -> Optional[str]:
    """Находит DOI в ссылке (путь или параметр, в т. ч. экранированный ``10.24234%2fwisdom``).

    Args:
        url: Нормализованный URL.

    Returns:
        Optional[str]: DOI в нижнем регистре без завершающих ``.pdf`` / пунктуации или ``None``.
    """
    if not url:
        return None
    m = _DOI_RE.search(unquote(url))
    if not m:
        return None
    doi = m.group(1).rstrip(".,;)/")
    doi = re.sub(r"(/(?:full|abstract|pdf|epdf|html))+$", "", doi, flags=re.I)
    doi = re.sub(r"\.pdf$", "", doi, flags=re.I)
    return doi.lower()


def classify(url: str) -> str:
    """Тип ссылки (см. описание модуля).

    DOI определяется раньше остального (Scopus с ``doi=`` в адресе — тоже ``doi``), кроме eLibrary.

    Args:
        url: Нормализованный URL.

    Returns:
        str: ``elibrary`` | ``doi`` | ``scopus`` | ``profile`` | ``cloud`` | ``file`` | ``library`` | ``journal``.
    """
    host = host_of(url)
    path = urlparse(url).path.lower()
    if any(h in host for h in _ELIBRARY):
        return "elibrary"
    if host in ("doi.org", "dx.doi.org") or extract_doi(url):
        return "doi"
    if any(h in host for h in _SCOPUS):
        return "scopus"
    if any(h in host for h in _PROFILES):
        return "profile"
    if any(h in host for h in _CLOUDS):
        return "cloud"
    if path.endswith(_FILE_EXT):
        return "file"
    if any(h in host for h in _LIBRARIES):
        return "library"
    return "journal"
