"""Организации, упомянутые в заключениях модели (``ai_analysis.resume_entities``, 12.11.2026).

Дашборды делают названия заказчиков и поставщиков в тексте модели ссылками на карточку организации. Искать названия
по всему справочнику ненадёжно (короткие названия дают ложные ссылки), поэтому сервис передаёт рядом с текстом
организации, которые были во входных данных модели:

* закупка — её заказчик и поставщик (паспорт закупки);
* организация — она сама и её крупнейшие контрагенты (``top_suppliers`` / ``top_customers`` профиля);
* сводка дня — организации из пунктов, переданных модели (внимание, приоритеты, переходы в красную зону, события
  риска, заказчики и поставщики с оценкой);
* сводка недели и месяца — организации ежедневных сводок, названия или ИНН которых есть в тексте.

Запись: ``{"role": "customer" | "supplier", "name", "inn", "kpp", "organisation_id"}`` — пустые поля не пишутся.
Сопоставление с текстом делают дашборды (ТЗ от 12.11.2026, п. 2.3); формат API не меняется.
"""
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

ROLES = ("customer", "supplier")
# организационно-правовые формы и типовые сокращения в начале названия — для проверки упоминания в тексте
_FORMS = re.compile(r"^(?:(?:ФГБОУ|ФГАОУ|ФГБУН|ФГБНУ|ФГБУ|ФГУП|ФГКУ|ФГАУ|ФБУН|ФКУ|ГБУ|ГАУ|МБУ|АНО|ООО|ОАО|ЗАО|ПАО|АО|ИП|"
                    r"ВО|ДПО|НКО)\s+)+", re.I)
_QUOTES = re.compile(r"[«»\"'“”„]")
MAX_ENTITIES = 50


def _clean(v: Any) -> Optional[str]:
    """Строка без лишних пробелов; пустое — ``None``."""
    s = re.sub(r"\s+", " ", str(v or "")).strip()
    return s or None


def entity(role: str, name: Any = None, inn: Any = None, kpp: Any = None,
           organisation_id: Any = None) -> Optional[Dict[str, Any]]:
    """Запись об организации; ``None`` — нет ни названия, ни ИНН, ни id или роль неизвестна.

    Args:
        role: ``customer`` или ``supplier``.
        name: Название (как его видела модель).
        inn: ИНН.
        kpp: КПП.
        organisation_id: id строки справочника организаций внешней БД.

    Returns:
        Optional[Dict[str, Any]]: ``{role, name?, inn?, kpp?, organisation_id?}``.
    """
    if role not in ROLES:
        return None
    out: Dict[str, Any] = {"role": role}
    for k, v in (("name", _clean(name)), ("inn", _clean(inn)), ("kpp", _clean(kpp))):
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


def mentioned(e: Dict[str, Any], text: Any) -> bool:
    """Упомянута ли организация в тексте: ИНН или название без формы (не короче 4 символов)."""
    t = norm(text)
    if e.get("inn") and e["inn"] in t:
        return True
    core = core_name(e.get("name"))
    return len(core) >= 4 and core in t


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
    own = entity(role, org.get("name"), org.get("inn"), org.get("kpp"), org.get("organisation_id"))
    other = "supplier" if role == "customer" else "customer"
    top = sec.get("top_suppliers") if role == "customer" else sec.get("top_customers")
    return merge([own] + [entity(other, t.get("name"), t.get("inn")) for t in top or []])


def for_daily(summary: Dict[str, Any], items: Sequence[Dict[str, Any]], top: int) -> List[Dict[str, Any]]:
    """Организации из пунктов ежедневной сводки, которые передаются модели.

    Args:
        summary: Результат ``daily.build_summary`` (без текста).
        items: Закупки сводки (``contract_item``): по ``card_id`` берутся заказчик и его ``organisation_id``.
        top: Сколько заказчиков и поставщиков с оценкой передаётся модели (``DAILY_TOP``).

    Returns:
        List[Dict[str, Any]]: Записи без повторов.
    """
    by_card = {i.get("card_id"): i for i in items}
    cards: List[Any] = []
    for key in ("attention", "priorities"):
        cards += [x.get("card_id") for x in summary.get(key) or []]
    for key in ("became_red", "score_jump", "red"):
        cards += [x.get("card_id") for x in (summary.get("contracts") or {}).get(key) or []]
    for key in ("new", "closed"):
        cards += [x.get("card_id") for x in (summary.get("risk_changes") or {}).get(key) or []]
    out: List[Optional[Dict[str, Any]]] = []
    for cid in cards:
        i = by_card.get(cid)
        if i:
            out.append(entity("customer", i.get("customer"), i.get("customer_inn"), i.get("customer_kpp"),
                              i.get("organisation_id")))
    out += [entity("customer", c.get("name"), c.get("inn"), c.get("kpp"), c.get("organisation_id"))
            for c in (summary.get("customers") or [])[:top]]
    out += [entity("supplier", s.get("name"), s.get("inn"), s.get("kpp"), s.get("organisation_id"))
            for s in (summary.get("suppliers") or [])[:top]]
    return merge(out)


def for_period(dailies: Iterable[Dict[str, Any]], text: Any) -> List[Dict[str, Any]]:
    """Организации ежедневных сводок периода, упомянутые в тексте сводки недели или месяца."""
    pool = merge((e for d in dailies for e in (d or {}).get("resume_entities") or []), limit=10 ** 6)
    return merge(e for e in pool if mentioned(e, text))
