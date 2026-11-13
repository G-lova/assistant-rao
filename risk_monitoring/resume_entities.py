"""Организации, упомянутые в заключениях модели (``ai_analysis.resume_entities``, 12.11.2026).

Дашборды делают названия заказчиков и поставщиков в тексте модели ссылками на карточку организации. Искать названия
по всему справочнику ненадёжно (короткие названия дают ложные ссылки), поэтому сервис передаёт рядом с текстом
организации, которые были во входных данных модели:

* закупка — её заказчик и поставщик (паспорт закупки);
* организация — она сама и её крупнейшие контрагенты (``top_suppliers`` / ``top_customers`` профиля);
* сводка дня — организации всех блоков сводки (внимание, приоритеты, закупки, события риска, заказчики и поставщики);
* сводка недели и месяца — организации ежедневных сводок.

Запись: ``{"role": "customer" | "supplier", "name", "short_name", "inn", "kpp", "organisation_id", "mentions"}`` —
пустые поля не пишутся. ``mentions`` — упоминания организации **так, как они написаны в тексте** (с падежными
окончаниями: «филиала РГСУ в г. Майкопе»), чтобы дашборды не угадывали склонения (13.11.2026). Упомянутые в тексте
организации идут первыми; список не длиннее ``MAX_ENTITIES`` (было 50 — у сводки дня названия из текста не попадали
в список, 13.11.2026). Сопоставление с текстом делают дашборды (ТЗ от 12.11.2026, п. 2.3); формат API не меняется.
"""
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROLES = ("customer", "supplier")
# организационно-правовые формы и типовые сокращения в начале названия — для проверки упоминания в тексте
_FORMS = re.compile(r"^(?:(?:ФГБОУ|ФГАОУ|ФГБУН|ФГБНУ|ФГБУ|ФГУП|ФГКУ|ФГАУ|ФБУН|ФКУ|ГБУ|ГАУ|МБУ|АНО|ООО|ОАО|ЗАО|ПАО|АО|ИП|"
                    r"ВО|ДПО|НКО)\s+)+", re.I)
_QUOTES = re.compile(r"[«»\"'“”„]")
MAX_ENTITIES = 500
_WORD = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")


def _clean(v: Any) -> Optional[str]:
    """Строка без лишних пробелов; пустое — ``None``."""
    s = re.sub(r"\s+", " ", str(v or "")).strip()
    return s or None


def entity(role: str, name: Any = None, inn: Any = None, kpp: Any = None,
           organisation_id: Any = None, short_name: Any = None) -> Optional[Dict[str, Any]]:
    """Запись об организации; ``None`` — нет ни названия, ни ИНН, ни id или роль неизвестна.

    Args:
        role: ``customer`` или ``supplier``.
        name: Название (как его видела модель).
        inn: ИНН.
        kpp: КПП.
        organisation_id: id строки справочника организаций внешней БД.
        short_name: Сокращённое название (справочник организаций), если отличается от ``name``.

    Returns:
        Optional[Dict[str, Any]]: ``{role, name?, short_name?, inn?, kpp?, organisation_id?}``.
    """
    if role not in ROLES:
        return None
    out: Dict[str, Any] = {"role": role}
    short = _clean(short_name)
    for k, v in (("name", _clean(name) or short), ("short_name", short if short != (_clean(name) or short) else None),
                 ("inn", _clean(inn)), ("kpp", _clean(kpp))):
        if v:
            out[k] = v
    try:
        if organisation_id not in (None, ""):
            out["organisation_id"] = int(organisation_id)
    except (TypeError, ValueError):
        pass
    return out if len(out) > 1 else None


def merge(items: Iterable[Optional[Dict[str, Any]]], limit: int = MAX_ENTITIES) -> List[Dict[str, Any]]:
    """Список без повторов: одна запись на роль + ИНН/КПП (или id, или название); поля повторов дополняют запись."""
    out: Dict[tuple, Dict[str, Any]] = {}
    for e in items:
        if not e:
            continue
        if e.get("inn"):
            key = (e["role"], "inn", e["inn"], e.get("kpp") or "")
        elif e.get("organisation_id") is not None:
            key = (e["role"], "id", e["organisation_id"])
        else:
            key = (e["role"], "name", norm(e.get("name")))
        if key in out:
            for k, v in e.items():
                out[key].setdefault(k, v)
        elif len(out) < limit:
            out[key] = dict(e)
    return list(out.values())


def norm(text: Any) -> str:
    """Текст для сравнения: нижний регистр, без кавычек и лишних пробелов."""
    return re.sub(r"\s+", " ", _QUOTES.sub(" ", str(text or ""))).strip().lower()


def core_name(name: Any) -> str:
    """Название без организационно-правовой формы и кавычек: «ФГБОУ ВО "НИУ "МЭИ"» → «ниу мэи»."""
    s = re.sub(r"\s+", " ", _QUOTES.sub(" ", str(name or ""))).strip()
    return _FORMS.sub("", s).strip().lower()


def _stem(word: str) -> str:
    """Основа слова для сравнения с падежной формой: без последних букв окончания."""
    if len(word) >= 6:
        return word[:max(4, len(word) - 3)]
    if len(word) >= 4:
        return word[:len(word) - 1]
    return word


def _word_match(name_word: str, text_word: str) -> bool:
    """Слово текста — то же слово названия, возможно в другом падеже; короткие слова и цифры — точно."""
    t = text_word.lower()
    if name_word == t:
        return True
    if len(name_word) < 4 or name_word.isdigit() or name_word.isupper():
        return False
    return t.startswith(_stem(name_word)) and abs(len(t) - len(name_word)) <= 3


def find_mentions(e: Dict[str, Any], text: Any, limit: int = 5) -> List[str]:
    """Упоминания организации в тексте так, как они написаны: название и сокращённое название без
    организационно-правовой формы, с учётом падежных окончаний; ИНН — как есть.

    Название из одного слова короче 6 букв не ищется (как в правилах дашбордов: «МАИ», «КГУ»).

    Args:
        e: Запись :func:`entity`.
        text: Текст модели.
        limit: Не больше стольких разных упоминаний.

    Returns:
        List[str]: Подстроки текста, без повторов, в порядке появления.
    """
    src = str(text or "")
    tokens = list(_WORD.finditer(src))
    found: List[Tuple[int, str]] = []
    for variant in (e.get("name"), e.get("short_name")):
        words = [w.group(0).lower() for w in _WORD.finditer(core_name(variant))]
        if not words or (len(words) == 1 and len(words[0]) < 6):
            continue
        n = len(words)
        for i in range(len(tokens) - n + 1):
            if all(_word_match(words[j], tokens[i + j].group(0)) for j in range(n)):
                start, end = tokens[i].start(), tokens[i + n - 1].end()
                found.append((start, src[start:end]))
    inn = e.get("inn")
    if inn:
        found += [(m.start(), inn) for m in re.finditer(rf"(?<!\d){re.escape(inn)}(?!\d)", src)]
    out: List[str] = []
    for _, span in sorted(found):
        if span not in out:
            out.append(span)
    return out[:limit]


def mentioned(e: Dict[str, Any], text: Any) -> bool:
    """Упомянута ли организация в тексте (:func:`find_mentions`)."""
    return bool(find_mentions(e, text, limit=1))


def finalize(entities: Iterable[Dict[str, Any]], text: Any, limit: int = MAX_ENTITIES) -> List[Dict[str, Any]]:
    """Список для ``ai_analysis``: упоминания из текста (``mentions``), сначала упомянутые организации, не длиннее
    ``limit``.

    Args:
        entities: Записи :func:`entity` (без повторов).
        text: Текст модели, к которому прилагается список.
        limit: Наибольшая длина списка.

    Returns:
        List[Dict[str, Any]]: Записи с ``mentions`` у упомянутых.
    """
    with_m, without = [], []
    for e in entities:
        e = {k: v for k, v in e.items() if k != "mentions"}
        m = find_mentions(e, text)
        if m:
            e["mentions"] = m
            with_m.append(e)
        else:
            without.append(e)
    return (with_m + without)[:limit]


def for_purchase(passport: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Заказчик и поставщик закупки из паспорта."""
    p = passport or {}
    cu, su = p.get("customer") or {}, p.get("supplier") or {}
    return merge([entity("customer", cu.get("name"), cu.get("inn"), cu.get("kpp"), cu.get("organisation_id")),
                  entity("supplier", su.get("name"), su.get("inn"), su.get("kpp"))])


def for_organisation(role: str, org: Dict[str, Any], section: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Организация в своей роли и её крупнейшие контрагенты из профиля.

    Args:
        role: Роль секции: ``customer`` (контрагенты — поставщики) или ``supplier`` (контрагенты — заказчики).
        org: ``name``, ``inn``, ``kpp``, ``organisation_id`` самой организации.
        section: Секция профиля (``top_suppliers`` / ``top_customers``).

    Returns:
        List[Dict[str, Any]]: Записи без повторов.
    """
    sec = section or {}
    own = entity(role, org.get("name"), org.get("inn"), org.get("kpp"), org.get("organisation_id"),
                 org.get("short_name"))
    other = "supplier" if role == "customer" else "customer"
    top = sec.get("top_suppliers") if role == "customer" else sec.get("top_customers")
    return merge([own] + [entity(other, t.get("name"), t.get("inn")) for t in top or []])


def for_daily(summary: Dict[str, Any], items: Sequence[Dict[str, Any]], top: Optional[int] = None) -> List[Dict[str, Any]]:
    """Организации всех блоков ежедневной сводки (до текста модели; упоминания — :func:`finalize`).

    Args:
        summary: Результат ``daily.build_summary`` (без текста).
        items: Закупки сводки (``contract_item``): по ``card_id`` берутся заказчик и его ``organisation_id``.
        top: Не используется: раньше брались только первые ``DAILY_TOP`` заказчиков и поставщиков, и названия из
            текста не попадали в список (13.11.2026).

    Returns:
        List[Dict[str, Any]]: Записи без повторов.
    """
    by_card = {i.get("card_id"): i for i in items}
    cards: List[Any] = []
    for key in ("attention", "priorities"):
        cards += [x.get("card_id") for x in summary.get(key) or []]
    for rows in (summary.get("contracts") or {}).values():
        if isinstance(rows, list):
            cards += [x.get("card_id") for x in rows if isinstance(x, dict)]
    for key in ("new", "closed"):
        cards += [x.get("card_id") for x in (summary.get("risk_changes") or {}).get(key) or []]
    out: List[Optional[Dict[str, Any]]] = []
    for cid in cards:
        i = by_card.get(cid)
        if i:
            out.append(entity("customer", i.get("customer"), i.get("customer_inn"), i.get("customer_kpp"),
                              i.get("organisation_id")))
    out += [entity("customer", c.get("name"), c.get("inn"), c.get("kpp"), c.get("organisation_id"))
            for c in summary.get("customers") or []]
    out += [entity("supplier", s.get("name"), s.get("inn"), s.get("kpp"), s.get("organisation_id"))
            for s in summary.get("suppliers") or []]
    return merge(out, limit=10 ** 6)


def for_period(dailies: Iterable[Dict[str, Any]], text: Any) -> List[Dict[str, Any]]:
    """Организации ежедневных сводок периода: упомянутые в тексте сводки недели или месяца — первыми, с упоминаниями."""
    pool = merge((e for d in dailies for e in (d or {}).get("resume_entities") or []), limit=10 ** 6)
    return finalize(pool, text)
