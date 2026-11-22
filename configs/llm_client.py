"""Клиент мультимодальной модели (``M_MODEL_*``) для всего сервиса.

Клиент обёрнут в ``configs.llm_resilience.ResilientClient``: 504 / 5xx / таймауты шлюза повторяются с паузой
(``llm_resilience.PROXY_RETRIES``, по умолчанию 4 попытки с паузами 5 → 15 → 45 с; это единственный слой повторов
при сбоях сервера — ``async_retry`` вызывающего их не повторяет) вместо повторов SDK через ~0,5 с, одновременно идёт не больше ``llm_resilience.MAX_INFLIGHT`` запросов
(по умолчанию 4) на процесс, при серии сбоев все запросы процесса ждут 30–300 с. Отключается ``LLM_RESILIENT=false``.
"""
import asyncio
from functools import lru_cache
from typing import Any, Dict, Tuple

from openai import AsyncOpenAI

from configs.config import Config
from configs.llm_resilience import resilient


class LoopBoundOpenAI:
    """``AsyncOpenAI``, свой для каждого цикла событий: пул соединений httpx не переходит из цикла в цикл.

    Celery запускает каждую задачу в своём цикле (``asyncio.run``) и закрывает его. Общий на процесс
    ``AsyncOpenAI`` (``get_llm`` закэширован) держал в пуле соединения закрытого цикла, и первый запрос следующей
    задачи падал с ``RuntimeError: Event loop is closed`` (прод, 22.11.2026: определение типа документа).
    Здесь клиент создаётся заново для каждого цикла; клиенты закрытых циклов отбрасываются. Атрибуты и методы
    (``chat``, ``base_url``, ``with_options`` …) — как у ``AsyncOpenAI``.

    Args:
        **kwargs: Параметры ``AsyncOpenAI`` (``base_url``, ``api_key``, ``timeout``, ``max_retries`` …).
    """

    def __init__(self, **kwargs: Any):
        self._kwargs = kwargs
        self._clients: Dict[int, Tuple[Any, AsyncOpenAI]] = {}   # id(цикла) → (цикл, клиент)

    def _client(self) -> AsyncOpenAI:
        """Клиент текущего цикла событий (вне цикла — общий клиент для синхронного кода)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        key = id(loop)
        got = self._clients.get(key)
        if got is not None and got[0] is loop:
            return got[1]
        # закрытые циклы больше не понадобятся (их соединения непригодны); id могут переиспользоваться
        for k, (lp, _) in list(self._clients.items()):
            if lp is not None and lp.is_closed():
                del self._clients[k]
        client = AsyncOpenAI(**self._kwargs)
        self._clients[key] = (loop, client)
        return client

    def with_options(self, **kwargs: Any) -> "LoopBoundOpenAI":
        """Копия с другими параметрами (тоже своя для каждого цикла)."""
        return LoopBoundOpenAI(**{**self._kwargs, **kwargs})

    def __getattr__(self, name: str) -> Any:
        """``chat``, ``base_url``, ``models`` … — от клиента текущего цикла."""
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._client(), name)


@lru_cache(maxsize=1)
def get_llm():
    """Клиент и имя модели ``M_MODEL_*``.

    Клиент — :class:`LoopBoundOpenAI`: кэшируется на процесс, но соединения у каждого цикла событий свои.

    Returns:
        tuple: ``(клиент, модель)``.
    """
    config = Config.get_m_model_config()
    client = LoopBoundOpenAI(base_url=config.api_url.rstrip("/"), api_key=config.api_key, timeout=300)
    return resilient(client), config.model
