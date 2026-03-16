from openai import AsyncOpenAI
from configs.config import Config


_client = None
_model = None

def get_llm():
    global _client, _model

    if _client is None:
        config = Config().get_m_model_config()

        _client = AsyncOpenAI(
            base_url=config.api_url.rstrip("/"),
            api_key=config.api_key,
            timeout=300
        )
        _model = config.model

    return _client, _model