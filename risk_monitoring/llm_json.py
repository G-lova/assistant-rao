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
  Отключается ``RM_LLM_STREAM=false``;
* **без встроенных повторов SDK** (``max_retries=0``): SDK повторяет 504 через ~0,8 с, а модель тем
  временем продолжает считать оборванный запрос — нагрузка растёт лавиной. Повторы — свои, с паузой
  5 → 15 → 45 с и случайным разбросом;
* **общий лимит одновременных запросов** к одному серверу в процессе (``RM_LLM_MAX_INFLIGHT``, 4);
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
    return AsyncOpenAI(base_url=url.rstrip("/"), api_key=key, timeout=300), model


def _guided_unsupported(error: BaseException) -> bool:
    """Похоже ли, что сервер отверг ``guided_json`` (HTTP 400 / упоминание guided в ошибке)."""
    status = getattr(error, "status_code", None)
    text = str(error).lower()
    return status == 400 and ("guided" in text or "extra" in text or "json" in text or "schema" in text)


RETRY_DELAYS = (5.0, 15.0, 45.0)    # паузы между попытками при сбое сервера, с
BREAKER_FAILS = 3                    # подряд сбоев до срабатывания предохранителя
BREAKER_PAUSE = (30.0, 300.0)        # пауза предохранителя: начальная и предельная, с
_gates: Dict[Tuple[int, str], asyncio.Semaphore] = {}
_breaker: Dict[str, Dict[str, float]] = {}


def transient(error: BaseException) -> bool:
    """Временный сбой сервера: 5xx, 408/409/429, таймаут, обрыв соединения.

    Args:
        error: Исключение клиента.

    Returns:
        bool: ``True`` — имеет смысл повторить позже.
    """
    status = getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int):
        return status >= 500 or status in (408, 409, 429)
    name = type(error).__name__.lower()
    return isinstance(error, (asyncio.TimeoutError, ConnectionError)) or any(
        k in name for k in ("timeout", "connection", "remoteprotocol", "readerror"))


def _server_key(client: Any) -> str:
    """Ключ сервера модели для лимита и предохранителя (базовый URL клиента)."""
    return str(getattr(client, "base_url", "") or id(client))


def _gate(key: str) -> asyncio.Semaphore:
    """Семафор одновременных запросов к серверу в текущем цикле событий (Celery запускает новый цикл на задачу)."""
    loop_id = id(asyncio.get_running_loop())
    sem = _gates.get((loop_id, key))
    if sem is None:
        sem = _gates[(loop_id, key)] = asyncio.Semaphore(max(1, int(os.getenv("RM_LLM_MAX_INFLIGHT", "4"))))
    return sem


async def _breaker_wait(key: str) -> None:
    """Ждёт, если предохранитель сервера сработал."""
    state = _breaker.get(key)
    if state and state.get("open_until", 0) > time.monotonic():
        wait = state["open_until"] - time.monotonic()
        logger.warning(f"LLM {key}: сервер перегружен, пауза {wait:.0f} с")
        await asyncio.sleep(wait)


def _breaker_record(key: str, ok: bool) -> None:
    """Учитывает результат запроса: успех сбрасывает счётчик, серия сбоев открывает паузу."""
    state = _breaker.setdefault(key, {"fails": 0, "pause": BREAKER_PAUSE[0], "open_until": 0.0})
    if ok:
        state.update(fails=0, pause=BREAKER_PAUSE[0])
        return
    state["fails"] += 1
    if state["fails"] >= BREAKER_FAILS:
        state["open_until"] = time.monotonic() + state["pause"]
        logger.warning(f"LLM {key}: {state['fails']} сбоя подряд — пауза {state['pause']:.0f} с")
        state["pause"] = min(state["pause"] * 2, BREAKER_PAUSE[1])
        state["fails"] = 0


async def _create(client: Any, stream: bool, kwargs: Dict[str, Any]) -> str:
    """Один запрос ``chat.completions.create`` без повторов SDK; при потоке собирает текст из чанков."""
    c = client.with_options(max_retries=0) if hasattr(client, "with_options") else client
    if not stream:
        resp = await c.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""
    parts: List[str] = []
    resp = await c.chat.completions.create(**kwargs, stream=True)
    if not hasattr(resp, "__aiter__"):  # сервер или обёртка вернули обычный ответ
        return resp.choices[0].message.content or ""
    async for chunk in resp:
        if getattr(chunk, "choices", None):
            delta = getattr(chunk.choices[0], "delta", None)
            if delta is not None and getattr(delta, "content", None):
                parts.append(delta.content)
    return "".join(parts)


async def call_llm(client: Any, retries: int = len(RETRY_DELAYS) + 1, **kwargs: Any) -> str:
    """Запрос к модели, устойчивый к перегрузке: поток, лимит, предохранитель, паузы между попытками.

    Ошибки, не связанные с перегрузкой (400 и т. п.), не повторяются и пробрасываются сразу.

    Args:
        client: OpenAI-совместимый асинхронный клиент.
        retries: Число попыток при временных сбоях сервера.
        **kwargs: Параметры ``chat.completions.create``.

    Returns:
        str: Текст ответа.

    Raises:
        Exception: Постоянная ошибка или последняя временная после исчерпания попыток.
    """
    key = _server_key(client)
    stream = os.getenv("RM_LLM_STREAM", "true").lower() not in ("0", "false", "no")
    last: Optional[BaseException] = None
    for attempt in range(max(1, retries)):
        await _breaker_wait(key)
        try:
            async with _gate(key):
                text = await _create(client, stream, kwargs)
            _breaker_record(key, True)
            return text
        except Exception as e:  # noqa: BLE001
            if stream and getattr(e, "status_code", None) == 400 and "stream" in str(e).lower():
                logger.warning(f"LLM {key}: поток не поддерживается, перехожу на обычный ответ")
                stream = False
                continue
            if not transient(e):
                raise
            last = e
            _breaker_record(key, False)
            if attempt < retries - 1:
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)] * random.uniform(0.8, 1.3)
                logger.warning(f"LLM {key}: {type(e).__name__} ({getattr(e, 'status_code', '')}), "
                               f"попытка {attempt + 1}/{retries}, ждём {delay:.0f} с")
                await asyncio.sleep(delay)
    raise last  # type: ignore[misc]


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
        last: Optional[BaseException] = None
        for attempt in range(self.retries):
            guided = bool(schema) and self.guided
            sys_text = system if guided or not schema else (
                system + "\n\nОтвет — строго один JSON-объект по схеме:\n" + json.dumps(schema, ensure_ascii=False))
            messages: List[Dict[str, str]] = [{"role": "system", "content": sys_text}, {"role": "user", "content": user}]
            try:
                async with self.semaphore:
                    await self.rate_limiter.acquire()
                    content = await call_llm(
                        self.client, model=self.model, messages=messages, max_tokens=max_tokens,
                        temperature=temperature, extra_body={"guided_json": schema} if guided else {})
                data = parse_json(content)
                if not isinstance(data, dict):
                    raise ValueError("Ответ модели не является объектом")
                return data
            except Exception as e:  # noqa: BLE001 — битый JSON / постоянная ошибка: повторяем
                last = e
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
