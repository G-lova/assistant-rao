"""Подмена переключателей ``configs/settings.py`` в тестах (раньше — переменные окружения)."""
import contextlib
from typing import Dict
from unittest import mock


@contextlib.contextmanager
def flags(values: Dict[str, str]):
    """Подменяет переключатели на время блока.

    Args:
        values: ``имя → "true" / "false"`` (как прежде в ``os.environ``).
    """
    from configs import settings

    with contextlib.ExitStack() as stack:
        for name, value in values.items():
            stack.enter_context(mock.patch.object(settings, name, str(value).lower() == "true"))
        yield
