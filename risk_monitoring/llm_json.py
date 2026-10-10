"""Вызов LLM со структурированным ответом (guided JSON) для анализаторов риск-мониторинга.

Общая обвязка: ограничение частоты запросов, повторы при сбоях, разбор JSON с починкой
(``json_repair``), версия промпта как хэш промпта и схемы — смена промпта сама инвалидирует кэш
анализа в ``ai_document_analysis``.
"""
import asyncio
import hashlib
import json
import re
from typing import Any, Dict, List, Optional

from configs.logger import get_logger

logger = get_logger(__name__)


def prompt_version(*parts: Any) -> str:
    """Версия промпта: короткий хэш его текста, схемы и прочих параметров.

    Args:
        *parts: Строки или JSON-совместимые объекты (промпт, схема, каталог кодов).

    Returns:
        str: 12 символов SHA-256.
    """
    h = hashlib.sha256()
    for p in parts:
        h.update((p if isinstance(p, str) else json.dumps(p, ensure_ascii=False, sort_keys=True)).encode("utf-8"))
    return h.hexdigest()[:12]


def parse_json(raw: str) -> Any:
    """Разбирает ответ модели как JSON: сначала строго, затем с починкой, затем первый ``{...}``.

    Args:
        raw: Сырой ответ модели.

    Returns:
        Any: Разобранный объект.

    Raises:
        ValueError: Если JSON извлечь не удалось.
    """
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        from json_repair import repair_json  # ленивый импорт: зависимость есть в requirements
        fixed = repair_json(text)
        data = json.loads(fixed) if isinstance(fixed, str) else fixed
        if data not in ("", None):
            return data
    except Exception:  # noqa: BLE001 — переходим к последнему способу
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        return json.loads(m.group(0))
    raise ValueError("В ответе модели нет JSON")


class LLMJson:
    """Обёртка над OpenAI-совместимым клиентом: один метод ``ask`` → словарь.

    Args:
        client: Асинхронный клиент (``chat.completions.create``).
        model: Модель.
        rate: Запросов в секунду (общий лимит для экземпляра).
        concurrency: Одновременных запросов.
        retries: Повторов при сбое или неразбираемом ответе.
    """

    def __init__(self, client: Any, model: str, rate: float = 1.5, concurrency: int = 3, retries: int = 3):
        """Создаёт обёртку; лимитер частоты — ``configs.rate_limiter.TokenBucket``."""
        from configs.rate_limiter import TokenBucket

        self.client = client
        self.model = model
        self.rate_limiter = TokenBucket(rate=rate)
        self.semaphore = asyncio.Semaphore(concurrency)
        self.retries = max(1, retries)

    async def ask(self, system: str, user: str, schema: Optional[Dict[str, Any]] = None,
                  max_tokens: int = 3000, temperature: float = 0.1) -> Dict[str, Any]:
        """Запрос к модели с guided JSON и разбором ответа.

        Args:
            system: Системный промпт.
            user: Сообщение пользователя.
            schema: JSON-схема ответа (передаётся как ``guided_json``).
            max_tokens: Лимит токенов ответа.
            temperature: Температура.

        Returns:
            Dict[str, Any]: Разобранный ответ.

        Raises:
            Exception: Последняя ошибка после исчерпания повторов.
        """
        messages: List[Dict[str, str]] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        extra = {"guided_json": schema} if schema else {}
        last: Optional[BaseException] = None
        for attempt in range(self.retries):
            try:
                async with self.semaphore:
                    await self.rate_limiter.acquire()
                    resp = await self.client.chat.completions.create(
                        model=self.model, messages=messages, max_tokens=max_tokens,
                        temperature=temperature, extra_body=extra)
                data = parse_json(resp.choices[0].message.content)
                if not isinstance(data, dict):
                    raise ValueError("Ответ модели не является объектом")
                return data
            except Exception as e:  # noqa: BLE001 — сеть, таймаут, битый JSON: повторяем
                last = e
                if attempt < self.retries - 1:
                    wait = 2 * (2 ** attempt)
                    logger.warning(f"LLM: попытка {attempt + 1}/{self.retries} не удалась ({type(e).__name__}: {e}), ждём {wait} с")
                    await asyncio.sleep(wait)
        raise last  # type: ignore[misc]
