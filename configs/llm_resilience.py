"""Устойчивый вызов OpenAI-совместимой модели при перегрузке шлюза (504) — общий для экспертизы и риск-мониторинга.

* **потоковый ответ** (``stream=True``): шлюз (openresty / nginx) обрывает соединение, если ответа нет дольше
  своего таймаута (у ``llmhub`` — около 90 с); при потоке токены идут сразу, и долгая генерация не обрывается.
  Отключается ``LLM_STREAM=false`` (или ``RM_LLM_STREAM=false``);
* **без встроенных повторов SDK** (``max_retries=0``): SDK повторяет 504 через ~0,8 с, а модель тем
  временем продолжает считать оборванный запрос — нагрузка растёт лавиной. Повторы — свои, с паузой
  5 → 15 → 45 с и случайным разбросом;
* **общий лимит одновременных запросов** к одному серверу в процессе (``LLM_MAX_INFLIGHT`` /
  ``RM_LLM_MAX_INFLIGHT``, по умолчанию 4);
* **предохранитель**: после 3 подряд сбоев сервера (5xx, таймаут, обрыв) все запросы процесса ждут
  30 с (затем 60, 120 … до 300 с), чтобы сервер разгрузился, а не получал повторы.
"""
import asyncio
import os
import random
import time
from typing import Any, Dict, List, Optional, Tuple

from configs.logger import get_logger

logger = get_logger(__name__)


def _env(name: str, default: str) -> str:
    """Настройка ``LLM_<name>`` или, для совместимости, ``RM_LLM_<name>``."""
    return os.getenv(f"LLM_{name}") or os.getenv(f"RM_LLM_{name}") or default


RETRY_DELAYS = (5.0, 15.0, 45.0)    # паузы между попытками при сбое сервера, с
BREAKER_FAILS = 3                    # подряд сбоев до срабатывания предохранителя
BREAKER_PAUSE = (30.0, 300.0)        # пауза предохранителя: начальная и предельная, с
_gates: Dict[Tuple[int, str], asyncio.Semaphore] = {}
CALLS: Dict[str, int] = {"requests": 0, "failed": 0}   # счётчик запросов к модели в процессе (для сводки задачи)
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
        sem = _gates[(loop_id, key)] = asyncio.Semaphore(max(1, int(_env("MAX_INFLIGHT", "4"))))
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
    stream = _env("STREAM", "true").lower() not in ("0", "false", "no")
    last: Optional[BaseException] = None
    for attempt in range(max(1, retries)):
        await _breaker_wait(key)
        CALLS["requests"] += 1
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
            CALLS["failed"] += 1
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
