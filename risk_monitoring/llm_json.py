"""Вызов LLM со структурированным ответом (guided JSON) для анализаторов риск-мониторинга.

Общая обвязка: ограничение частоты запросов, повторы при сбоях, разбор JSON с починкой
(``json_repair``), версия промпта как хэш промпта и схемы — смена промпта сама инвалидирует кэш
анализа в ``ai_document_analysis``.

Модели. Распознавание и извлечение данных выполняет модель из ``get_llm()`` (``M_MODEL_NAME``,
мультимодальная). Смысловые задачи риск-мониторинга — паспорт документа, риски, сравнение редакций,
анализ событий — может выполнять другая, более сильная модель: ``RM_REASONING_MODEL``
(например, ``prod_big_llm``) на том же сервере или на ``RM_REASONING_API_URL``. Если переменная
не задана, используется модель ``get_llm()``.

Если сервер модели не поддерживает ``guided_json`` (ошибка 400 на ``extra_body``), обёртка
переключается на запрос без него: схема передаётся в системном промпте, ответ разбирается и чинится.

Таймауты шлюза (504) — ``call_llm``:

* **потоковый ответ** (``stream=True``): шлюз (nginx) обрывает соединение, если ответа нет дольше
  ``proxy_read_timeout`` (обычно 60 с); при потоке токены идут сразу, и долгая генерация не обрывается.
  Отключается ``configs.llm_resilience.STREAM``;
* **без встроенных повторов SDK** (``max_retries=0``): SDK повторяет 504 через ~0,8 с, а модель тем
  временем продолжает считать оборванный запрос — нагрузка растёт лавиной. Повторы — свои, с паузой
  5 → 15 → 45 с и случайным разбросом;
* **общий лимит одновременных запросов** к одному серверу в процессе (``configs.llm_resilience.MAX_INFLIGHT``, 4);
* **предохранитель**: после 3 подряд сбоев сервера (5xx, таймаут, обрыв) все запросы процесса ждут
  30 с (затем 60, 120 … до 300 с), чтобы сервер разгрузился, а не получал повторы.
"""
import asyncio
import hashlib
import json
import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

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
    # размышления модели в ответе (модели с «мышлением» без разбора reasoning на сервере): <think>…</think>;
    # незакрытый <think> — ответ оборван на размышлениях, JSON нет (30.10.2026, перепроверка правил)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I).strip()
    if re.match(r"<think>", text, re.I):
        raise ValueError("Ответ модели оборван на размышлениях (<think> без JSON) — мало max_tokens")
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


def reasoning_llm(default_client: Any = None, default_model: Optional[str] = None) -> Tuple[Any, str]:
    """Клиент и модель для смысловых задач риск-мониторинга.

    Args:
        default_client: Клиент из ``get_llm()`` — используется, если отдельная модель не задана
            или задана на том же сервере.
        default_model: Модель из ``get_llm()``.

    Returns:
        Tuple[Any, str]: ``(клиент, модель)``.
    """
    model = (os.getenv("RM_REASONING_MODEL") or "").strip()
    url = (os.getenv("RM_REASONING_API_URL") or "").strip()
    if default_client is None or default_model is None:
        from configs.llm_client import get_llm
        default_client, default_model = get_llm()
    if not model:
        return default_client, default_model
    if not url:
        return default_client, model
    from openai import AsyncOpenAI

    from configs.config import Config
    key = os.getenv("RM_REASONING_API_KEY") or Config.OPENAI_API_KEY
    from configs.llm_resilience import resilient

    return resilient(AsyncOpenAI(base_url=url.rstrip("/"), api_key=key, timeout=300)), model


def _guided_unsupported(error: BaseException) -> bool:
    """Похоже ли, что сервер отверг ``guided_json`` (HTTP 400 / упоминание guided в ошибке)."""
    status = getattr(error, "status_code", None)
    text = str(error).lower()
    return status == 400 and ("guided" in text or "extra" in text or "json" in text or "schema" in text)


from configs.llm_resilience import (  # noqa: E402,F401 — общий устойчивый вызов модели (экспертиза и риск-мониторинг)
    BREAKER_FAILS, BREAKER_PAUSE, CALLS, RETRY_DELAYS, _breaker, _breaker_record, _breaker_wait, _create, _gate, _gates,
    _server_key, call_llm, transient,
)


class LLMJson:
    """Обёртка над OpenAI-совместимым клиентом: один метод ``ask`` → словарь.

    Args:
        client: Асинхронный клиент (``chat.completions.create``).
        model: Модель.
        rate: Запросов в секунду (общий лимит для экземпляра).
        concurrency: Одновременных запросов.
        retries: Повторов при неразбираемом ответе (сбои сервера повторяет ``call_llm``).
    """

    def __init__(self, client: Any, model: str, rate: float = 1.5, concurrency: int = 3, retries: int = 3):
        """Создаёт обёртку; лимитер частоты — ``configs.rate_limiter.TokenBucket``."""
        from configs.rate_limiter import TokenBucket

        self.client = client
        self.model = model
        self.rate_limiter = TokenBucket(rate=rate)
        self.semaphore = asyncio.Semaphore(concurrency)
        self.retries = max(1, retries)
        self.guided = True

    async def ask_text(self, system: str, user: str, max_tokens: int = 1500, temperature: float = 0.2) -> str:
        """Запрос к модели за обычным текстом (без JSON): резюме, пояснения.

        Модели рассуждений на guided JSON с одним текстовым полем иногда отвечают без JSON («В ответе модели нет
        JSON», резюме закупки 20.10.2026), а текст им писать проще. Ответ очищается от ``<think>`` и обрамления.

        Returns:
            str: Текст ответа (пустая строка — модель ничего не написала).
        """
        import re

        async with self.semaphore:
            await self.rate_limiter.acquire()
            content = await call_llm(self.client, model=self.model, max_tokens=max_tokens, temperature=temperature,
                                     messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        text = re.sub(r"(?s)<think>.*?</think>", "", content or "").strip()
        text = re.sub(r"^```\w*\s*|\s*```$", "", text).strip()
        if text.startswith("{"):   # модель всё же ответила JSON
            try:
                data = parse_json(text)
                if isinstance(data, dict):
                    text = str(data.get("resume") or next(iter(data.values()), "") or "").strip()
            except Exception:  # noqa: BLE001
                pass
        return text

    async def ask(self, system: str, user: str, schema: Optional[Dict[str, Any]] = None,
                  max_tokens: int = 3000, temperature: float = 0.1, list_key: Optional[str] = None) -> Dict[str, Any]:
        """Запрос к модели с guided JSON и разбором ответа.

        Args:
            system: Системный промпт.
            user: Сообщение пользователя.
            schema: JSON-схема ответа (передаётся как ``guided_json``).
            max_tokens: Лимит токенов ответа.
            temperature: Температура.
            list_key: Модель вернула массив вместо объекта — принять его как ``{list_key: массив}`` (схема с одним
                полем-массивом, например ``checks``).

        Returns:
            Dict[str, Any]: Разобранный ответ.

        Raises:
            Exception: Последняя ошибка после исчерпания повторов.
        """
        last: Optional[BaseException] = None
        for attempt in range(self.retries):
            guided = bool(schema) and self.guided
            sys_text = system if guided or not schema else (
                system + "\n\nОтвет — строго один JSON-объект по схеме:\n" + json.dumps(schema, ensure_ascii=False))
            messages: List[Dict[str, str]] = [{"role": "system", "content": sys_text}, {"role": "user", "content": user}]
            content = None
            try:
                async with self.semaphore:
                    await self.rate_limiter.acquire()
                    content = await call_llm(
                        self.client, model=self.model, messages=messages, max_tokens=max_tokens,
                        temperature=temperature, extra_body={"guided_json": schema} if guided else {})
                data = parse_json(content)
                if isinstance(data, list) and list_key:
                    data = {list_key: data}
                if not isinstance(data, dict):
                    raise ValueError("Ответ модели не является объектом")
                return data
            except Exception as e:  # noqa: BLE001 — битый JSON / постоянная ошибка: повторяем
                last = e
                if content is not None and isinstance(e, ValueError):
                    # что именно ответила модель — без этого «нет JSON» не разобрать (30.10.2026)
                    head = re.sub(r"\s+", " ", str(content))[:300]
                    logger.warning(f"LLM {self.model}: ответ не разобран ({len(str(content))} симв., guided={guided}): {head!r}")
                if guided and _guided_unsupported(e):
                    logger.warning(f"LLM {self.model}: guided_json не поддерживается, перехожу на схему в промпте")
                    self.guided = False
                    continue
                if transient(e):
                    break  # call_llm уже выждал и повторил: сервер недоступен, не добавляем нагрузку
                if attempt < self.retries - 1:
                    wait = 2 * (2 ** attempt)
                    logger.warning(f"LLM: попытка {attempt + 1}/{self.retries} не удалась ({type(e).__name__}: {e}), ждём {wait} с")
                    await asyncio.sleep(wait)
        raise last  # type: ignore[misc]
