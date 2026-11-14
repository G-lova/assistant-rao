"""Файлы ресурсов проекта (промпты, схемы, SQL, XML-шаблоны) по пути от корня репозитория, в UTF-8, с кэшем.

Раньше файлы открывались относительным путём (``open("prompts/ocr_prompt.txt")``) без ``encoding=``: код работал,
только если процесс запущен из корня репозитория, а кодировка зависела от локали контейнера (в ``C``/``POSIX``
русский текст промптов не читался бы). И каждый новый объект (``OCRProcessor``, ``TypeDetector`` …) читал
файлы с диска заново.

Текст кэшируется на время жизни процесса: файлы ресурсов меняются только с новой версией кода, а API и воркеры
перезапускаются при её выкладке.
"""
from functools import lru_cache
from pathlib import Path
from typing import Union

ROOT = Path(__file__).resolve().parent.parent
"""Корень репозитория (каталог с ``prompts/``, ``schemas/``, ``queries/``, ``xml/``)."""


def resource_path(path: Union[str, Path]) -> Path:
    """Абсолютный путь к ресурсу: относительный путь считается от корня репозитория, абсолютный — как есть.

    Args:
        path: Например, ``"prompts/ocr_prompt.txt"``.

    Returns:
        Path: Абсолютный путь.
    """
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


@lru_cache(maxsize=256)
def _read_cached(path: str) -> str:
    """Текст файла по абсолютному пути (кэш процесса)."""
    return Path(path).read_text(encoding="utf-8")


def read_text(path: Union[str, Path]) -> str:
    """Текст файла ресурса в UTF-8 (кэшируется на время жизни процесса).

    Args:
        path: Путь от корня репозитория или абсолютный.

    Returns:
        str: Содержимое файла.

    Raises:
        OSError: Файла нет или он не читается.
    """
    return _read_cached(str(resource_path(path)))
