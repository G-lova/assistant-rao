"""Импорт модулей сервиса в песочнице без тяжёлых зависимостей (aiohttp, openai, asyncpg …).

Отсутствующие пакеты временно подменяются ``MagicMock``-модулями только на время импорта и теста; после
выхода заглушки и импортированные с ними модули проекта убираются, прежние модули проекта возвращаются.
"""
import importlib
import sys
import types
from contextlib import contextmanager
from typing import Iterator
from unittest import mock

PROJECT_PREFIXES = ("evaluate_documents", "configs", "src", "knowledge_store", "risk_monitoring", "conclusion",
                    "search_experts")


@contextmanager
def stubbed_import(name: str, attempts: int = 60) -> Iterator[object]:
    """Импортирует модуль, подменяя заглушками недостающие сторонние пакеты.

    Args:
        name: Имя модуля проекта.
        attempts: Максимум подмен.

    Yields:
        module: Импортированный модуль (действует до выхода из контекста).
    """
    before = {k: v for k, v in sys.modules.items() if k.startswith(PROJECT_PREFIXES)}
    stubs = []
    # упрощённые заглушки других тестов (types.ModuleType без __spec__, например aiohttp из test_summary)
    # на время импорта заменяются полными MagicMock-модулями
    partial = {k: v for k, v in sys.modules.items()
               if isinstance(v, types.ModuleType) and getattr(v, "__spec__", None) is None
               and not k.startswith(PROJECT_PREFIXES) and k not in sys.builtin_module_names and k != "__main__"}
    for key in partial:
        sys.modules[key] = mock.MagicMock()
    try:
        for _ in range(attempts):
            try:
                module = importlib.import_module(name)
                break
            except ModuleNotFoundError as e:
                if not e.name or e.name.startswith(PROJECT_PREFIXES):
                    raise
                stub = mock.MagicMock()
                stub.__path__ = []
                stub.__spec__ = None
                sys.modules[e.name] = stub
                stubs.append(e.name)
                for key in [k for k in sys.modules if k.startswith(PROJECT_PREFIXES)]:
                    sys.modules.pop(key, None)
        else:
            raise ImportError(f"не удалось импортировать {name}")
        yield module
    finally:
        # убираем заглушки и модули проекта, импортированные с ними; настоящие сторонние пакеты остаются
        for key in stubs:
            sys.modules.pop(key, None)
        for key in [k for k in sys.modules if k.startswith(PROJECT_PREFIXES)]:
            sys.modules.pop(key, None)
        sys.modules.update(before)
        sys.modules.update(partial)
