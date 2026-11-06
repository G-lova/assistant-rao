"""DOC-001 «нет обязательного раздела» — по оглавлению всего документа, а не по ответу модели.

Модель видит документ частями и считала, что раздела нет во всём документе, если его нет в её части: 222 из 297
DOC-001 выгрузки 06.11.2026 — «отсутствует раздел о сроках / оплате / приёмке». Теперь DOC-001 модель не ищет
(код убран из её списка), а сервис проверяет полный текст:

1. Проверка — только для основного текста контракта или проекта контракта: тип документа — контракт, в тексте
   есть «предмет контракта (договора)», текст не короче :data:`MIN_CHARS`. Доп. соглашения, приложения,
   спецификации и формы не проверяются — разделы контракта в них и не должны быть.
2. Обязательные темы (ст. 34 44-ФЗ): сроки исполнения, порядок оплаты, приёмка, ответственность сторон.
   Гарантийные обязательства обязательны не для всех контрактов — не проверяются.
3. Тема считается найденной, если она есть в заголовке раздела (оглавление — :func:`headings`) или в тексте
   (устойчивые формулировки :data:`TOPICS`). Риск — только если темы нет нигде.

Риск сервиса отличается от прежних рисков модели началом описания (:data:`TOC_PREFIX`): сохранённые DOC-001
модели в балл закупки не идут (``contract_package.file_risks``).
"""
import re
from typing import Any, Dict, List, Optional

CONTRACT_TYPES = {"docProjContractFiles", "docContractPostTovarFiles", "docContractDoWorkFiles", "docContractNIRFiles"}
MIN_CHARS = 5000
TOC_PREFIX = "По оглавлению документа"

BODY_RE = re.compile(r"предмет\w*\s+(государственного\s+|муниципального\s+)?(контракт|договор)", re.IGNORECASE)

# тема → (признак в заголовке, признак в тексте)
TOPICS: Dict[str, Dict[str, "re.Pattern"]] = {
    "сроки исполнения": {
        "head": re.compile(r"срок|порядок\s+(поставк|выполнени|оказани)|услови\w*\s+(поставк|выполнени|оказани)", re.I),
        "text": re.compile(r"срок\w*\s+(исполнени|поставк|выполнени|оказани|действи)|(поставк|выполнени|оказани)\w*\s+"
                           r"[^.]{0,60}(в\s+течение|не\s+позднее|до\s+\d|с\s+\d|по\s+\d)", re.I),
    },
    "порядок оплаты": {
        "head": re.compile(r"оплат|расч[её]т|цена\s+(контракт|договор)", re.I),
        "text": re.compile(r"(порядок|срок\w*|услови\w*)\s+(и\s+\w+\s+)?оплат|оплата\s+(производится|осуществляется)"
                           r"|оплат\w*\s+[^.]{0,80}в\s+течение", re.I),
    },
    "приёмка": {
        "head": re.compile(r"при[её]м", re.I),
        "text": re.compile(r"при[её]мк\w*\s+(товар|работ|услуг|результат|поставлен|выполнен|оказан)"
                           r"|документ\w*\s+о\s+при[её]мке|акт\w*\s+(сдачи|при[её]м)", re.I),
    },
    "ответственность сторон": {
        "head": re.compile(r"ответственност", re.I),
        "text": re.compile(r"ответственност\w*\s+сторон|неустойк|штраф\w*\s+начисля|пен[яи]\s+начисля", re.I),
    },
}

HEADING_RE = re.compile(
    r"^\s*(?:(?:раздел|статья|глава)\s+[\dIVXLC]+\.?|[\dIVXLC]{1,4}\.(?:\d{1,2}\.?)?)\s*(?P<title>[А-ЯЁA-Z][^\n]{2,110})$"
    r"|^\s*(?P<caps>[А-ЯЁ][А-ЯЁ ,\-–«»()]{8,110})$",
    re.MULTILINE,
)


def applies(detected_type: Optional[str], text: Optional[str]) -> bool:
    """Нужна ли проверка разделов: основной текст контракта (проекта контракта) достаточной длины."""
    return bool(text) and detected_type in CONTRACT_TYPES and len(text) >= MIN_CHARS and bool(BODY_RE.search(text))


def headings(text: str) -> List[str]:
    """Заголовки разделов: нумерованные строки («5. ПОРЯДОК ОПЛАТЫ», «Раздел 3 …») и строки прописными."""
    out = []
    for m in HEADING_RE.finditer(text or ""):
        title = (m.group("title") or m.group("caps") or "").strip()
        # заголовок короткий; длинная строка с номером — это пункт текста
        if title and len(title.split()) <= 12:
            out.append(title)
    return out


def missing_sections(text: str) -> Dict[str, Any]:
    """Обязательные темы контракта, которых нет ни в заголовках, ни в тексте.

    Args:
        text: Полный текст документа.

    Returns:
        Dict[str, Any]: ``missing`` — темы без раздела и без устойчивой формулировки в тексте; ``headings`` —
        найденные заголовки (для доказательства).
    """
    heads = headings(text)
    head_text = "\n".join(heads)
    missing = [topic for topic, rx in TOPICS.items()
               if not rx["head"].search(head_text) and not rx["text"].search(text or "")]
    return {"missing": missing, "headings": heads}


def doc001_risk(text: str, detected_type: Optional[str]) -> Optional[Dict[str, Any]]:
    """Риск DOC-001 сервиса в компактном формате ``DocumentRiskAnalyzer.to_compact`` или ``None``.

    Цитата — заголовок раздела о предмете контракта (точная строка текста); перечень найденных разделов — в
    ``section``.
    """
    if not applies(detected_type, text):
        return None
    found = missing_sections(text)
    if not found["missing"]:
        return None
    anchor = BODY_RE.search(text)
    line_start = text.rfind("\n", 0, anchor.start()) + 1
    line_end = text.find("\n", anchor.end())
    fragment = text[line_start: line_end if line_end != -1 else len(text)].strip()[:300]
    toc = "; ".join(found["headings"][:20])
    names = ", ".join(found["missing"])
    return {
        "code": "DOC-001",
        "category": "DOC",
        "severity": "medium",
        "title": "Нет обязательного раздела контракта",
        "explanation": f"{TOC_PREFIX}: не найдено ни раздела, ни условий — {names}.",
        "law": "ст. 34 Федерального закона № 44-ФЗ",
        "verification_needed": [f"Проверить, есть ли в контракте или его приложениях условия: {names}"],
        "fragment": fragment,
        "section": (f"Разделы документа: {toc}" if toc else "Заголовки разделов не распознаны")[:300],
        "page": None,
        "confidence": 0.9,
    }


def is_service_risk(risk: Dict[str, Any]) -> bool:
    """DOC-001 сервиса (по оглавлению), а не модели — по началу описания."""
    text = str(risk.get("description") or risk.get("explanation") or "")
    return text.startswith(TOC_PREFIX)
