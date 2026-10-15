"""Клиент мультимодальной модели (``M_MODEL_*``) для всего сервиса.

Клиент обёрнут в ``configs.llm_resilience.ResilientClient``: 504 / 5xx / таймауты шлюза повторяются с паузой
(``LLM_PROXY_RETRIES``, по умолчанию одна повторная попытка через ~5 с) вместо повторов SDK через ~0,5 с, одновременно идёт не больше ``LLM_MAX_INFLIGHT`` запросов
(по умолчанию 4) на процесс, при серии сбоев все запросы процесса ждут 30–300 с. Отключается ``LLM_RESILIENT=false``.
"""
from functools import lru_cache

from openai import AsyncOpenAI

from configs.config import Config
from configs.llm_resilience import resilient


@lru_cache(maxsize=1)
def get_llm():
    """Клиент и имя модели ``M_MODEL_*``.

    Returns:
        tuple: ``(клиент, модель)``.
    """
    config = Config.get_m_model_config()
    client = AsyncOpenAI(base_url=config.api_url.rstrip("/"), api_key=config.api_key, timeout=300)
    return resilient(client), config.model
