"""Синхронизация источников публикаций экспертов с внешней БД.

Как обрабатываются изменения во внешней БД (новый эксперт, новые/удалённые файлы и ссылки, смена ФИО):

1. **Снимок.** Двумя SQL-запросами к порталу берутся все эксперты со ссылками анкеты и все файлы публикаций / монографий
   (≈ 1 тыс. экспертов, 1,5 тыс. файлов, 3 тыс. ссылок — дешевле, чем отслеживать изменения по датам: у ``media``
   нет даты изменения, а ``users.updated_at`` меняется от любого поля анкеты).
2. **Отпечаток эксперта.** Для каждого эксперта считается хэш множества его источников. Совпал с сохранённым —
   эксперт пропускается (типичный запуск: изменились единицы экспертов).
3. **Сравнение источников** изменившегося эксперта: новые (новый эксперт, загруженный файл, добавленная ссылка) —
   в очередь ``pending``; исчезнувшие — ``removed`` (публикации перестают учитываться в similarity); вернувшиеся —
   снова в очередь. Файл с тем же содержимым (перезагрузка, тот же файл у соавтора) не читается и не разбирается
   повторно: текст и разбор берутся из ``ai_documents`` / ``ai_document_analysis`` по SHA-256.
4. **Смена ФИО** — без повторного разбора: заново проверяется авторство уже извлечённых публикаций.
5. **Удалённый эксперт** (``deleted_at``, сменилась роль, нет в выборке при полной синхронизации) — все его
   источники ``removed``.

Синхронизация запускается Celery beat раз в ``settings.EXPERT_PUB_SYNC_EVERY_MIN`` минут и по запросу
``POST /expert-publications/sync`` (например, портал вызывает его после сохранения анкеты — с ``expert_ids``,
тогда обрабатываются только эти эксперты).
"""
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from configs.logger import get_logger
from search_experts.publications import repo
from search_experts.publications.authors import author_matches
from search_experts.publications.links import classify, normalize_url, parse_link_list

logger = get_logger(__name__)

COLLECTIONS = {"documentPublication": "publication", "documentMonographs": "monograph"}
LINK_FIELDS = (("linkPublication", "publication"), ("linkMonographs", "monograph"))


@dataclass
class Source:
    """Источник публикаций эксперта (файл анкеты или ссылка).

    Attributes:
        kind: ``media`` | ``link``.
        key: ``media.id`` для файла, нормализованный URL для ссылки.
        collection: ``publication`` | ``monograph``.
        url: Адрес скачивания файла / ссылка.
        file_name: Имя файла (для ссылки — ``None``).
        link_type: ``media`` для файла, тип ссылки (:func:`search_experts.publications.links.classify`).
    """

    kind: str
    key: str
    collection: str
    url: Optional[str]
    file_name: Optional[str]
    link_type: str

    @property
    def full_key(self) -> str:
        """Ключ источника в пределах эксперта: ``"<kind>:<key>"``."""
        return f"{self.kind}:{self.key}"


@dataclass
class ExpertSnapshot:
    """Эксперт во внешней БД на момент синхронизации.

    Attributes:
        expert_id: ID эксперта.
        name: ФИО.
        deleted: Удалён (``deleted_at``).
        sources: Источники публикаций.
    """

    expert_id: int
    name: Optional[str]
    deleted: bool = False
    sources: List[Source] = field(default_factory=list)

    def sources_hash(self) -> str:
        """Отпечаток множества источников (порядок не важен).

        Returns:
            str: SHA-256 (16 символов) строк ``kind:key|collection|url``.
        """
        lines = sorted(f"{s.full_key}|{s.collection}|{s.url or ''}" for s in self.sources)
        return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


def _blank(value: Any) -> bool:
    """Пустое значение строки DataFrame / API (``None``, ``NaN``, ``""``)."""
    return value is None or (isinstance(value, float) and value != value) or (isinstance(value, str) and not value.strip())


def media_url(media_id: Any, file_name: str, disk: Optional[str], media_path: str, storage_path: str) -> str:
    """Адрес скачивания файла media — как в ``queries/evaluate_docs_script.sql``.

    Args:
        media_id: ``media.id``.
        file_name: ``media.file_name``.
        disk: ``media.disk`` (``media`` → ``/api/media/``, иначе ``/storage/``).
        media_path: ``DatabaseConfig.media_path``.
        storage_path: ``DatabaseConfig.storage_path``.

    Returns:
        str: URL (скачивается ``CloudStorageParser.download_inner``).
    """
    base = media_path if str(disk or "").lower() == "media" else storage_path
    return f"{base}{int(media_id)}/{file_name}"


def build_snapshot(users: Iterable[dict], media: Iterable[dict], media_path: str, storage_path: str) -> Dict[int, ExpertSnapshot]:
    """Собирает снимок экспертов из строк внешней БД.

    Ссылка, встречающаяся у эксперта дважды (в публикациях и монографиях), учитывается один раз (первое вхождение).
    Файлы экспертов, которых нет в выборке ``users`` (не эксперт), не учитываются.

    Args:
        users: Строки ``queries/expert_pub_users.sql``.
        media: Строки ``queries/expert_pub_media.sql``.
        media_path: Префикс ``/api/media/``.
        storage_path: Префикс ``/storage/``.

    Returns:
        Dict[int, ExpertSnapshot]: По ID эксперта.
    """
    experts: Dict[int, ExpertSnapshot] = {}
    for u in users:
        eid = int(u["expert_id"])
        snap = ExpertSnapshot(eid, None if _blank(u.get("name")) else str(u["name"]).strip(),
                              deleted=not _blank(u.get("deleted_at")))
        seen = set()
        for column, collection in LINK_FIELDS:
            for raw in parse_link_list(None if _blank(u.get(column)) else u.get(column)):
                url = normalize_url(raw)
                if not url or url in seen:
                    continue
                seen.add(url)
                snap.sources.append(Source("link", url, collection, url, None, classify(url)))
        experts[eid] = snap
    for m in media:
        eid = int(m["expert_id"])
        if eid not in experts or _blank(m.get("file_name")):
            continue
        collection = COLLECTIONS.get(str(m.get("collection_name")), "publication")
        experts[eid].sources.append(Source(
            "media", str(int(m["media_id"])), collection,
            media_url(m["media_id"], str(m["file_name"]), m.get("disk"), media_path, storage_path),
            str(m["file_name"]), "media"))
    return experts


def initial_status(source: Source, link_types: Sequence[str]) -> str:
    """Статус нового источника: файлы и включённые типы ссылок — в очередь, остальные — ``unsupported``.

    Args:
        source: Источник.
        link_types: Обрабатываемые типы ссылок (``settings.EXPERT_PUB_LINK_TYPES``).

    Returns:
        str: ``pending`` | ``unsupported``.
    """
    if source.kind == "media" or source.link_type in link_types:
        return repo.PENDING
    return repo.UNSUPPORTED


def _filter_sql(sql: str, column: str, expert_ids: Optional[Sequence[int]]) -> (str, list):
    """Подставляет в запрос фильтр по экспертам (``{{EXPERT_FILTER}}``) и возвращает привязки.

    Комментарии ``/* … */`` и ``-- …`` убираются: портал (``/api/database/query``) принимает только запросы,
    начинающиеся с SELECT / SHOW / DESCRIBE / EXPLAIN / WITH (403 на запрос с комментарием в начале, 31.10.2026).
    """
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"(?m)--[^\n]*$", " ", sql).strip()
    if not expert_ids:
        return sql.replace("{{EXPERT_FILTER}}", ""), []
    ids = [int(i) for i in expert_ids]
    return sql.replace("{{EXPERT_FILTER}}", f"AND {column} IN ({', '.join('?' * len(ids))})"), ids


async def fetch_snapshot(data_fetcher, db_config, expert_ids: Optional[Sequence[int]] = None) -> Dict[int, ExpertSnapshot]:
    """Читает снимок из внешней БД (два запроса через ``/api/database/query``).

    Args:
        data_fetcher: ``configs.data_fetcher.DataFetcher`` окружения.
        db_config: ``DatabaseConfig`` окружения (``media_path``, ``storage_path``).
        expert_ids: Только эти эксперты (``None`` — все).

    Returns:
        Dict[int, ExpertSnapshot]: По ID эксперта.
    """
    from configs.utils import load_sql_query_async

    users_sql, users_b = _filter_sql(await load_sql_query_async("expert_pub_users.sql"), "u.id", expert_ids)
    media_sql, media_b = _filter_sql(await load_sql_query_async("expert_pub_media.sql"), "m.model_id", expert_ids)
    users = await data_fetcher.fetch_async_expertise_data(users_sql, bindings=users_b)
    media = await data_fetcher.fetch_async_expertise_data(media_sql, bindings=media_b)
    return build_snapshot(users.to_dict("records") if len(users) else [],
                          media.to_dict("records") if len(media) else [],
                          db_config.media_path, db_config.storage_path)


async def reverify_authors(conn, env: str, expert_id: int, name: Optional[str]) -> int:
    """Заново проверяет авторство публикаций эксперта (после смены ФИО).

    Args:
        conn: Соединение.
        env: Окружение.
        expert_id: ID эксперта.
        name: Новое ФИО.

    Returns:
        int: Сколько публикаций изменили признак.
    """
    changes = []
    for p in await repo.publications_of_expert(conn, env, expert_id):
        verified = author_matches(name, p.get("authors"))
        old = p.get("author_verified")
        if isinstance(old, str):  # psql-адаптер тестов возвращает текст
            old = {"true": True, "false": False}.get(old.lower())
        if verified != old:
            changes.append((p["id"], verified))
    await repo.set_author_verified(conn, changes)
    return len(changes)


async def apply_snapshot(conn, env: str, snapshot: Dict[int, ExpertSnapshot], full: bool,
                         link_types: Sequence[str]) -> Dict[str, int]:
    """Сравнивает снимок с внутренней БД и записывает изменения.

    Args:
        conn: Соединение с внутренней БД.
        env: Окружение.
        snapshot: Снимок (все эксперты при ``full`` или только запрошенные).
        full: Полная синхронизация: эксперты, которых нет в снимке, считаются удалёнными.
        link_types: Обрабатываемые типы ссылок.

    Returns:
        Dict[str, int]: ``experts_new``, ``experts_changed``, ``experts_unchanged``, ``experts_deleted``,
        ``renamed``, ``sources_removed``, ``sources_upserted``.
    """
    stored = await repo.load_experts(conn, env, None if full else list(snapshot))
    st = dict(experts_new=0, experts_changed=0, experts_unchanged=0, experts_deleted=0, renamed=0,
              sources_removed=0, sources_upserted=0)
    unchanged = []

    for eid, snap in snapshot.items():
        old = stored.get(eid)
        if snap.deleted:
            if old is None or not old.get("deleted_at"):
                st["sources_removed"] += await repo.remove_missing_sources(conn, env, eid, [])
                await repo.upsert_expert(conn, env, eid, snap.name, None, deleted=True)
                st["experts_deleted"] += 1
            continue
        new_hash = snap.sources_hash()
        renamed = old is not None and (old.get("name") or None) != snap.name
        if renamed:
            st["renamed"] += 1
            await reverify_authors(conn, env, eid, snap.name)
        if old is not None and old.get("sources_hash") == new_hash and not old.get("deleted_at"):
            if renamed:
                await repo.upsert_expert(conn, env, eid, snap.name, new_hash)
            else:
                unchanged.append(eid)
            st["experts_unchanged"] += 1
            continue
        # новый эксперт или изменился набор источников: сначала эксперт (ФИО нужно обработке), затем источники
        await repo.upsert_expert(conn, env, eid, snap.name, new_hash)
        await repo.upsert_sources(conn, env, [
            (eid, s.kind, s.key, s.collection, s.url, s.file_name, s.link_type, initial_status(s, link_types))
            for s in snap.sources])
        st["sources_upserted"] += len(snap.sources)
        st["sources_removed"] += await repo.remove_missing_sources(conn, env, eid, [s.full_key for s in snap.sources])
        st["experts_new" if old is None else "experts_changed"] += 1

    if full:
        for eid, old in stored.items():
            if eid not in snapshot and not old.get("deleted_at"):
                st["sources_removed"] += await repo.remove_missing_sources(conn, env, eid, [])
                await repo.upsert_expert(conn, env, eid, old.get("name"), None, deleted=True)
                st["experts_deleted"] += 1

    await repo.touch_experts(conn, env, unchanged)
    return st
