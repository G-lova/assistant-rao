"""Оформление заключений ИИ риск-мониторинга для чтения: абзацы, списки, главный вывод первым.

Как интерфейс внешней системы показывает текст, неизвестно (30.10.2026), поэтому по умолчанию — «безопасный»
формат (``AI_TEXT_FORMAT = "plain"``), который читается и как простой текст, и как markdown:

- абзацы по 1–3 предложения, между ними пустая строка;
- перечисления — пункты с новой строки, начинающиеся с «— »;
- без заголовков, жирного шрифта и символов разметки (``*``, ``#``, ``_``, `` ` ``).

``AI_TEXT_FORMAT = "markdown"`` — для интерфейса, который рендерит markdown: пункты «- », жирный шрифт разрешён.

Модель получает требования к оформлению (:func:`instruction`), а её ответ и шаблонные тексты дополнительно
приводятся к формату кодом (:func:`normalize`): модель не всегда следует инструкции.
"""
import re
from typing import Iterable, List, Optional

PLAIN_BULLET = "— "
MD_BULLET = "- "

BULLET_RE = re.compile(r"^\s*(?:[-*•–—]|\d{1,2}[.)])\s+")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*")


def mode(cfg: object = None) -> str:
    """Формат текста из настроек: ``plain`` (по умолчанию) или ``markdown``."""
    if cfg is None:
        from configs import settings as cfg
    m = str(getattr(cfg, "AI_TEXT_FORMAT", "plain") or "plain").lower()
    return m if m in ("plain", "markdown") else "plain"


def instruction(cfg: object = None) -> str:
    """Требования к оформлению для промпта модели (добавляются к системному промпту заключения)."""
    if mode(cfg) == "markdown":
        return ("Оформи текст для лёгкого чтения в markdown: первым абзацем — главный вывод; дальше короткие абзацы "
                "по 1–3 предложения, разделённые пустой строкой; перечисления (признаки риска, что проверить, "
                "изменения) — маркированным списком «- », каждый пункт с новой строки. Можно выделить жирным 1–2 "
                "ключевых слова. Без заголовков и без JSON.")
    return ("Оформи текст для лёгкого чтения: первым абзацем — главный вывод; дальше короткие абзацы по 1–3 "
            "предложения, разделённые пустой строкой; перечисления (признаки риска, что проверить, изменения) — "
            "списком, каждый пункт с новой строки и начинается с «— ». Без заголовков, жирного шрифта, символов "
            "*, # и _, без JSON.")


def _strip_inline(text: str, keep_bold: bool) -> str:
    """Убирает разметку внутри строки: жирный и курсив (кроме жирного в markdown), код."""
    if not keep_bold:
        text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"\1", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    return text


def normalize(text: Optional[str], cfg: object = None) -> str:
    """Приводит заключение к формату: абзацы через пустую строку, пункты списка с маркером, без лишней разметки.

    Заголовок («### Итог») становится отдельной строкой «Итог:»; строка, оканчивающаяся двоеточием перед
    списком, остаётся его вводной строкой (без пустой строки между ними).

    Args:
        text: Текст модели или шаблона.
        cfg: Настройки (``AI_TEXT_FORMAT``).

    Returns:
        str: Оформленный текст (пустая строка — для пустого входа).
    """
    if not text:
        return ""
    md = mode(cfg) == "markdown"
    bullet = MD_BULLET if md else PLAIN_BULLET
    raw = str(text).replace("\r\n", "\n").replace("\r", "\n")
    blocks: List[str] = []
    para: List[str] = []
    items: List[str] = []
    intro: List[str] = []

    def flush() -> None:
        """Закрывает текущий абзац или список (с вводной строкой)."""
        nonlocal para, items, intro
        if items:
            lst = "\n".join(bullet + i for i in items)
            blocks.append(("\n".join(intro) + "\n" + lst) if intro else lst)
        elif intro:
            para = intro + para
        if para:
            blocks.append(" ".join(para))
        para, items, intro = [], [], []

    for line in raw.split("\n"):
        s = line.strip()
        if not s:
            flush()
            continue
        if HEADING_RE.match(s):
            s = HEADING_RE.sub("", _strip_inline(s, keep_bold=False)).rstrip(":").strip()
            flush()
            if s:
                intro = [s + ":"]
            continue
        s = _strip_inline(s, keep_bold=md)
        if BULLET_RE.match(s):
            if para:
                # «Признаки риска:» перед пунктами — вводная строка списка
                if para[-1].endswith(":"):
                    head = para.pop()
                    if para:
                        blocks.append(" ".join(intro + para))
                        intro = []
                    intro = intro + [head]
                else:
                    blocks.append(" ".join(intro + para))
                    intro = []
                para = []
            items.append(BULLET_RE.sub("", s).strip())
        else:
            if items:
                flush()
            if intro and not items and not para and not intro[-1].endswith(":"):
                flush()
            para.append(s)
    flush()
    out = "\n\n".join(b for b in blocks if b)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def compose(lead: str, items: Iterable[str] = (), tail: str = "", intro: str = "", cfg: object = None) -> str:
    """Шаблонное заключение (без модели) в том же формате: вывод, список, завершающий абзац.

    Args:
        lead: Первый абзац — главный вывод.
        items: Пункты списка (пустые пропускаются).
        tail: Последний абзац.
        intro: Строка перед списком (например, «Признаки риска:»).
        cfg: Настройки.

    Returns:
        str: Текст.
    """
    bullet = MD_BULLET if mode(cfg) == "markdown" else PLAIN_BULLET
    parts = [lead.strip()] if lead and lead.strip() else []
    lst = [bullet + str(i).strip().rstrip(";") for i in items if i and str(i).strip()]
    if lst:
        parts.append(((intro.strip() + "\n") if intro else "") + "\n".join(lst))
    if tail and tail.strip():
        parts.append(tail.strip())
    return "\n\n".join(parts)
