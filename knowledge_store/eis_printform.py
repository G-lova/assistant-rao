"""Раздел 1 заключения («Наличие информации…», критерии 1.1–1.46) по печатной форме извещения ЕИС.

Если XML извещения (:mod:`knowledge_store.eis_notice`) в экспертизе нет, извещение всё равно присутствует как
«Печатная форма» (HTML из ЕИС). После ``FileReader.read_html`` она превращается в построчный текст
(«метка» — строка, «значение» — следующие строки). Модуль разбирает этот текст по известным меткам и разделам
печатной формы и применяет правила, аналогичные XML-правилам: значение ``1`` — сведения есть (доказательство
``метка = значение``), ``0`` — метка есть, но значение «Информация отсутствует»/пусто, ``2`` — явно «не установлено»
(этапов нет, специализированной организации нет и т. д.), ``None`` — печатная форма не позволяет решить.

Работает по сохранённому тексту (``pe_documents.text_full``), поэтому повторная загрузка документов и миграции не
нужны. Только стандартная библиотека; сети нет. Результат — те же :class:`knowledge_store.eis_notice.Finding`,
привязанные к номеру критерия, поэтому они сопоставляются с полями формы через
:func:`knowledge_store.eis_notice.map_to_fields`.
"""
import re
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from knowledge_store.eis_notice import ABSENT, NOT_PROVIDED, PRESENT, Finding

NO_INFO = re.compile(r"^(?:информация\s+отсутствует|не\s+установлен\w*|не\s+предусмотрен\w*|нет|-|—)\.?$", re.I)

# Метки «параметр — значение»: ключ → основа метки (сравнивается по началу нормализованной строки).
LABELS: Dict[str, str] = {
    "number": "Номер извещения",
    "object_name": "Наименование объекта закупки",
    "method": "Способ определения поставщика",
    "etp_name": "Наименование электронной площадки",
    "etp_url": "Адрес электронной площадки",
    "placing": "Размещение осуществляет",
    "org": "Организация, осуществляющая размещение",
    "post_address": "Почтовый адрес",
    "fact_address": "Место нахождения",
    "responsible": "Ответственное должностное лицо",
    "email": "Адрес электронной почты",
    "phone": "Номер контактного телефона",
    "fax": "Факс",
    "extra": "Дополнительная информация",
    "end_dt": "Дата и время окончания срока подачи заявок",
    "first_parts": "Дата рассмотрения и оценки первых частей заявок",
    "bidding": "Дата проведения процедуры подачи предложений",
    "second_parts": "Дата рассмотрения и оценки вторых частей заявок",
    "summarizing": "Дата подведения итогов определения поставщика",
    "price": "Начальная (максимальная) цена контракта",
    "price_max": "Максимальное значение цены контракта",
    "start_date": "Дата начала исполнения контракта",
    "term": "Срок исполнения контракта",
    "budget_funds": "Закупка за счет бюджетных средств",
    "own_funds": "Закупка за счет собственных средств организации",
    "ikz": "Идентификационный код закупки",
    "place": "Место поставки товара, выполнения работы или оказания услуги",
    "one_side": "Предусмотрена возможность одностороннего отказа",
    "advance": "Аванс",
    "app_required": "Требуется обеспечение заявки",
    "app_size": "Размер обеспечения заявки",
    "app_order": "Порядок внесения денежных средств в качестве обеспечения заявки",
    "app_not_required": "Обеспечение заявки не требуется",
    "contract_required": "Требуется обеспечение исполнения контракта",
    "contract_size": "Размер обеспечения исполнения контракта",
    "contract_order": "Порядок обеспечения исполнения контракта",
    "contract_not_required": "Обеспечение исполнения контракта не требуется",
    "warranty_required": "Требуется обеспечение гарантийных обязательств",
    "warranty_size": "Размер обеспечения гарантийных обязательств",
    "warranty_order": "Порядок предоставления обеспечения гарантийных обязательств",
    "warranty_not_required": "Обеспечение гарантийных обязательств не требуется",
    "multi": "Право заключения контрактов с несколькими участниками",
    "advantages": "Преимущества",
    "requirements": "Требования к участникам",
    "account_no": "\"Номер расчётного счёта\"",
    "treasury_account": "Номер единого казначейского счета",
}
# Продолжение метки на следующей строке (длинные подписи переносятся)
LABEL_CONTINUATION = re.compile(r"^(?:а также условия гарантии|в ч\. 10 ст\. 34|Закона № 44-ФЗ)$", re.I)

# Заголовки разделов печатной формы
SECTIONS = ("Общая информация", "Контактная информация", "Информация о процедуре закупки", "Условия контракта",
            "Информация о сроках исполнения контракта", "Финансовое обеспечение закупки", "Этапы исполнения контракта",
            "Финансирование за счет", "Объект закупки", "Преимущества и требования к участникам", "Обеспечение заявки",
            "Реквизиты счета для учета операций", "Реквизиты счета для перечисления", "Обеспечение исполнения контракта",
            "Платежные реквизиты", "Обеспечение гарантийных обязательств", "Требования к гарантии качества",
            "Информация о банковском и (или) казначейском сопровождении", "Применение национального режима",
            "Критерии оценки заявок", "Перечень прикрепленных документов", "Дополнительная информация и документы")

_KTRU = re.compile(r"\b\d{2}\.\d{2}\.\d{2}\.\d{3}-\d{8}\b")
_ZEROS = re.compile(r"^[0\s.\-]+$")


def _norm(text: str) -> str:
    """Строка без лишних пробелов и «ё»."""
    return re.sub(r"\s+", " ", (text or "").replace("ё", "е").replace("Ё", "Е")).strip()


def is_print_form(text: str) -> bool:
    """Это печатная форма **извещения** (а не разъяснения/изменения): есть заголовок и «Номер извещения»."""
    head = (text or "")[:600]
    return "Печатная форма" in head and "Извещение о проведении" in head and "Номер извещения" in (text or "")[:3000]


class PrintForm:
    """Разобранная печатная форма: значения по меткам и строки по разделам."""

    def __init__(self, text: str):
        """Разбирает построчный текст печатной формы.

        Args:
            text: Результат ``FileReader.read_html`` для печатной формы извещения.
        """
        self.lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        self.pairs: Dict[str, List[str]] = {}
        self.raw_labels: Dict[str, str] = {}          # ключ → подпись метки, как она напечатана («Аванс, %»)
        self.sections: Dict[str, List[str]] = {}
        self._parse()

    def _label_at(self, i: int) -> Optional[Tuple[str, int]]:
        """Ключ метки в строке ``i`` и число строк метки (с переносом)."""
        line = _norm(self.lines[i])
        for key, stem in LABELS.items():
            if line.lower().startswith(_norm(stem).lower()):
                size = 1
                while i + size < len(self.lines) and LABEL_CONTINUATION.match(_norm(self.lines[i + size])):
                    size += 1
                return key, size
        return None

    def _section_at(self, i: int) -> Optional[str]:
        """Название раздела, если строка ``i`` — заголовок раздела."""
        line = _norm(self.lines[i])
        for name in SECTIONS:
            if line.lower().startswith(name.lower()) and len(line) <= len(name) + 60:
                return name
        return None

    def _parse(self) -> None:
        """Заполняет :attr:`pairs` и :attr:`sections` одним проходом по строкам."""
        current_label, section = None, "Заголовок"
        i = 0
        while i < len(self.lines):
            line = self.lines[i]
            sec = self._section_at(i)
            label = self._label_at(i)
            exact = sec is not None and _norm(line).lower() == sec.lower()
            if sec and (exact or not label):       # «Обеспечение гарантийных обязательств не требуется» — метка, а не раздел
                section, current_label = sec, None
                self.sections.setdefault(section, [])
                i += 1
                continue
            self.sections.setdefault(section, []).append(line)
            if label:
                current_label = label[0]
                self.pairs.setdefault(current_label, [])
                self.raw_labels.setdefault(current_label, _norm(line).split(":")[0] if current_label == "advance" else "")
                self.sections[section].extend(self.lines[i + 1:i + label[1]])
                rest = _norm(line)[len(_norm(LABELS[current_label])):].strip(" :")
                if rest:                                   # значение в той же строке: «"БИК"000000000»
                    self.pairs[current_label].append(rest)
                i += label[1]
                continue
            if current_label:
                self.pairs[current_label].append(line)
            i += 1

    def value(self, key: str) -> Optional[str]:
        """Значение метки без заглушек «Информация отсутствует»; ``None``, если метки нет или значение пусто."""
        lines = [ln for ln in self.pairs.get(key, []) if not NO_INFO.match(_norm(ln))]
        return _norm(" ".join(lines)) or None

    def has(self, key: str) -> bool:
        """Есть ли метка в форме (значение может быть пустым)."""
        return key in self.pairs

    def section_text(self, name: str) -> str:
        """Текст раздела (все строки)."""
        return "\n".join(self.sections.get(name, []))


def _present(form: PrintForm, key: str, label: str, absent: Optional[int] = ABSENT) -> Optional[Finding]:
    """``1``, если у метки есть значение; ``absent`` (по умолчанию ``0``), если метка есть, но пуста; иначе ``None``."""
    value = form.value(key)
    if value:
        return Finding(PRESENT, f"{label} = {value}"[:500])
    if form.has(key) and absent is not None:
        return Finding(absent, f"{label}: информация отсутствует")
    return None


def _first(form: PrintForm, keys: Sequence[str], label: str) -> Optional[Finding]:
    """Первое найденное значение среди меток ``keys``."""
    for key in keys:
        value = form.value(key)
        if value:
            return Finding(PRESENT, f"{label} = {value}"[:500])
    return None


def _specialized(form: PrintForm) -> Optional[Finding]:
    """1.7: «Размещение осуществляет: Заказчик» — специализированной организации нет (``2``), иначе ``1``."""
    lines = form.pairs.get("placing") or []
    if not lines:
        return None
    head = _norm(lines[0]).lower()
    if head.startswith("заказчик"):
        return Finding(NOT_PROVIDED, "Размещение осуществляет = Заказчик (специализированная организация не привлечена)")
    return Finding(PRESENT, f"Размещение осуществляет = {_norm(' '.join(lines))}"[:500])


def _object_columns(form: PrintForm, column: str, label: str) -> Optional[Finding]:
    """Раздел «Объект закупки» содержит колонку ``column`` (печатная форма всегда выводит её с данными)."""
    section = form.sections.get("Объект закупки", [])
    if any(_norm(ln).lower().startswith(column.lower()) for ln in section) and \
            any(re.match(r"^\d[\d\s.,]*$", _norm(ln)) for ln in section):
        return Finding(PRESENT, f"Объект закупки: колонка «{column}» заполнена")
    return None


def _ktru(form: PrintForm) -> Optional[Finding]:
    """1.12: в таблице объекта есть код КТРУ (``NN.NN.NN.NNN-NNNNNNNN``); иначе печатная форма не решает."""
    match = _KTRU.search(form.section_text("Объект закупки"))
    return Finding(PRESENT, f"Объект закупки: код позиции КТРУ = {match.group(0)}") if match else None


def _stages(form: PrintForm) -> Optional[Finding]:
    """1.17 / 1.19: «Контракт не разделен на этапы» → ``2``; есть перечень этапов → ``1``; иначе решение за экспертом."""
    text = form.section_text("Этапы исполнения контракта")
    if re.search(r"не разделен на этапы", text, re.I):
        return Finding(NOT_PROVIDED, "Этапы исполнения контракта: контракт не разделен на этапы")
    if re.search(r"\bэтап", text, re.I):
        return Finding(PRESENT, "Этапы исполнения контракта: " + _norm(text)[:300])
    return None


def _funding(form: PrintForm) -> Optional[Finding]:
    """1.20: указан источник финансирования («Закупка за счет…»: Да или раздел «Финансирование за счет…»)."""
    for key, label in (("budget_funds", "Закупка за счет бюджетных средств"),
                       ("own_funds", "Закупка за счет собственных средств организации")):
        if (form.value(key) or "").lower() == "да":
            return Finding(PRESENT, f"{label} = Да")
    if "Финансирование за счет" in form.sections:
        return Finding(PRESENT, "Финансирование за счет: " + _norm(form.section_text("Финансирование за счет"))[:200])
    return None


def _currency(form: PrintForm) -> Optional[Finding]:
    """1.21: в цене указана валюта (``РОССИЙСКИЙ РУБЛЬ``, ``RUB`` и др.)."""
    value = form.value("price") or form.value("price_max") or ""
    match = re.search(r"[A-Za-zА-ЯЁа-яё]{3,}", value)
    return Finding(PRESENT, f"Начальная (максимальная) цена контракта = {value}") if match else None


def _advance(form: PrintForm) -> Optional[Finding]:
    """1.22: метка «Аванс…» со значением — ``1``; в печатной форме без аванса метки нет — «не предусмотрено» (``2``)."""
    if form.value("advance"):
        return Finding(PRESENT, f"{form.raw_labels.get('advance') or 'Аванс'} = {form.value('advance')}")
    if form.sections.get("Условия контракта") is not None or form.has("price"):
        return Finding(NOT_PROVIDED, "в печатной форме нет сведений об авансе (аванс не предусмотрен)")
    return None


def _criteria(form: PrintForm, need_percent: bool) -> Optional[Finding]:
    """1.23 / 1.24: раздел «Критерии оценки заявок» со значимостью критериев."""
    text = form.section_text("Критерии оценки заявок")
    if re.search(r"Значимость критерия оценки:\s*\d", text):
        return Finding(PRESENT, "Критерии оценки заявок: " + _norm(text)[:200])
    return None


def _requirement(pattern: str, label: str) -> Callable[[PrintForm], Optional[Finding]]:
    """Правило «в требованиях к участникам упомянута норма ``pattern``» → ``1``, иначе ``None``."""
    regex = re.compile(pattern, re.I)

    def rule(form: PrintForm) -> Optional[Finding]:
        text = " ".join(form.pairs.get("requirements", []))
        match = regex.search(text)
        return Finding(PRESENT, f"Требования к участникам: …{_norm(text[max(0, match.start() - 20):match.end() + 60])}") if match else None
    rule.__doc__ = f"Правило для {label}."
    return rule


def _advantages(pattern: str, label: str) -> Callable[[PrintForm], Optional[Finding]]:
    """Правило преимуществ: упомянута норма ``pattern`` → ``1``; «Не установлены» → ``2``; иначе решает эксперт."""
    regex = re.compile(pattern, re.I)

    def rule(form: PrintForm) -> Optional[Finding]:
        text = " ".join(form.pairs.get("advantages", []))
        if regex.search(text):
            return Finding(PRESENT, f"Преимущества = {_norm(text)[:300]}")
        if re.fullmatch(r"не\s+установлен\w*\.?", _norm(text), re.I):
            return Finding(NOT_PROVIDED, "Преимущества = Не установлены")
        return None
    rule.__doc__ = f"Правило для {label}."
    return rule


def _order(key: str, label: str) -> Callable[[PrintForm], Optional[Finding]]:
    """Порядок обеспечения: ссылка на регламент площадки — не порядок (решает эксперт), как и в XML-правилах."""
    def rule(form: PrintForm) -> Optional[Finding]:
        value = form.value(key)
        if value and re.search(r"регламент", value, re.I):
            return Finding(None, f"{label} = {value}"[:480] + " (ссылка на регламент — решает эксперт)")
        return _present(form, key, label, absent=None)
    rule.__doc__ = f"Правило «{label}»."
    return rule


def _account(form: PrintForm) -> Optional[Finding]:
    """1.33: указан реальный номер счёта (не нули) или единый казначейский счёт."""
    for key in ("treasury_account",):
        if form.value(key):
            return Finding(PRESENT, f"Номер единого казначейского счета = {form.value(key)}")
    for value in form.pairs.get("account_no", []):
        digits = re.sub(r"\D", "", value)
        if len(digits) >= 20 and not _ZEROS.match(digits):
            return Finding(PRESENT, f"Номер расчётного счёта = {digits}")
    return None


def _national_regime(form: PrintForm) -> Optional[Finding]:
    """1.30: раздела «Применение национального режима по ст. 14» нет — запреты/ограничения не установлены (``2``)."""
    if "Применение национального режима" in form.sections:
        return None                 # по эталону экспертов решения неоднозначны — оставляем эксперту/LLM
    return Finding(NOT_PROVIDED, "в печатной форме нет раздела «Применение национального режима по ст. 14 Закона № 44-ФЗ»") \
        if form.sections.get("Объект закупки") else None


def _bank_support(form: PrintForm) -> Optional[Finding]:
    """1.38: если сопровождение требуется — ``1``; «не требуется» эксперты оценивают по-разному, решает эксперт."""
    text = form.section_text("Информация о банковском и (или) казначейском сопровождении")
    if not text.strip() or re.search(r"не требуется", text, re.I):
        return None
    return Finding(PRESENT, "Информация о банковском и (или) казначейском сопровождении: " + _norm(text)[:200])


def _multi(form: PrintForm) -> Optional[Finding]:
    """1.39: «Право заключения контрактов с несколькими участниками…: Не установлено» → ``2``."""
    lines = form.pairs.get("multi") or []
    text = _norm(" ".join(lines))
    if not text:
        return None
    if NO_INFO.match(_norm(lines[-1])):
        return Finding(NOT_PROVIDED, f"Право заключения контрактов с несколькими участниками = {text}"[:500])
    return None                     # «установлено» — эксперты оценивают по-разному


def _one_side(form: PrintForm) -> Optional[Finding]:
    """1.40: «Предусмотрена возможность одностороннего отказа…» со значением «Да»."""
    value = (form.value("one_side") or "").lower()
    return Finding(PRESENT, "Предусмотрена возможность одностороннего отказа от исполнения контракта = Да") if value.endswith("да") else None


def _antimonopoly(form: PrintForm) -> Optional[Finding]:
    """1.46: в начале печатной формы есть предупреждение об ответственности за нарушение антимонопольных требований."""
    head = " ".join(form.lines[:12])
    return Finding(PRESENT, "Внимание! За нарушение требований антимонопольного законодательства… предусмотрена ответственность") \
        if re.search(r"антимонопольн\w+ законодательств\w+.*ответственност", head, re.I) else None


# критерий → правило печатной формы
RULES: Dict[str, Callable[[PrintForm], Optional[Finding]]] = {
    "1.1": lambda f: _first(f, ["placing", "org"], "Наименование заказчика"),
    "1.2": lambda f: _present(f, "fact_address", "Место нахождения"),
    "1.3": lambda f: _present(f, "post_address", "Почтовый адрес"),
    "1.4": lambda f: _present(f, "email", "Адрес электронной почты"),
    "1.5": lambda f: _present(f, "phone", "Номер контактного телефона"),
    "1.6": lambda f: _present(f, "responsible", "Ответственное должностное лицо"),
    "1.7": _specialized,
    "1.8": lambda f: _present(f, "ikz", "Идентификационный код закупки"),
    "1.9": lambda f: _present(f, "method", "Способ определения поставщика"),
    "1.10": lambda f: _present(f, "etp_url", "Адрес электронной площадки"),
    "1.11": lambda f: _present(f, "object_name", "Наименование объекта закупки"),
    "1.12": _ktru,
    "1.13": lambda f: _object_columns(f, "Количество", "количество"),
    "1.14": lambda f: _object_columns(f, "Единица измерения", "единица измерения"),
    "1.15": lambda f: _present(f, "place", "Место поставки товара, выполнения работы или оказания услуги"),
    "1.16": lambda f: _first(f, ["term", "start_date"], "Срок исполнения контракта"),
    "1.17": _stages,
    "1.18": lambda f: _first(f, ["price", "price_max"], "Начальная (максимальная) цена контракта"),
    "1.19": _stages,
    "1.20": _funding,
    "1.21": _currency,
    "1.22": _advance,
    "1.23": lambda f: _criteria(f, False),
    "1.24": lambda f: _criteria(f, True),
    "1.25": _requirement(r"ч(?:\.|асти)\s*1\s+ст(?:\.|атьи)\s*31", "ч. 1 ст. 31"),
    "1.26": _requirement(r"ч(?:\.|асти)\s*1\.1\s+ст(?:\.|атьи)\s*31", "ч. 1.1 ст. 31"),
    "1.27": _requirement(r"ч(?:\.|асти)\s*2(?:\.1)?\s+ст(?:\.|атьи)\s*31|ч(?:\.|астями)\s*2\s+и\s*2\.1", "ч. 2 и 2.1 ст. 31"),
    "1.28": _advantages(r"ст(?:\.|атьи|атьями)\s*(?:28|29)\b", "ст. 28, 29"),
    "1.29": _advantages(r"ч(?:\.|асти)\s*3\s+ст(?:\.|атьи)\s*30|ст(?:\.|атьи)\s*30\b", "ч. 3 ст. 30"),
    "1.30": _national_regime,
    "1.31": lambda f: _present(f, "app_size", "Размер обеспечения заявки", absent=None),
    "1.32": _order("app_order", "Порядок внесения денежных средств в качестве обеспечения заявки"),
    "1.33": _account,
    "1.34": lambda f: _present(f, "contract_size", "Размер обеспечения исполнения контракта", absent=None),
    "1.35": _order("contract_order", "Порядок обеспечения исполнения контракта"),
    "1.36": lambda f: _present(f, "warranty_size", "Размер обеспечения гарантийных обязательств", absent=None),
    "1.37": _order("warranty_order", "Порядок предоставления обеспечения гарантийных обязательств"),
    "1.38": _bank_support,
    "1.39": _multi,
    "1.40": _one_side,
    "1.41": lambda f: _present(f, "end_dt", "Дата и время окончания срока подачи заявок", absent=None),
    "1.42": lambda f: _present(f, "first_parts", "Дата рассмотрения и оценки первых частей заявок", absent=None),
    "1.43": lambda f: _present(f, "bidding", "Дата проведения процедуры подачи предложений", absent=None),
    "1.44": lambda f: _present(f, "second_parts", "Дата рассмотрения и оценки вторых частей заявок", absent=None),
    "1.45": lambda f: _present(f, "summarizing", "Дата подведения итогов определения поставщика", absent=None),
    "1.46": _antimonopoly,
}

# Критерии, для которых печатная форма не используется (точность по эталону экспертов недостаточна)
DISABLED: set = set()


def evaluate_text(text: str) -> Dict[str, Finding]:
    """Применяет правила раздела 1 к тексту печатной формы извещения.

    Args:
        text: Построчный текст печатной формы (``pe_documents.text_full``).

    Returns:
        dict: ``номер критерия → Finding``; пусто, если текст не является печатной формой извещения.
    """
    if not is_print_form(text):
        return {}
    form = PrintForm(text)
    out: Dict[str, Finding] = {}
    for criterion, rule in RULES.items():
        if criterion in DISABLED:
            continue
        try:
            found = rule(form)
        except Exception:  # noqa: BLE001 — одно правило не должно ронять остальные
            found = None
        if found is not None and found.value is not None:
            out[criterion] = found
    return out


def to_json(findings: Dict[str, Finding]) -> dict:
    """Результат в формате ``extraction['eis_notice']`` (``{"version": 0, "criteria": {...}}``) для :func:`eis_notice.map_to_fields`."""
    return {"version": 0, "source": "print_form",
            "criteria": {c: {"value": f.value, "evidence": f.evidence[:500]} for c, f in findings.items()}}
