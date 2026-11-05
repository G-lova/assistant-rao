"""Соавторство в конфликте интересов при подборе экспертов.

Соавторы кандидата — авторы его разобранных публикаций (``expert_publications``, источник ``done``, эксперт среди
авторов или авторы не проверены) за последние ``CONFLICT_COAUTHOR_YEARS`` лет (год неизвестен — учитывается).

* **Соавтор — контактное лицо или руководитель заказчика** (``coauthor_customer``): эксперт исключается.
* **Два кандидата — соавторы** (``coauthor_expert``): остаётся тот, у кого выше сходство с экспертизой
  (как при совпадении фамилий, ``ConflictDetector.find_nepotism``).

Люди сравниваются по фамилии и инициалам: «Жанаева С.Я.», «Жанаева Светлана Яковлевна», «S.Ya. Zhanaeva» — один
человек; фамилия должна совпасть точно (с учётом ё/е и транслитерации: «Иванов» и «Иванова» — разные люди), первый
инициал — обязательно, второй — если указан у обоих. Без инициалов совпадение не засчитывается (однофамильцы).
"""
import datetime
import json
import re
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

from configs.logger import get_logger
from search_experts.publications.authors import translit

logger = get_logger(__name__)


class Person(NamedTuple):
    """Человек для сравнения: фамилия и инициалы (нижний регистр, «ё» → «е»; латиница — как есть).

    Attributes:
        surname: Фамилия.
        initials: Инициалы — первые буквы имени и отчества (``("с", "я")``; латиница: ``("s", "ya")``).
    """

    surname: str
    initials: Tuple[str, ...]


_INITIAL_RE = re.compile(r"^([^\W\d_]{1,2})\.?$")


def parse_person(text: Optional[str]) -> Optional[Person]:
    """Разбирает ФИО в любой записи: «Иванов И.И.», «И.И. Иванов», «Иванов Иван Иванович», «Ivanov, I.», «S.Ya. Zhanaeva».

    Фамилия — первое полное слово (не инициал), если инициалы идут после него или ФИО полное; иначе — слово после
    инициалов. Латинский диграф инициала («Ya.», «Zh.») сохраняется.

    Args:
        text: ФИО.

    Returns:
        Optional[Person]: Человек или ``None`` (нет фамилии или инициалов, организация вместо ФИО).
    """
    if not isinstance(text, str):
        return None
    t = text.strip().lower().replace("ё", "е")
    t = re.sub(r"([^\W\d_])\.(?=[^\W\d_])", r"\1. ", t)       # «и.и.иванов» → «и. и. иванов»
    parts = [p for p in re.split(r"[\s,]+", t) if p]
    if not 2 <= len(parts) <= 4:
        return None
    words, initials, order = [], [], []
    for p in parts:
        m = _INITIAL_RE.match(p)
        if m and (p.endswith(".") or len(m.group(1)) == 1):
            initials.append(m.group(1))
            order.append("i")
        elif re.fullmatch(r"[^\W\d_]+(?:-[^\W\d_]+)?", p.rstrip(".")) and len(p.rstrip(".")) >= 2:
            words.append(p.rstrip("."))
            order.append("w")
        else:
            return None
    if not words:
        return None
    if initials and len(words) > 1:   # «Иванов И.И. Петров» — склейка двух авторов, неоднозначно
        return None
    if initials:
        surname = words[0]
    else:  # «Иванов Иван Иванович»: фамилия первая
        surname, initials = words[0], [w[0] for w in words[1:3]]
    if not initials or len(surname) < 2:
        return None
    return Person(surname, tuple(initials[:2]))


def parse_people(text: Optional[str]) -> List[Person]:
    """Разбирает поле с одним или несколькими ФИО через «;» или «,» (контактные лица заказчика).

    «Ivanov, I.» — один человек (после запятой только инициалы), «Шамшурин Виталий Викторович, Толмачева Эльвира
    Анатольевна» — два.

    Args:
        text: Значение поля.

    Returns:
        List[Person]: Разобранные люди (пусто — не ФИО, например название организации).
    """
    if not isinstance(text, str) or not text.strip():
        return []
    parts = [p.strip() for p in re.split(r"[;,]", text) if p.strip()]
    if len(parts) > 1 and all(len(p.split()) >= 2 for p in parts):
        people = [parse_person(p) for p in parts]
        return [p for p in people if p]
    one = parse_person(text)
    return [one] if one else []


def _latin(person: Person) -> Person:
    """Латинская запись человека (транслитерация фамилии и инициалов)."""
    return Person(translit(person.surname), tuple(translit(i) for i in person.initials))


def same_person(a: Optional[Person], b: Optional[Person]) -> bool:
    """Один ли это человек: фамилия точно, первый инициал, второй — если есть у обоих (кириллица ↔ латиница).

    Args:
        a: Первый человек.
        b: Второй человек.

    Returns:
        bool: ``True`` — совпадение.
    """
    if not a or not b:
        return False
    for x, y in ((a, b), (_latin(a), _latin(b)), (_latin(a), b), (a, _latin(b))):
        if x.surname != y.surname:
            continue
        if not (x.initials[0].startswith(y.initials[0]) or y.initials[0].startswith(x.initials[0])):
            continue
        if len(x.initials) > 1 and len(y.initials) > 1 and not (
                x.initials[1].startswith(y.initials[1]) or y.initials[1].startswith(x.initials[1])):
            continue
        return True
    return False


def coauthors_of(name: Optional[str], authors: Iterable[str]) -> List[Person]:
    """Соавторы из списка авторов публикации — все, кроме самого эксперта.

    Args:
        name: ФИО эксперта.
        authors: Авторы публикации.

    Returns:
        List[Person]: Разобранные соавторы.
    """
    me = parse_person(name)
    out = []
    for a in authors or []:
        p = parse_person(a)
        if p and not same_person(p, me):
            out.append(p)
    return out


async def load_coauthors(env: str, expert_ids: Sequence[int], min_year: Optional[int],
                         connection=None) -> Dict[int, List[Tuple[str, Optional[int], List[str]]]]:
    """Авторы публикаций кандидатов из внутренней БД.

    Args:
        env: Окружение.
        expert_ids: Кандидаты.
        min_year: Публикации не раньше этого года (``None`` — все; год неизвестен — учитывается).
        connection: Фабрика соединений (по умолчанию ``knowledge_store.db.connection``).

    Returns:
        Dict[int, list]: ``expert_id`` → ``[(название, год, [авторы]), …]``.
    """
    if connection is None:
        from knowledge_store.db import connection
    year_sql = "AND (p.year IS NULL OR p.year >= $3)" if min_year else ""
    params = [env, [int(i) for i in expert_ids]] + ([int(min_year)] if min_year else [])
    async with connection() as conn:
        rows = await conn.fetch(
            f"""
            SELECT p.expert_id, p.title, p.year, p.authors
            FROM expert_publications p JOIN expert_pub_sources s ON s.id = p.source_id AND s.status = 'done'
            WHERE p.env = $1 AND p.expert_id = ANY($2::bigint[]) AND p.authors IS NOT NULL
              AND p.author_verified IS NOT FALSE {year_sql}
            """, *params)
    out: Dict[int, list] = {}
    for r in rows:
        authors = json.loads(r["authors"]) if isinstance(r["authors"], (str, bytes)) else r["authors"]
        if authors:
            out.setdefault(int(r["expert_id"]), []).append((r["title"], r["year"], list(authors)))
    return out


def customer_coauthor_conflicts(df, publications: Dict[int, list]) -> Dict[int, str]:
    """Эксперты, у которых соавтор — контактное лицо или руководитель заказчика.

    Args:
        df (pandas.DataFrame): Кандидаты (``expert_id``, ``expert_name``, ``expertise_name``,
            ``expertise_director_name``).
        publications: Результат :func:`load_coauthors`.

    Returns:
        Dict[int, str]: ``expert_id`` → пояснение («Маликов А.А. — соавтор: <название>»).
    """
    if df.empty:
        return {}
    first = df.iloc[0]
    customer = [(n, p) for n in (first.get("expertise_name"), first.get("expertise_director_name"))
                for p in parse_people(n)]
    out = {}
    if not customer:
        return out
    for eid, name in df[["expert_id", "expert_name"]].drop_duplicates("expert_id").itertuples(index=False):
        for title, year, authors in publications.get(int(eid), []):
            hit = next(((n, p) for p in coauthors_of(name, authors) for n, c in customer if same_person(p, c)), None)
            if hit:
                out[int(eid)] = f"{hit[0]} — соавтор: {str(title)[:120]} ({year or 'год ?'})"
                break
    return out


def _surname_keys(person: Person) -> Set[str]:
    """Ключи индекса по фамилии: как написано и в транслитерации (латинская запись соавтора — тот же ключ)."""
    return {person.surname, translit(person.surname)}


def expert_coauthor_pairs(df, publications: Dict[int, list]) -> List[Tuple[int, int, str]]:
    """Пары кандидатов-соавторов.

    Кандидаты индексируются по фамилии (кириллица и транслитерация), поэтому соавтор публикации сравнивается только
    с однофамильцами-кандидатами: O(соавторов), а не O(публикаций × кандидатов) — на 900 кандидатах и 3,9 тыс.
    публикаций полный перебор занимал ~3 минуты (05.11.2026).

    Args:
        df (pandas.DataFrame): Кандидаты (``expert_id``, ``expert_name``).
        publications: Результат :func:`load_coauthors`.

    Returns:
        List[Tuple[int, int, str]]: ``(expert_id, соавтор-кандидат, название публикации)``.
    """
    names = {int(e): n for e, n in df[["expert_id", "expert_name"]].drop_duplicates("expert_id").itertuples(index=False)}
    index: Dict[str, List[Tuple[int, Person]]] = {}
    for eid, name in names.items():
        person = parse_person(name)
        if person:
            for key in _surname_keys(person):
                index.setdefault(key, []).append((eid, person))
    pairs, seen = [], set()
    for eid, pubs in publications.items():
        if eid not in names:
            continue
        for title, year, authors in pubs:
            for co in coauthors_of(names[eid], authors):
                found = index.get(co.surname, []) + index.get(translit(co.surname), [])
                for other, person in found:
                    pair = frozenset((eid, other))
                    if other == eid or pair in seen or not same_person(co, person):
                        continue
                    seen.add(pair)
                    pairs.append((eid, other, str(title)[:120]))
    return pairs


def exclude_coauthor_pairs(df, pairs: List[Tuple[int, int, str]]) -> Set[int]:
    """Из каждой пары соавторов исключается кандидат с меньшим ``similarity_embeddings``.

    Args:
        df (pandas.DataFrame): Кандидаты со столбцом ``similarity_embeddings``.
        pairs: Результат :func:`expert_coauthor_pairs`.

    Returns:
        Set[int]: Исключаемые ``expert_id``.
    """
    sim = df.drop_duplicates("expert_id").set_index("expert_id")["similarity_embeddings"].to_dict()
    out = set()
    for a, b, _ in pairs:
        if a in out or b in out:
            continue
        out.add(a if sim.get(a, 0) < sim.get(b, 0) else b)
    return out


def min_year(years: Optional[int]) -> Optional[int]:
    """Первый учитываемый год соавторства (``None`` — без ограничения).

    Args:
        years: ``CONFLICT_COAUTHOR_YEARS``.

    Returns:
        Optional[int]: Год.
    """
    return datetime.date.today().year - int(years) if years else None
