"""Справочник организаций «название → ИНН» для экспертов без реквизитов в анкете.

У эксперта без ``organization_inn`` / ``diplom_inn`` ИНН восстанавливается по названию: название без ОПФ
(:func:`search_experts.conflict_detector._org_tokens`) сравнивается с названиями справочника по TF-IDF символьных
n-грамм (2–4 буквы внутри слов: устойчиво к опечаткам, падежам и порядку слов; частые слова «государственный»,
«университет» весят мало). Лучшее совпадение со сходством ≥ ``CONFLICT_ORG_DIRECTORY_THRESHOLD`` даёт ИНН, если
следующее по сходству название другой организации не ближе ``CONFLICT_ORG_DIRECTORY_MARGIN`` (неоднозначность).

Справочник:

* официальные названия портала — заказчики, места работы и организации дипломов экспертов с ИНН
  (``queries/org_directory.sql``);
* псевдонимы — как эксперты сами писали организацию до заполнения ИНН (``org_name_aliases``, миграция 012:
  начальные данные из выгрузки users от 27.10.2026, дальше пополняется задачей ``org_aliases_task``). Именно они
  дают аббревиатуры: «РЭУ им. Г.В. Плеханова» → ИНН РЭУ.

Проверка 02.11.2026 на 404 экспертах с ИНН (их названия от 27.10 — как у экспертов без ИНН, справочник без
собственной записи эксперта): найдено 246 из 265 совпадений «эксперт работает у заказчика», 5 ложных;
``fuzz.ratio`` по целой строке — 65 и 3290 ложных.

Справочник строится один раз на процесс и окружение и обновляется через ``CONFLICT_ORG_DIRECTORY_TTL_SEC``.
"""
import asyncio
import re
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from configs.logger import get_logger

logger = get_logger(__name__)


@dataclass
class OrgMatch:
    """Найденная организация.

    Attributes:
        inn: ИНН.
        kpp: КПП записи справочника (у восстановленного ИНН КПП эксперта неизвестен — в сравнении не участвует).
        score: Косинусное сходство названий (0…1).
        name: Название из справочника.
        kind: Источник записи: ``customer`` | ``expert_work`` | ``expert_diplom`` | ``alias``.
    """

    inn: str
    kpp: Optional[str]
    score: float
    name: str
    kind: str


# Указание региона и подчинённости, которое есть в официальном названии и обычно опускается в анкете:
# «… образования города Москвы "Московский городской педагогический университет"», «… Пензенской области …»,
# «… при Президенте Российской Федерации» (в анкете — «РФ»)
_REGION_RE = re.compile(
    r"\b(?:город[ау]?|г)\s+(?:москвы|санкт-петербурга|севастополя)\b"
    r"|\b[\w-]+(?:ской|цкой)\s+области\b"
    r"|\b[\w-]+(?:ского|цкого)\s+края\b"
    r"|\b[\w-]+ского\s+автономного\s+округа(?:\s*-\s*[\w-]+)?\b"
    r"|\bроссийской\s+федерации\b"
    r"|\bминистерства\s+[\w-]+(?:\s+и\s+[\w-]+)?\s+(?:россии|рф)\b"
    r"|\bмин(?:здрава|обрнауки|просвещения|сельхоза|спорта|культуры)\s+(?:россии|рф)\b")


def _core(name: Optional[str]) -> str:
    """Название без ОПФ, указания региона, кавычек и инициалов (ядро для сравнения)."""
    from search_experts.conflict_detector import _org_tokens
    if not isinstance(name, str):
        return ""
    text = re.sub(r"[.,«»\"]", " ", name.lower().replace("ё", "е"))
    return " ".join(_org_tokens(_REGION_RE.sub(" ", text), abbr_prefix_only=True))


class OrgDirectory:
    """Справочник «название → ИНН» с поиском по TF-IDF символьных n-грамм.

    Args:
        entries: Записи ``(название, ИНН, КПП, источник)``; некорректные ИНН и пустые ядра отбрасываются,
            повторы (то же ядро и ИНН) схлопываются.
    """

    def __init__(self, entries: Iterable[Tuple[Optional[str], Optional[str], Optional[str], str]]):
        """Строит матрицу TF-IDF по ядрам названий."""
        from search_experts.conflict_detector import normalize_inn, normalize_kpp

        seen = set()
        self.names: List[str] = []
        self.inns: List[str] = []
        self.kpps: List[Optional[str]] = []
        self.kinds: List[str] = []
        self.cores: List[str] = []
        cores = self.cores
        for name, inn, kpp, kind in entries:
            inn = normalize_inn(inn)
            core = _core(name)
            if not inn or len(core) < 3 or (core, inn) in seen:
                continue
            seen.add((core, inn))
            cores.append(core)
            self.names.append(str(name).strip())
            self.inns.append(inn)
            self.kpps.append(normalize_kpp(kpp))
            self.kinds.append(kind)
        self._resolved: Dict[tuple, Optional[OrgMatch]] = {}   # кэш resolve: справочник живёт TTL на процесс
        self.vectorizer = None
        self.matrix = None
        if cores:
            from sklearn.feature_extraction.text import TfidfVectorizer
            self.vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), sublinear_tf=True)
            self.matrix = self.vectorizer.fit_transform(cores)

    def __len__(self) -> int:
        """Число записей справочника."""
        return len(self.inns)

    def resolve(self, name: Optional[str], threshold: float, margin: float, top: int = 10,
                branch_threshold: Optional[float] = None) -> Optional[OrgMatch]:
        """Ищет организацию по названию.

        Кандидаты — записи со сходством ≥ ``threshold`` (не больше ``top`` лучших); из них берутся только прошедшие
        проверку слов (:func:`covers`): символьное сходство «Иркутского государственного медицинского университета»
        с «Иркутским государственным университетом» 0.82, но слова «медицинский» в нём нет.

        Args:
            name: Название в произвольной записи («ФГБОУ ВО РЭУ им. Г.В. Плеханова»).
            threshold: Минимальное сходство.
            margin: Если среди прошедших проверку есть другая организация (другой ИНН), отстающая от лучшей меньше чем
                на ``margin``, совпадение неоднозначно — ИНН не восстанавливается.
            top: Сколько лучших записей проверять.
            branch_threshold: Порог для названия подразделения («… институт (филиал) ФГБОУ ВО X»): длинное
                название снижает сходство, а структуру проверяет :func:`covers`. ``None`` — как ``threshold``.

        Returns:
            Optional[OrgMatch]: Найденная организация или ``None``.
        """
        if self.matrix is None:
            return None
        key = (name, threshold, margin, top, branch_threshold)
        if key not in self._resolved:
            self._resolved[key] = self._resolve(name, threshold, margin, top, branch_threshold)
        return self._resolved[key]

    def _resolve(self, name, threshold, margin, top, branch_threshold) -> Optional[OrgMatch]:
        """Поиск без кэша (см. :meth:`resolve`): названия кандидатов повторяются от запроса к запросу, поэтому
        результат кэшируется на время жизни справочника (на 900 кандидатах без кэша — ~2,7 с на запрос)."""
        core = _core(name)
        if len(core) < 3:
            return None
        if branch_threshold is not None and any(w in ("филиал", "при") for w in core.split()):
            threshold = min(threshold, branch_threshold)
        sims = (self.vectorizer.transform([core]) @ self.matrix.T).toarray()[0]
        passed = []
        for j in sims.argsort()[::-1][:top]:
            j = int(j)
            if sims[j] < threshold:
                break
            if covers(core, self.cores[j]):
                passed.append(j)
        if not passed:
            return None
        best = passed[0]
        for j in passed[1:]:
            if sims[j] < sims[best] - margin:
                break
            if self.inns[j] != self.inns[best]:
                return None
        return OrgMatch(self.inns[best], self.kpps[best], float(sims[best]), self.names[best], self.kinds[best])


# Слова, которые организация может иметь в официальном названии и не писать в обиходном («национальный
# исследовательский Нижегородский государственный университет»)
_OPTIONAL_WORDS = {"национальный", "исследовательский"}


def _match(word: str, others) -> bool:
    """Слово есть среди ``others`` с допуском на опечатку и падеж."""
    from search_experts.conflict_detector import _same_token
    return any(_same_token(word, o) or same_stem(word, o) for o in others)


def covers(query_core: str, entry_core: str) -> bool:
    """Название эксперта и название справочника — одна организация по словам (в обе стороны).

    1. Все значимые слова запроса есть в названии справочника (с допуском на опечатку и падеж). Для подразделения
       («северо-кавказский институт филиал ранхигс …») — слова после «филиал» / «при»: это и есть работодатель.
    2. Лишние слова названия справочника допустимы только: в хвосте «им …» (почётное имя), из
       ``_OPTIONAL_WORDS``, короткие (до 5 букв — аббревиатуры «ринх», «мгюа») и до «филиал» / «при» (запись
       справочника — филиал организации из запроса). Иначе лишнее слово различает организации:
       «Новосибирский государственный университет» ≠ «… технический университет».

    Слова короче 3 букв (предлоги, инициалы) не учитываются.

    Args:
        query_core: Ядро названия эксперта.
        entry_core: Ядро названия справочника.

    Returns:
        bool: ``True`` — одна организация.
    """
    def after_marker(words):
        for i, w in enumerate(words):
            if w in ("филиал", "при"):
                return i
        return -1

    q = [w for w in query_core.split() if len(w) >= 3 or w == "им"]
    e = [w for w in entry_core.split() if len(w) >= 3 or w == "им"]
    qm = after_marker(q)
    q_main = q[qm + 1:] if qm >= 0 else q
    q_main = [w for w in q_main if w != "им"]
    if not q_main or not all(_match(w, e) for w in q_main):
        return False
    em = after_marker(e)
    im = e.index("им") if "им" in e else len(e)
    for i, w in enumerate(e):
        if w == "им" or i > im or i < em or w in _OPTIONAL_WORDS or len(w) <= 5 or w in ("филиал", "при"):
            continue
        if not _match(w, q):
            return False
    return True


def same_stem(a: str, b: str) -> bool:
    """Одно слово в разных падежах: общее начало не короче 5 букв и отличается только окончанием (до 3 букв).

    «академии» / «академия», «государственного» / «государственный», «российской» / «российская».

    Args:
        a: Первое слово.
        b: Второе слово.

    Returns:
        bool: ``True`` — основа общая.
    """
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n >= 5 and n >= max(len(a), len(b)) - 3


# ---------------------------------------------------------------- загрузка и кэш на процесс

# Кэш без блокировки: Celery запускает каждую задачу в своём цикле событий (asyncio.run), а asyncio.Lock
# привязывается к циклу; два одновременных построения справочника безвредны
_CACHE: Dict[str, Tuple[float, OrgDirectory]] = {}


async def load_aliases(connection=None) -> List[Tuple[str, str, Optional[str], str]]:
    """Псевдонимы из внутренней БД (``org_name_aliases``).

    Args:
        connection: Фабрика соединений (по умолчанию ``knowledge_store.db.connection``).

    Returns:
        List[tuple]: ``(название, ИНН, КПП, "alias")``; при недоступной БД или до миграции 012 — пусто.
    """
    if connection is None:
        from knowledge_store.db import connection
    try:
        async with connection() as conn:
            rows = await conn.fetch("SELECT alias, inn, kpp FROM org_name_aliases")
        return [(r["alias"], r["inn"], r["kpp"], "alias") for r in rows]
    except Exception as e:  # noqa: BLE001 — справочник работает и без псевдонимов
        logger.warning(f"Псевдонимы организаций не загружены: {type(e).__name__}: {e}")
        return []


async def get_directory(env: str, data_fetcher, connection=None) -> OrgDirectory:
    """Справочник окружения из кэша процесса или заново (портал + псевдонимы).

    Args:
        env: Окружение.
        data_fetcher: ``DataFetcher`` окружения (запрос ``queries/org_directory.sql``).
        connection: Фабрика соединений с внутренней БД.

    Returns:
        OrgDirectory: Справочник.
    """
    from configs import settings
    from configs.utils import load_sql_query_async

    ttl = float(getattr(settings, "CONFLICT_ORG_DIRECTORY_TTL_SEC", 3600))
    cached = _CACHE.get(env)
    if cached and time.monotonic() - cached[0] < ttl:
        return cached[1]
    df = await data_fetcher.fetch_async_expertise_data(await load_sql_query_async("org_directory.sql"), bindings=[])
    rows = [(r.get("name"), r.get("inn"), r.get("kpp"), r.get("kind")) for r in df.to_dict("records")] if len(df) else []
    rows += await load_aliases(connection)
    directory = await asyncio.to_thread(OrgDirectory, rows)
    _CACHE[env] = (time.monotonic(), directory)
    logger.info(f"Справочник организаций {env}: {len(directory)} записей")
    return directory


def resolve_frame(df, directory: OrgDirectory, threshold: float, margin: float, branch_threshold: Optional[float] = None):
    """Восстанавливает ИНН места работы и диплома у кандидатов без реквизитов.

    Добавляет столбцы ``expert_organization_inn_guess``, ``expert_organization_guess`` (название и сходство из
    справочника) и такие же для диплома (``expert_diplom_*``). У кого ИНН есть в анкете — не трогается.

    Args:
        df (pandas.DataFrame): Кандидаты (``expert_organization``, ``expert_organization_inn``, ``expert_diplom``,
            ``expert_diplom_inn``).
        directory: Справочник.
        threshold: Порог сходства.
        margin: Зазор неоднозначности.
        branch_threshold: Порог для названий подразделений (см. :meth:`OrgDirectory.resolve`).

    Returns:
        pandas.DataFrame: Тот же датафрейм с новыми столбцами.
    """
    from search_experts.conflict_detector import normalize_inn

    for field in ("organization", "diplom"):
        inns, notes = [], []
        for name, inn in zip(df.get(f"expert_{field}", [None] * len(df)), df.get(f"expert_{field}_inn", [None] * len(df))):
            match = None
            if not normalize_inn(inn) and isinstance(name, str) and name.strip():
                match = directory.resolve(name, threshold, margin, branch_threshold=branch_threshold)
            inns.append(match.inn if match else None)
            notes.append(f"{match.name} ({match.score:.2f}, {match.kind})" if match else None)
        df[f"expert_{field}_inn_guess"] = inns
        df[f"expert_{field}_guess"] = notes
    return df
