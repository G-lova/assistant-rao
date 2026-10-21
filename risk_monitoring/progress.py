"""Журнал хода задач риск-мониторинга: что именно делается сейчас и сколько это заняло.

Каждый объект анализа (файл, XML, закупка, организация, типовое положение) задаёт «субъект»
(:func:`subject`), и этапы пишутся строками вида::

    Файл 2805 «ТЗ.docx» ▶ поиск похожих документов
    Файл 2805 «ТЗ.docx» ✓ поиск похожих документов — 1.2 с (найдено 3)
    Файл 2805 «ТЗ.docx» ▶ анализ рисков: запрос к модели
    Файл 2805 «ТЗ.docx» ✗ анализ рисков: запрос к модели — 420.0 с: TimeoutError

Субъект хранится в ``contextvars``: у каждой корутины ``asyncio.gather`` — свой, поэтому строки параллельных
файлов не путаются. Пульс задачи (``tasks._pulsed``) перечисляет этапы, которые выполняются сейчас, и сколько
они идут (:func:`active`).
"""
import contextlib
import contextvars
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

from configs.logger import get_logger

logger = get_logger("risk_monitoring.progress")

_SUBJECT: contextvars.ContextVar = contextvars.ContextVar("rm_subject", default="")
_ACTIVE: Dict[int, Tuple[str, str, float]] = {}
_seq = 0

# Виды анализа документа (ключи кэша ai_document_analysis) → название этапа
ANALYSIS_TITLES = {
    "data_extraction": "извлечение данных",
    "doc_type": "определение типа документа",
    "document_profile": "паспорт документа",
    "risk_analysis": "анализ рисков",
    "risk_analysis_print_form": "анализ рисков печатной формы",
    "version_diff": "сравнение с прошлой редакцией",
}


def subject(name: str) -> None:
    """Задаёт субъект для текущей корутины (файл, XML, закупка …)."""
    _SUBJECT.set(name)


def current() -> str:
    """Текущий субъект."""
    return _SUBJECT.get() or "Задача"


def note(message: str) -> None:
    """Строка журнала о текущем субъекте без замера времени."""
    logger.info(f"{current()}: {message}")


@contextlib.contextmanager
def _track(title: str) -> Iterator[Dict[str, Any]]:
    """Общая часть :func:`step` и :func:`astep`: начало, итог, ошибка, учёт активных этапов."""
    global _seq
    _seq += 1
    key, who, start = _seq, current(), time.monotonic()
    info: Dict[str, Any] = {"detail": None}
    _ACTIVE[key] = (who, title, start)
    logger.info(f"{who} ▶ {title}")
    try:
        yield info
    except BaseException as e:
        logger.warning(f"{who} ✗ {title} — {time.monotonic() - start:.1f} с: {type(e).__name__}: {str(e)[:200]}")
        raise
    else:
        tail = f" ({info['detail']})" if info.get("detail") else ""
        logger.info(f"{who} ✓ {title} — {time.monotonic() - start:.1f} с{tail}")
    finally:
        _ACTIVE.pop(key, None)


@contextlib.asynccontextmanager
async def step(title: str):
    """Этап с замером времени: ``async with progress.step("поиск похожих документов") as s: …; s["detail"] = "3"``.

    Args:
        title: Что делается (по-русски).

    Yields:
        Dict[str, Any]: ``detail`` — уточнение, которое попадёт в строку итога.
    """
    with _track(title) as info:
        yield info


def active(limit: int = 6) -> List[str]:
    """Этапы, которые выполняются сейчас (самые долгие первыми): «Файл 2805: анализ рисков 340 с»."""
    now = time.monotonic()
    items = sorted(_ACTIVE.values(), key=lambda v: v[2])[:limit]
    return [f"{who}: {title} {now - start:.0f} с" for who, title, start in items]


def analysis_title(analysis_type: str, cached: Optional[bool] = None) -> str:
    """Название этапа по виду анализа документа."""
    return ANALYSIS_TITLES.get(analysis_type.split(":")[0], analysis_type)
