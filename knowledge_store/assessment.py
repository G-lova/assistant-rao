"""Поля сводного ЭЗ, которые определяются оценкой модели по документам целиком, а не поиском фактов.

* ``field2_2_2_0`` — метод обоснования НМЦК (выбор): 1 — рыночный, 2 — нормативный, 3 — затратный,
  4 — тарифный, 5 — проектно-сметный. Сначала регулярное выражение по строке «Используемый метод определения НМЦК»,
  при неудаче — точечный вопрос модели по найденным окнам текста (значение принимается только с дословной цитатой).
* ``field2_2_1_6`` — единый стиль документации и отсутствие логических ошибок: модель просматривает документы по частям
  и называет конкретные дефекты с дословными цитатами; ответ «0» ставится только при подтверждённой цитате,
  иначе — «1» как предложение модели (``proposed``).

* ``field2_3_2``, ``_3``, ``_4``, ``_6``, ``_8`` — соответствие отдельным отраслям законодательства (образование, гражданское,
  бюджетное, персональные данные, антимонопольное). Прямых формулировок «соответствует закону» в документах нет, поэтому
  применяется презумпция соответствия с проверкой на типичные нарушения: по ключевым словам берутся фрагменты документов,
  модель ищет конкретные нарушения из чек-листа; «0» — только при нарушении с подтверждённой цитатой, иначе «1» как
  предложение модели (``proposed``). Заполняются только если поиск фактов не дал значения.

Модуль не зависит от ``asyncpg`` и сети; модель вызывается через переданную функцию ``llm_call``.
"""
import asyncio
import json
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from configs.logger import get_logger
from knowledge_store import facts as facts_mod

logger = get_logger(__name__)

LlmCall = Callable[[List[dict], dict], Awaitable[str]]

NMCK_CHOICE_KEY = "field2_2_2_0"
STYLE_KEY = "field2_2_1_6"
# Код выбора метода НМЦК в форме конкурса: основа слова → (код, название)
NMCK_METHOD_CODES = {"рыночн": (1, "рыночный"), "нормативн": (2, "нормативный"), "затратн": (3, "затратный"),
                     "тарифн": (4, "тарифный"), "проектно-сметн": (5, "проектно-сметный")}

# Презумпция соответствия: ключ → (чек-лист нарушений, шаблон ключевых слов для отбора фрагментов, формулировка для «1»)
PRESUMPTION = {
    "field2_2_3": ("требования к содержанию и составу заявки (ст. 42, 43, 44, 49 44-ФЗ): требуются документы или сведения, "
                   "не предусмотренные законом для заявки (например, копии учредительных документов, выписки из ЕГРЮЛ, "
                   "бухгалтерская отчётность, отзывы и рекомендации, документы о деловой репутации сверх закона); "
                   "требования к составу заявки противоречат закону или друг другу; инструкция по заполнению заявки "
                   "противоречива либо несовместима с требованиями извещения",
                   r"состав\w* заявк|содержани\w+ заявк|инструкци\w+ по заполнени|в составе заявк|"
                   r"первая часть заявк|вторая часть заявк|документы.{0,60}заявк",
                   "требованиям к содержанию и составу заявки"),
    "field2_3_3": ("гражданское законодательство РФ (ГК РФ): размер неустойки (пени) и штрафов, не соответствующий "
                   "ст. 34 44-ФЗ и постановлению Правительства № 1042; ответственность только одной стороны; одностороннее "
                   "расторжение контракта только заказчиком вне оснований закона; условия, противоречащие ГК РФ",
                   r"неустойк|пени|штраф|расторжен|односторонн\w+ отказ|ответственност|гражданск\w+ кодекс",
                   "требованиям гражданского законодательства Российской Федерации"),
    "field2_3_4": ("бюджетное законодательство РФ (БК РФ): размер аванса и порядок его выплаты сверх допустимого, "
                   "несоответствие вида расходов (КОСГУ, КБК) виду контракта, оплата за счёт не предусмотренных источников, "
                   "условия расчётов, противоречащие БК РФ",
                   r"аванс|казначейск|бюджетн|финансировани|вид\w* расход|лимит|порядок оплат|расчет\w* по контракт",
                   "требованиям бюджетного законодательства Российской Федерации"),
    "field2_3_6": ("персональные данные (152-ФЗ): требование предоставить персональные данные без согласия субъекта или "
                   "без указания цели обработки; обязанность обрабатывать данные без правового основания; передача данных "
                   "третьим лицам без оснований",
                   r"персональн\w+ данн|152-ФЗ|согласие на обработк",
                   "требованиям законодательства Российской Федерации о персональных данных"),
    "field2_3_8": ("антимонопольное законодательство (135-ФЗ, ст. 17 и 25 44-ФЗ): требования, ограничивающие конкуренцию — "
                   "завышенные или нерелевантные требования к опыту, лицензиям, членству, наличию собственных мощностей; "
                   "указание товарного знака без «или эквивалент»; требования, под которые подходит один участник; "
                   "необоснованно короткие сроки",
                   r"аналогичн\w+ (?:опыт|работ|услуг)|опыт\w* (?:выполнени|оказани|поставк)|лицензи|членств|"
                   r"товарн\w+ знак|торгов\w+ марк|эквивалент|ограничени\w+ конкуренц",
                   "антимонопольному законодательству Российской Федерации"),
}
EDUCATION_KEY = "field2_3_2"
EDUCATION_RE = r"образовательн|обучени|учебн|школ|вуз\b|университет|дошкольн|повышени\w+ квалификации|курсов\w+ подготовк|студент"
EXPENSE_KEY = "field2_3_5"
# Критерии соответствия, где эксперты применяют «2» (не применимо). Для остальных «2» от модели — не ответ,
# а признак «не нашла сведений»: значение остаётся эксперту (по эталону «2» здесь не встречается).
NOT_APPLICABLE_ALLOWED = frozenset({"field2_2_1_5", "field2_2_2_5_2", "field2_2_2_5_3", "field2_4_3", "field2_4_4", "field2_4_5"})
FILL_IF_EMPTY = frozenset(PRESUMPTION) | {EDUCATION_KEY, EXPENSE_KEY}   # ставятся, только если поиск фактов значения не дал
VIOLATION_SCHEMA = {"type": "object", "properties": {"violations": {"type": "array", "maxItems": 2, "items": {
    "type": "object", "properties": {"quote": {"type": "string", "maxLength": 300}, "issue": {"type": "string", "maxLength": 300}},
    "required": ["quote", "issue"]}}}, "required": ["violations"], "additionalProperties": False}
VIOLATION_PROMPT = ("Ты — эксперт по закупкам (44-ФЗ). Проверь фрагменты документации на НАРУШЕНИЯ из чек-листа: {checklist}. "
                    "Нарушение — только явное и подтверждаемое текстом (конкретное условие, число или формулировка). "
                    "Отсутствие информации нарушением не является. Для каждого нарушения приведи ДОСЛОВНУЮ цитату "
                    "(до 300 символов) и одно предложение: в чём оно состоит и какому требованию противоречит. "
                    "Нет нарушений — верни пустой список. Только JSON.")

CHUNK = 6000          # размер части документа для одного запроса, символов
MAX_DOCS = 6          # сколько документов просматривать
MAX_CHUNKS = 4        # сколько частей брать из документа (равномерно)

NMCK_SCHEMA = {"type": "object", "properties": {
    "method": {"type": ["string", "null"], "enum": ["рыночный", "нормативный", "затратный", "тарифный", "проектно-сметный", None]},
    "quote": {"type": ["string", "null"], "maxLength": 300}}, "required": ["method", "quote"], "additionalProperties": False}
NMCK_PROMPT = ("Ты — эксперт по закупкам (44-ФЗ). По фрагментам определи, КАКОЙ ОДИН метод определения начальной "
               "(максимальной) цены контракта применён заказчиком: рыночный (метод сопоставимых рыночных цен, анализ рынка), "
               "нормативный, затратный, тарифный или проектно-сметный. Если применено несколько методов, выбери основной. "
               "Используй только текст. Приведи ДОСЛОВНУЮ цитату (до 300 символов). Нет данных — method=null. Только JSON.")
STYLE_SCHEMA = {"type": "object", "properties": {"defects": {"type": "array", "maxItems": 3, "items": {
    "type": "object", "properties": {"quote": {"type": "string", "maxLength": 300}, "issue": {"type": "string", "maxLength": 300}},
    "required": ["quote", "issue"]}}}, "required": ["defects"], "additionalProperties": False}
STYLE_PROMPT = ("Ты — эксперт по закупкам (44-ФЗ). Проверь фрагмент документации на единый стиль и отсутствие логических "
                "ошибок. Дефект — ТОЛЬКО явный и подтверждаемый текстом: внутреннее противоречие (разные сроки, суммы, "
                "наименования одного и того же), ссылка на несуществующий пункт или приложение, упоминание другого "
                "заказчика, другой закупки или другого объекта (остатки чужого шаблона), незаполненные поля и "
                "плейсхолдеры («___», «[указать]»), несогласованные термины. Не считай дефектом особенности вёрстки, "
                "стиль изложения и отсутствие сведений. Для каждого дефекта приведи ДОСЛОВНУЮ цитату (до 300 символов) и "
                "коротко (одно предложение) опиши, в чём он состоит. Если дефектов нет, верни пустой список. Только JSON.")


_ISSUE_KEYS = ("issue", "нарушение", "violation", "описание", "проблема", "defect", "дефект", "comment")
_QUOTE_KEYS = ("quote", "цитата", "fragment", "фрагмент")


def _parse(raw: str) -> Optional[dict]:
    """Ответ модели как объект (``None``, если разобрать нельзя).

    Модель на практике отдаёт то объект ``{"violations": [...]}``, то голый список, то список в обёртке
    ```json; список приводится к объекту ``{"items": [...]}``.
    """
    data = facts_mod.parse_json_any(raw)
    if isinstance(data, list):
        return {"items": data}
    return data


def _items(parsed: dict, *names: str) -> List[dict]:
    """Элементы списка нарушений/дефектов из ответа: по ключам ``names``, затем ``items``; только словари."""
    for name in (*names, "items"):
        value = parsed.get(name)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


def _pick(item: dict, keys: Sequence[str]) -> str:
    """Первое непустое строковое значение из ``item`` по списку допустимых имён полей (регистр не важен)."""
    lowered = {str(k).casefold(): v for k, v in item.items()}
    for key in keys:
        value = lowered.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return ""


def keyword_windows(documents: Dict[int, dict], pattern: str, radius: int = 350, limit: int = 4) -> List[str]:
    """Окна текста вокруг ключевых слов (сначала документы извещения)."""
    out: List[str] = []
    for doc in sorted(documents.values(), key=lambda d: d.get("doc_code") != "docIzvejenieFiles"):
        text = doc.get("text") or ""
        for m in re.finditer(pattern, text, flags=re.I):
            window = text[max(0, m.start() - 150):min(len(text), m.end() + radius)]
            if all(window[:60] not in w for w in out):
                out.append(window)
            if len(out) >= limit:
                return out
    return out


def _quote_source(documents: Dict[int, dict], quote: Any) -> Tuple[Optional[str], Optional[str]]:
    """Находит цитату в документах: ``(подтверждённый текст, имя файла)``."""
    if not quote:
        return None, None
    for doc in documents.values():
        found = facts_mod.find_quote(str(quote), doc.get("text") or "")
        if found:
            return found[:300], doc.get("filename")
    return None, None


async def detect_nmck_choice(documents: Dict[int, dict], llm_call: Optional[LlmCall]) -> Optional[dict]:
    """Определяет метод обоснования НМЦК для ``field2_2_2_0``.

    Args:
        documents: Документы экспертизы с текстами.
        llm_call: Функция вызова LLM или ``None``.

    Returns:
        Optional[dict]: ``{"value", "method", "quote", "document", "source"}`` или ``None``, если метод не найден.
    """
    from knowledge_store import summary          # отложенный импорт: summary импортирует этот модуль
    stem, snippet = summary.used_nmck_method(documents)
    if stem in NMCK_METHOD_CODES:
        code, name = NMCK_METHOD_CODES[stem]
        return {"value": code, "method": name, "quote": (snippet or "")[:300], "source": "text_rule"}
    if llm_call is None:
        return None
    windows = keyword_windows(documents, r"(?:метод\w*|методик\w*)[^\n]{0,80}Н\(?М?\)?ЦК|Н\(?М?\)?ЦК[^\n]{0,80}метод\w*")
    if not windows:
        return None
    try:
        parsed = _parse(await llm_call([{"role": "system", "content": NMCK_PROMPT},
                                        {"role": "user", "content": "\n\n".join(f"[{i}] {w}" for i, w in enumerate(windows, 1))}],
                                       NMCK_SCHEMA)) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"knowledge_store: метод НМЦК не определён моделью: {type(e).__name__}: {e}")
        return None
    name = parsed.get("method")
    quote, filename = _quote_source(documents, parsed.get("quote"))
    for code, label in NMCK_METHOD_CODES.values():
        if label == name and quote:
            return {"value": code, "method": label, "quote": quote, "document": filename, "source": "llm_extraction"}
    return None


def style_chunks(text: str, chunk: int = CHUNK, limit: int = MAX_CHUNKS) -> List[str]:
    """Части документа для просмотра: до ``limit`` кусков по ``chunk`` символов, равномерно по тексту."""
    text = text or ""
    if len(text) <= chunk:
        return [text] if text.strip() else []
    count = min(limit, -(-len(text) // chunk))
    step = (len(text) - chunk) / max(count - 1, 1)
    return [text[int(i * step):int(i * step) + chunk] for i in range(count)]


async def assess_style(documents: Dict[int, dict], llm_call: LlmCall, concurrency: int = 4) -> Dict[str, Any]:
    """Оценивает единый стиль и отсутствие логических ошибок по документам.

    Args:
        documents: Документы экспертизы с текстами (``filename``, ``text``).
        llm_call: Функция вызова LLM.
        concurrency: Сколько частей проверять одновременно.

    Returns:
        dict: ``{"defects": [{"document", "quote", "issue"}], "checked_docs", "checked_parts", "errors"}``.
        Дефект входит в список только если его цитата подтверждена текстом части документа.
    """
    docs = sorted((d for d in documents.values() if (d.get("text") or "").strip()),
                  key=lambda d: (d.get("doc_code") != "docIzvejenieFiles", -len(d.get("text") or "")))[:MAX_DOCS]
    semaphore, errors = asyncio.Semaphore(concurrency), []

    async def check(doc: dict, part: str) -> List[dict]:
        """Один запрос по одной части документа."""
        try:
            async with semaphore:
                raw = await llm_call([{"role": "system", "content": STYLE_PROMPT},
                                      {"role": "user", "content": f"Документ «{doc.get('filename')}»:\n{part}"}], STYLE_SCHEMA)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{type(e).__name__}: {e}"[:200])
            return []
        out = []
        for item in _items(_parse(raw) or {}, "defects"):
            issue = _pick(item, _ISSUE_KEYS)
            found = facts_mod.find_quote(_pick(item, _QUOTE_KEYS), part)
            if found and issue:
                out.append({"document": doc.get("filename"), "quote": found[:300], "issue": issue})
        return out

    jobs = [check(d, part) for d in docs for part in style_chunks(d["text"])]
    results = await asyncio.gather(*jobs)
    defects = [x for r in results for x in r]
    seen, unique = set(), []
    for d in defects:
        key = re.sub(r"\W+", " ", d["issue"]).casefold()[:60]
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return {"defects": unique, "checked_docs": len(docs), "checked_parts": len(jobs), "errors": errors}


async def assess_presumption(key: str, documents: Dict[int, dict], llm_call: LlmCall) -> Tuple[Optional[int], dict]:
    """Презумпция соответствия для ``key`` (см. :data:`PRESUMPTION`): ищет нарушения из чек-листа в релевантных фрагментах.

    Args:
        key: Ключ критерия.
        documents: Документы экспертизы с текстами.
        llm_call: Функция вызова LLM.

    Returns:
        Tuple[Optional[int], dict]: ``(0 или 1, запись trace)``; ``(None, {"error"})``, если модель не ответила.
    """
    checklist, pattern, phrase = PRESUMPTION[key]
    windows = keyword_windows(documents, pattern, radius=500, limit=6)
    if not windows:
        return 1, {"status": "proposed", "source": "llm_presumption", "quote": None,
                   "comment": f"Положений, затрагивающих соответствие {phrase}, в документах не найдено; нарушений не выявлено.",
                   "note": "презумпция соответствия: фрагментов по теме нет, требует проверки экспертом"}
    try:
        raw = await llm_call([{"role": "system", "content": VIOLATION_PROMPT.format(checklist=checklist)},
                              {"role": "user", "content": "\n\n".join(f"[{i}] {w}" for i, w in enumerate(windows, 1))}],
                             VIOLATION_SCHEMA)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"knowledge_store: проверка {key} не выполнена: {type(e).__name__}: {e}")
        return None, {"error": f"{type(e).__name__}: {e}"[:300]}
    parsed = _parse(raw)
    if parsed is None:
        return None, {"error": f"ответ модели не разобран: {str(raw)[:200]}"}
    found = []
    for item in _items(parsed, "violations"):
        issue = _pick(item, _ISSUE_KEYS)
        if issue:
            quote, filename = _quote_source(documents, _pick(item, _QUOTE_KEYS))
            if quote:
                found.append({"document": filename, "quote": quote, "issue": issue})
    if found:
        return 0, {"status": "proposed", "source": "llm_presumption", "document": found[0]["document"],
                   "quote": found[0]["quote"], "defects": found,
                   "comment": " ".join(f"В документе «{d['document']}»: {d['issue'].rstrip('.')}." for d in found)}
    return 1, {"status": "proposed", "source": "llm_presumption", "quote": None,
               "comment": f"Нарушений требований, относящихся к {phrase}, в проверенных фрагментах не выявлено.",
               "note": "презумпция соответствия: нарушений с цитатой не найдено, требует проверки экспертом"}


# КВР (последние 3 знака ИКЗ) → (описание, префиксы ОКПД2, при которых вид работ соответствует; ``None`` — любые)
KVR_RULES = {
    "244": ("прочая закупка товаров, работ и услуг для обеспечения государственных (муниципальных) нужд", None),
    "241": ("закупка научно-исследовательских, опытно-конструкторских и технологических работ", ("72",)),
    "242": ("закупка товаров, работ, услуг в сфере информационно-коммуникационных технологий",
            ("26", "58.2", "61", "62", "63.1")),
    "243": ("закупка товаров, работ, услуг в целях капитального ремонта государственного (муниципального) имущества",
            ("41", "42", "43", "71", "33")),
    "247": ("закупка энергетических ресурсов", ("35", "36")),
}


def assess_expense_type(documents: Dict[int, dict]) -> Optional[Tuple[int, dict]]:
    """Критерий 3.5: соответствие видов работ (услуг) виду расходов бюджета — по КВР в ИКЗ.

    ИКЗ — 36 цифр: последние 3 — код вида расходов (КВР), перед ними 4 — группа ОКПД2. КВР 244 («прочая закупка»)
    подходит к любым услугам; для 241/242/243/247 вид работ должен относиться к своей группе ОКПД2. По эталону
    экспертов «0» здесь почти не встречается, поэтому несоответствие кодом не объявляется: если КВР и ОКПД2
    не сходятся или КВР неизвестен, значение остаётся эксперту.

    Args:
        documents: Документы экспертизы с текстами (печатная форма извещения и др.).

    Returns:
        Optional[Tuple[int, dict]]: ``(1, запись trace)`` или ``None``, если ИКЗ не найден или КВР не сходится с ОКПД2.
    """
    for doc in sorted(documents.values(), key=lambda d: d.get("doc_code") != "docIzvejenieFiles"):
        text = doc.get("text") or ""
        for match in re.finditer(r"(?<!\d)(\d{36})(?!\d)", text):
            code = match.group(1)
            kvr, okpd = code[-3:], code[-7:-3]
            rule = KVR_RULES.get(kvr)
            if not rule:
                continue
            title, prefixes = rule
            dotted = f"{okpd[:2]}.{okpd[2:]}"
            if prefixes is not None and not any(dotted.startswith(p) for p in prefixes):
                return None
            return 1, {"status": "proposed", "source": "rule", "document": doc.get("filename"), "quote": code,
                       "comment": f"По ИКЗ закупки код вида расходов (КВР) {kvr} — {title}; вид работ (услуг, ОКПД2 {dotted}) "
                                  "ему соответствует.",
                       "note": "вывод по коду КВР из ИКЗ; требует проверки экспертом"}
    return None


def education_applicable(documents: Dict[int, dict]) -> bool:
    """Относится ли закупка к сфере образования (по объекту закупки в документах извещения)."""
    for doc in sorted(documents.values(), key=lambda d: d.get("doc_code") != "docIzvejenieFiles")[:2]:
        text = (doc.get("text") or "")
        match = re.search(r"наименование объекта закупки[^\n]*\n?[^\n]{0,300}", text, re.I)
        if match and re.search(EDUCATION_RE, match.group(0), re.I):
            return True
    return False


async def compute_assessed(fields: Sequence[dict], documents: Dict[int, dict],
                           llm_call: Optional[LlmCall], skip: Sequence[str] = ()) -> Dict[str, Tuple[Any, dict]]:
    """Значения ``field2_2_2_0`` и ``field2_2_1_6`` (если они есть в форме) для передачи в :func:`summary.assemble`.

    Значения считаются до сборки, чтобы итоги подразделов и пояснения ``*_text`` учитывали их.

    Args:
        fields: Поля формы.
        documents: Документы экспертизы с текстами.
        llm_call: Функция вызова LLM или ``None``.
        skip: Ключи, уже решённые поиском фактов (для :data:`FILL_IF_EMPTY` повторно не оцениваются).

    Returns:
        dict: ``ключ → (значение, запись trace)``; ключ отсутствует, если определить значение не удалось.
    """
    keys = {f["field_key"] for f in fields}
    preset: Dict[str, Tuple[Any, dict]] = {}
    if NMCK_CHOICE_KEY in keys:
        found = await detect_nmck_choice(documents, llm_call)
        if found:
            preset[NMCK_CHOICE_KEY] = (found["value"], {
                "status": "verified" if found["source"] == "text_rule" else "proposed",
                "source": found["source"], "document": found.get("document"), "quote": found["quote"],
                "comment": f"Применён {found['method']} метод обоснования НМЦК"})
    if STYLE_KEY in keys and llm_call is not None:
        result = await assess_style(documents, llm_call)
        if result["checked_parts"] and len(result["errors"]) < result["checked_parts"]:
            defects = result["defects"][:3]
            if defects:
                entry = {"status": "proposed", "source": "llm_assessment", "quote": defects[0]["quote"],
                         "comment": " ".join(f"В документе «{d['document']}»: {d['issue'].rstrip('.')}." for d in defects),
                         "defects": defects}
                preset[STYLE_KEY] = (0, entry)
            else:
                entry = {"status": "proposed", "source": "llm_assessment", "quote": None,
                         "comment": "Единый стиль документации выдержан, логических ошибок и противоречий не выявлено "
                                    f"(просмотрено документов: {result['checked_docs']}, частей: {result['checked_parts']}).",
                         "note": "оценка модели без цитаты: требует проверки экспертом"}
                preset[STYLE_KEY] = (1, entry)
            if result["errors"]:
                entry["errors"] = result["errors"][:3]
    if llm_call is not None:
        todo = [k for k in PRESUMPTION if k in keys and k not in skip]
        for key, res in zip(todo, await asyncio.gather(*(assess_presumption(k, documents, llm_call) for k in todo))):
            if res:
                preset[key] = res
    if EXPENSE_KEY in keys and EXPENSE_KEY not in skip:
        found = assess_expense_type(documents)
        if found:
            preset[EXPENSE_KEY] = found
    if EDUCATION_KEY in keys and EDUCATION_KEY not in skip and not education_applicable(documents) and documents:
        preset[EDUCATION_KEY] = (1, {
            "status": "proposed", "source": "rule", "quote": None,
            "comment": "Объект закупки не относится к сфере образования, требования Федерального закона от 29.12.2012 № 273-ФЗ "
                       "к документации не предъявляются; нарушений не выявлено.",
            "note": "по практике экспертов для неприменимого закона ставится «1»; требует проверки экспертом"})
    return preset
