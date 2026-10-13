"""Ключ экспертизы в хранилище знаний: окружение (``X-API-Database``) + ``expertise_id``.

Один Postgres сервиса обслуживает все окружения (dev / stage / prod), а ``expertise_id`` в них
пересекаются, поэтому все таблицы ``pe_*`` ключуются парой ``(env, expertise_id)``. Функции
репозитория принимают :class:`Scope`, а не голый ``expertise_id`` — забыть окружение нельзя.
"""
import re
from typing import NamedTuple, Optional

ENV_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def normalize_env(env: Optional[str]) -> str:
    """Приводит имя окружения к виду, в котором оно хранится в ключах (нижний регистр, без пробелов).

    Args:
        env: Значение заголовка ``X-API-Database`` (``dev``, ``Stage``, ``prod`` …).

    Returns:
        str: Нормализованное имя окружения.

    Raises:
        ValueError: Окружение не задано или содержит недопустимые символы.
    """
    value = (env or "").strip().lower()
    if not ENV_RE.match(value):
        raise ValueError(f"Недопустимое окружение: {env!r}")
    return value


class Scope(NamedTuple):
    """Экспертиза в конкретном окружении.

    Attributes:
        env: Нормализованное окружение (``dev`` / ``stage`` / ``prod`` …).
        expertise_id: ID экспертизы в этом окружении.
    """

    env: str
    expertise_id: int

    @classmethod
    def of(cls, env: Optional[str], expertise_id: int) -> "Scope":
        """Создаёт ключ с нормализацией окружения и приведением ``expertise_id`` к ``int``.

        Args:
            env: Окружение (как пришло в заголовке).
            expertise_id: ID экспертизы.

        Returns:
            Scope: Ключ экспертизы.
        """
        return cls(normalize_env(env), int(expertise_id))

    def __str__(self) -> str:
        """Короткая запись для логов: ``prod:12345``."""
        return f"{self.env}:{self.expertise_id}"
