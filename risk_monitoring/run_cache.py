"""Общий кэш одного прогона риск-мониторинга: каждая строка внешней БД и каждый архив ЕИС — один раз.

Анализаторы файлов и событий XML за один прогон обращаются к одним и тем же XML, их предыдущим версиям, файлам и
архивам. Без общего кэша каждый анализатор (а событийный — ещё и для каждого XML отдельно) повторял запросы и
скачивал архивы заново (ежедневный прогон 20.10.2026: «поиск закупки для XML без привязки» — до 170 с на XML,
этап событий — 6000 с).

* :meth:`RunCache.keyed` — строки запроса по ключу: запрашиваются только ещё не запрошенные id, пачками;
* ``archives`` — общий ``ArchiveCache`` (архив скачивается один раз, удаляет его владелец кэша);
* ``parsed`` — разобранные XML (``id → (тег, версия, тело)``);
* ``links`` — привязка XML без закупки (``xml_id → link``).
"""
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Set

CHUNK = 500   # значений в одном IN (...)

Fetch = Callable[..., Awaitable[List[Dict[str, Any]]]]


def chunks(values: Sequence[Any], size: Optional[int] = None) -> List[List[Any]]:
    """Список пачками не больше ``size`` (по умолчанию ``CHUNK``)."""
    values = list(values)
    size = size or CHUNK
    return [values[i:i + size] for i in range(0, len(values), size)]


class RunCache:
    """Кэш одного прогона.

    Args:
        archives: Общий ``ArchiveCache`` (``None`` — у каждого анализатора свой).
    """

    def __init__(self, archives: Any = None):
        """Пустые кэши."""
        self.archives = archives
        self.rows: Dict[str, Dict[Any, List[Dict[str, Any]]]] = {}
        self.fetched: Dict[str, Set[Any]] = {}
        self.parsed: Dict[int, Any] = {}
        self.bodies: Dict[int, Any] = {}
        self.links: Dict[int, Dict[str, Any]] = {}
        self.file_rows: Dict[int, Dict[str, Any]] = {}
        self.file_ids: Set[int] = set()
        self.file_urls: Set[str] = set()
        self.queries: Dict[str, int] = {}
        # закупки, все файлы которых уже в file_rows (запросом по дате): по ссылкам их вложения не дозапрашиваются
        self.covered_cards: Set[int] = set()
        # XML, файлы которых (и их предыдущих версий) уже загружены целиком — по ссылкам вложений не дозапрашиваются
        self.covered_xml: Set[int] = set()

    def seed(self, name: str, rows: Iterable[Dict[str, Any]], key: str, ids: Iterable[Any]) -> None:
        """Кладёт в кэш строки, полученные одним запросом по дате (``daily_xml.sql`` …), и отмечает ``ids`` как
        запрошенные: :meth:`keyed` по ним во внешнюю БД больше не обращается.

        Args:
            name: Запрос, вместо которого получены строки (``xml_meta.sql``, ``xml_previous.sql``).
            rows: Строки.
            key: Колонка-ключ.
            ids: Значения, для которых строки получены полностью (в т. ч. те, у кого строк нет).
        """
        store = self.rows.setdefault(name, {})
        for r in rows:
            k = _norm(r.get(key))
            if k is not None:
                store.setdefault(k, []).append(r)
        self.fetched.setdefault(name, set()).update(_norm(i) for i in ids if i is not None)

    def seed_files(self, rows: Iterable[Dict[str, Any]], xml_ids: Iterable[int], cards: Iterable[int]) -> None:
        """Файлы, полученные запросом по дате: XML ``xml_ids`` и закупки ``cards`` покрыты полностью."""
        for r in rows:
            if r.get("id") is not None:
                self.file_rows[int(r["id"])] = r
        self.file_ids.update(int(i) for i in xml_ids)
        self.covered_cards.update(int(c) for c in cards)

    def cover_xml(self, xml_ids: Iterable[int]) -> None:
        """XML привязанных закупок, все файлы которых уже в ``file_rows`` (вложения по ссылкам среди них)."""
        self.covered_xml.update(int(i) for i in xml_ids)

    def release_xml(self) -> None:
        """Освобождает память под разобранные XML (после пачки ежедневного прогона)."""
        self.parsed.clear()
        self.bodies.clear()

    def count(self, name: str) -> None:
        """Учёт запросов к внешней БД (для журнала и тестов)."""
        self.queries[name] = self.queries.get(name, 0) + 1

    async def keyed(self, fetch: Fetch, name: str, ids: Iterable[Any], key: str, head: Sequence[Any] = (),
                    list_name: str = "ids") -> Dict[Any, List[Dict[str, Any]]]:
        """Строки запроса ``name`` по значениям ``ids``, сгруппированные по колонке ``key``.

        Запрашиваются только значения, которых ещё не было в этом прогоне (пачками по ``CHUNK``).

        Args:
            fetch: ``_query`` анализатора: ``fetch(name, head=..., **{list_name: [...]})``.
            name: Файл запроса.
            ids: Нужные значения.
            key: Колонка результата, по которой строки относятся к значению (``id``, ``current_id``).
            head: Одиночные параметры до списка.
            list_name: Имя маркера списка в запросе.

        Returns:
            Dict[Any, List[Dict[str, Any]]]: ``значение → строки`` (только для запрошенных ``ids``).
        """
        want = list(dict.fromkeys(_norm(i) for i in ids if i is not None))
        store = self.rows.setdefault(name, {})
        done = self.fetched.setdefault(name, set())
        missing = [i for i in want if i not in done]
        for part in chunks(missing):
            self.count(name)
            for r in await fetch(name, head=list(head), **{list_name: part}):
                k = _norm(r.get(key))
                if k is not None:
                    store.setdefault(k, []).append(r)
            done.update(part)
        return {i: store.get(i, []) for i in want}

    async def one_each(self, fetch: Fetch, name: str, ids: Iterable[Any], key: str = "id",
                       head: Sequence[Any] = ()) -> Dict[Any, Dict[str, Any]]:
        """Как :meth:`keyed`, но одна строка на значение (``xml_meta.sql``, ``card_orgs.sql``)."""
        return {k: v[0] for k, v in (await self.keyed(fetch, name, ids, key, head)).items() if v}


def _norm(v: Any) -> Optional[Any]:
    """Ключ кэша: числа (id из БД приходят int, float или numpy) — ``int``; строки (номера закупок с ведущим нулём)
    остаются строками без пробелов по краям."""
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip() or None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f:  # NaN
        return None
    return int(f) if f == int(f) else f
