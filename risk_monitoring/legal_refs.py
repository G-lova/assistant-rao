"""Проверка ссылок модели на нормы (поле ``law`` риска) по загруженным редакциям актов (``legal_acts``).

Модель пишет нормы по памяти: «ч. 6 ст. 96 44-ФЗ», «пп. "а" п. 3 Правил, утв. ПП РФ № 1042», «п. 3 ст. 164 НК РФ».
Ссылка разбирается на уровни (:func:`parse_refs`) и ищется в нормах редакции, действующей на дату (:class:`LawChecker`).

Статусы ссылки (``law_check.refs[].status``):

* ``found`` — норма есть; ``address`` — адрес в тексте, ``excerpt`` — начало нормы (эксперту не нужно открывать закон);
* ``lost_force`` — норма утратила силу;
* ``not_found`` — такой нормы в редакции нет (нет части / пункта / статьи) — ссылка, вероятно, выдумана;
* ``unknown_act`` — акт не загружен или не распознан.

Общий статус риска (``law_check.status``) — худший из статусов разобранных ссылок: ``not_found`` → ``lost_force``
→ ``found``; если ни одну ссылку проверить нельзя — ``unknown_act``; если ссылок нет — ``no_ref``.
Балл риска проверка не меняет: это подсказка эксперту (и материал для калибровки промпта).
"""
import re
from typing import Any, Dict, List, Optional, Tuple

# акт по упоминанию в ссылке; порядок важен — постановления раньше законов («ПП № 1042 по 44-ФЗ»)
ACT_PATTERNS: List[Tuple["re.Pattern", Any]] = [
    (re.compile(r"(постановлени\w*|ПП)\b[^;]{0,120}?(?:№|N)\s*(\d{2,4})(?!\d|-ФЗ)", re.I), lambda m: f"ПП-{m.group(2)}"),
    (re.compile(r"\b44-ФЗ|закон\w*\s+о\s+контрактной\s+системе", re.I), "44-ФЗ"),
    (re.compile(r"\b223-ФЗ|закон\w*\s+о\s+закупках\s+товаров", re.I), "223-ФЗ"),
    (re.compile(r"\b135-ФЗ|о\s+защите\s+конкуренции", re.I), "135-ФЗ"),
    (re.compile(r"\bНК\s*(РФ)?\b|налогов\w*\s+кодекс", re.I), "НК"),
    (re.compile(r"\bБК\s*(РФ)?\b|бюджетн\w*\s+кодекс", re.I), "БК"),
    (re.compile(r"\bГК\s*(РФ)?\b|гражданск\w*\s+кодекс", re.I), "ГК"),
]

NUM = r"(\d{1,3}(?:\.\d{1,3}){0,2})"
LETTER = r"[«\"']?([а-я])[»\"']?(?![а-я])"
# уровни ссылки: подпункт буквой, подпункт цифрой (кодекс, постановление), пункт, часть, статья
TOKEN_RE = re.compile(
    r"(?<![а-яё])(?:"
    r"(?P<sub>(?:пп\.|подп\.|подпункт\w*)\s*(?:" + LETTER + r"|" + NUM + r"))"
    r"|(?P<point>(?:п\.|пункт\w*)\s*" + NUM + r")"
    r"|(?P<part>(?:ч\.|част\w*)\s*" + NUM + r")"
    r"|(?P<art>(?:ст\.|стать\w*)\s*" + NUM + r"))",
    re.I,
)

WORST = ("not_found", "lost_force", "found")


def _act(text: str) -> Optional[str]:
    """Ключ акта по упоминанию в тексте ссылки."""
    for rx, key in ACT_PATTERNS:
        m = rx.search(text)
        if m:
            return key(m) if callable(key) else key
    return None


def parse_refs(law: Optional[str], default_act: Optional[str] = None) -> List[Dict[str, Any]]:
    """Ссылки на нормы из поля ``law``.

    Ссылки разделяются «;» и союзами; акт без явного упоминания — предыдущий в строке или ``default_act`` (закон
    закупки). Уровни — как написала модель: ``article``, ``part``, ``point``, ``sub_num``, ``sub_letter``.

    Args:
        law: Текст нормы из риска.
        default_act: Ключ акта по умолчанию (``44-ФЗ`` / ``223-ФЗ``).

    Returns:
        List[Dict[str, Any]]: ``ref`` (фрагмент), ``act``, уровни, ``segment_hint`` («Правил» → ``правила``).
    """
    if not law:
        return []
    out: List[Dict[str, Any]] = []
    last_act = None
    for piece in re.split(r";|\s+и\s+(?=(?:ч\.|ст\.|п\.|пп\.|част|стать|пункт|подпункт))|,\s+(?=(?:ч\.|ст\.|пп?\.)\s*\d)",
                          law):
        piece = piece.strip()
        if not piece:
            continue
        act = _act(piece) or last_act or default_act
        ref: Dict[str, Any] = {"ref": piece[:200], "act": act}
        for m in TOKEN_RE.finditer(piece):
            if m.group("sub"):
                letter, num = m.group(2), m.group(3)
                ref.setdefault("sub_letter" if letter else "sub_num", letter or num)
            elif m.group("point"):
                ref.setdefault("point", m.group(5))
            elif m.group("part"):
                ref.setdefault("part", m.group(7))
            elif m.group("art"):
                ref.setdefault("article", m.group(9))
        if re.search(r"\bправил", piece, re.I):
            ref["segment_hint"] = "правила"
        elif re.search(r"\bположени[яе]\b", piece, re.I):
            ref["segment_hint"] = "положение"
        if _act(piece):
            last_act = act
        # НК РФ — две части разными законами: ст. 1–142 — часть первая (146-ФЗ), с 143 — часть вторая (117-ФЗ)
        art = ref.get("article")
        if act == "НК" and art and int(art.split(".")[0]) < 143:
            ref["act"] = "НК-1"
        if any(ref.get(k) for k in ("article", "part", "point", "sub_letter", "sub_num")):
            out.append(ref)
    return out


def ref_keys(ref: Dict[str, Any], kind: str) -> List[Tuple[Optional[str], ...]]:
    """Варианты ключа ``(lvl1, lvl2, lvl3, lvl4)`` для ссылки по виду акта (``None`` — уровень не указан).

    В законе «п. 2 ст. 3» без части часто означает часть 2 (223-ФЗ), поэтому пробуются оба прочтения.
    """
    a, p, pt, sn, sl = (ref.get(k) for k in ("article", "part", "point", "sub_num", "sub_letter"))
    if kind == "law":
        keys = [(a, p, pt, sl)]
        if p is None and pt is not None:
            keys.append((a, pt, sn, sl))
        return keys
    if kind == "code":
        return [(a, pt or p, sn, sl)]
    return [(None, pt, sn, sl)]


def _matches(norm: Dict[str, Any], key: Tuple[Optional[str], ...]) -> bool:
    """Норма соответствует ключу: указанные уровни совпадают, не указанные — любые."""
    return all(k is None or norm.get(f"lvl{i + 1}") == k for i, k in enumerate(key))


def check_ref(ref: Dict[str, Any], kind: str, norms: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Проверка одной ссылки по нормам редакции.

    Returns:
        Dict[str, Any]: ``status``, ``address``, ``excerpt``, ``reason`` (для ``not_found`` — какой уровень не найден).
    """
    hint = ref.get("segment_hint")
    pool = norms
    if kind == "decree":
        # пункт Правил ≠ пункт самого постановления: сначала сегмент, названный в ссылке, затем любой утверждённый
        named = [n for n in norms if hint and (n.get("segment") or "").lower().startswith(hint)]
        approved = [n for n in norms if n.get("segment")]
        pool = named or approved or norms
    for key in ref_keys(ref, kind):
        hits = [n for n in pool if _matches(n, key)]
        if hits:
            # точная норма (без более глубоких уровней) — «ч. 6», а не её «п. 1»; иначе первая по тексту
            exact = [n for n in hits if all(n.get(f"lvl{i + 1}") == key[i] for i in range(4))]
            best = exact[0] if exact else hits[0]
            status = "lost_force" if best.get("lost_force") else "found"
            return {"status": status, "address": best["address"], "excerpt": (best.get("text") or "")[:300]}
    # какой уровень не нашёлся — для пояснения эксперту
    key = ref_keys(ref, kind)[0]
    reason = None
    for depth in range(1, 5):
        partial = tuple(key[:depth]) + (None,) * (4 - depth)
        if key[depth - 1] is not None and not any(_matches(n, partial) for n in pool):
            reason = ("статьи", "части" if kind == "law" else "пункта", "пункта" if kind == "law" else "подпункта",
                      "подпункта")[depth - 1] + f" {key[depth - 1]}"
            break
    return {"status": "not_found", "reason": f"нет {reason}" if reason else "норма не найдена"}


def summarize(results: List[Dict[str, Any]]) -> str:
    """Общий статус по ссылкам."""
    statuses = {r["status"] for r in results}
    for s in WORST:
        if s in statuses:
            return s
    return "unknown_act" if results else "no_ref"


class LawChecker:
    """Проверка ссылок рисков по редакциям в Postgres; редакции и нормы кэшируются на время жизни объекта.

    Args:
        pool: Пул ``asyncpg`` (``AIRepo.pool``).
    """

    def __init__(self, pool: Any):
        """Создаёт репозиторий актов и кэш."""
        from risk_monitoring.legal_acts import LegalActsRepo

        self.acts = LegalActsRepo(pool)
        self._editions: Dict[Tuple[str, Optional[str]], Optional[Dict[str, Any]]] = {}
        self._norms: Dict[int, List[Dict[str, Any]]] = {}

    async def _edition(self, act_key: str, on: Optional[str]) -> Optional[Dict[str, Any]]:
        """Редакция на дату с нормами (``None`` — акт не загружен)."""
        k = (act_key, on)
        if k not in self._editions:
            ed = await self.acts.edition_on(act_key, on)
            if ed and ed["id"] not in self._norms:
                self._norms[ed["id"]] = await self.acts.norms(ed["id"])
            self._editions[k] = ed
        return self._editions[k]

    async def check(self, law: Optional[str], default_act: Optional[str] = None,
                    on: Optional[str] = None) -> Dict[str, Any]:
        """Проверка поля ``law``.

        Args:
            law: Текст нормы.
            default_act: Акт по умолчанию (закон закупки).
            on: Дата ``YYYY-MM-DD`` (редакция на дату; по умолчанию сегодня).

        Returns:
            Dict[str, Any]: ``status`` и ``refs`` (ссылка, акт, редакция, статус, адрес, отрывок).
        """
        refs = []
        for ref in parse_refs(law, default_act):
            ed = await self._edition(ref["act"], on) if ref.get("act") else None
            if not ed:
                refs.append({"ref": ref["ref"], "act": ref.get("act"), "status": "unknown_act"})
                continue
            res = check_ref(ref, ed["kind"], self._norms[ed["id"]])
            refs.append({"ref": ref["ref"], "act": ref["act"],
                         "edition": ed["edition_date"].isoformat() if hasattr(ed["edition_date"], "isoformat")
                         else ed["edition_date"], **res})
        checked = [r for r in refs if r["status"] != "unknown_act"]
        status = summarize(checked) if checked else ("unknown_act" if refs else "no_ref")
        return {"status": status, "refs": refs}

    async def annotate(self, risks: List[Dict[str, Any]], fz: Any = None, on: Optional[str] = None) -> Dict[str, int]:
        """Добавляет ``law_check`` к рискам с нормой (на месте). Риски без проверяемых ссылок не меняются.

        Returns:
            Dict[str, int]: Число рисков по общему статусу.
        """
        default_act = f"{fz}-ФЗ" if fz in (44, 223, "44", "223") else None
        counts: Dict[str, int] = {}
        for r in risks or []:
            if not r.get("law"):
                continue
            res = await self.check(r["law"], default_act, on)
            counts[res["status"]] = counts.get(res["status"], 0) + 1
            if res["status"] not in ("no_ref", "unknown_act"):
                r["law_check"] = res
        return counts
