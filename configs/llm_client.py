from openai import AsyncOpenAI
from configs.config import Config
from functools import lru_cache

@lru_cache(maxsize=1)
def get_llm():
    config = Config.get_m_model_config()
    return AsyncOpenAI(base_url=config.api_url.rstrip("/"), api_key=config.api_key, timeout=300), config.model