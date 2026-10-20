"""Устойчивый вызов OpenAI-совместимой модели при перегрузке шлюза (504) — общий для экспертизы и риск-мониторинга.

* **потоковый ответ** (``stream=True``): шлюз (openresty / nginx) обрывает соединение, если ответа нет дольше
  своего таймаута (у ``llmhub`` — около 90 с); при потоке токены идут сразу, и долгая генерация не обрывается.
  Отключается константой ``STREAM`` в этом модуле;
* **без встроенных повторов SDK** (``max_retries=0``): SDK повторяет 504 через ~0,8 с, а модель тем
  временем продолжает считать оборванный запрос — нагрузка растёт лавиной. Повторы — свои, с паузой
  5 → 15 → 45 с и случайным разбросом;
* **общий лимит одновременных запросов** к одному серверу в процессе (``MAX_INFLIGHT``, 4);
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


# Переключатели — в коде, не в .env
MAX_INFLIGHT = 4        # одновременных запросов к одному серверу модели в процессе
STREAM = True           # потоковый ответ в call_llm (шлюз не обрывает долгую генерацию)
PROXY_RETRIES = 2       # попыток в ResilientClient по умолчанию (у вызовов экспертизы есть свои повторы)
RESILIENT = True        # оборачивать ли клиент get_llm() в ResilientClient
# Предел одного запроса к модели целиком, с. Таймаут клиента (300 с) — это пауза между порциями ответа: поток, который
# медленно идёт (модель зациклилась до max_tokens), им не обрывается, и задача «молчит» десятки минут (20.10.2026).
# По пределу запрос прерывается как временный сбой (asyncio.TimeoutError) — с записью в журнал и повтором.
REQUEST_DEADLINE = 420


RETRY_DELAYS = (5.0, 15.0, 45.0)    # паузы между попытками при сбое сервера, с
BREAKER_FAILS = 3                    # подряд сбоев до срабатывания предохранителя
BREAKER_PAUSE = (30.0, 300.0)        # пауза предохранителя: начальная и предельная, с
_gates: Dict[Tuple[int, str], asyncio.Semaphore] = {}
# счётчики запросов к модели в процессе: для сводки задачи и пульса (inflight — выполняются сейчас)
CALLS: Dict[str, int] = {"requests": 0, "failed": 0, "inflight": 0, "deadline": 0}
_breaker: Dict[str, Dict[str, float]] = {}


UNAVAILABLE_RE = __import__("re").compile(
    r"\b(404|500|502|503|504)\b|Gateway|Service Unavailable|Internal Server Error|timed out|Timeout", __import__("re").I)


# Переполнение контекста (vLLM, 400): «maximum context length is 16384 tokens. However, you requested 3000 output
# tokens and your prompt contains at least 13385 input tokens»
CONTEXT_RE = __import__("re").compile(
    r"maximum context length is (\d+) tokens.*?requested (\d+) output tokens.*?(?:prompt contains|messages resulted in)"
    r"\D*(\d+) input tokens", __import__("re").I | __import__("re").S)
MIN_OUTPUT_TOKENS = 1024     # меньше — ответ JSON не поместится, запрос делится вызывающим
CONTEXT_MARGIN = 32          # запас: сервер считает «at least» и добавляет служебные токены


class ContextOverflow(ValueError):
    """Запрос не помещается в контекст модели даже с уменьшенным ответом — текст нужно делить.

    Attributes:
        limit: Контекст модели (токенов).
        input_tokens: Токенов в запросе.
    """

    def __init__(self, limit: int, input_tokens: int, message: str):
        """Сохраняет размеры."""
        super().__init__(message)
        self.limit, self.input_tokens = limit, input_tokens


def context_overflow(error: BaseException) -> Optional[tuple]:
    """Размеры из ошибки переполнения контекста: ``(контекст, запрошено на ответ, токенов запроса)`` или ``None``."""
    if isinstance(error, ContextOverflow):
        return error.limit, 0, error.input_tokens
    if getattr(error, "status_code", None) not in (400, None):
        return None
    m = CONTEXT_RE.search(str(error))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def fit_output(error: BaseException, kwargs: Dict[str, Any]) -> bool:
    """При переполнении контекста уменьшает ``max_tokens`` в ``kwargs``, если на ответ остаётся достаточно места.

    Args:
        error: Ошибка запроса.
        kwargs: Параметры ``chat.completions.create`` (меняются на месте).

    Returns:
        bool: ``True`` — повторить с новым ``max_tokens``.

    Raises:
        ContextOverflow: Запрос не помещается даже с ответом ``MIN_OUTPUT_TOKENS`` — текст нужно делить.
    """
    sizes = context_overflow(error)
    if not sizes:
        return False
    limit, _, used = sizes
    room = limit - used - CONTEXT_MARGIN
    if room >= MIN_OUTPUT_TOKENS and room < int(kwargs.get("max_tokens") or limit):
        logger.warning(f"LLM: запрос {used} токенов из {limit}, ответ уменьшен с {kwargs.get('max_tokens')} до {room}")
        kwargs["max_tokens"] = room
        return True
    raise ContextOverflow(limit, used, f"запрос {used} токенов не помещается в контекст {limit}: разделите текст") from error


def split_halves(text: str) -> List[str]:
    """Делит текст на две части по границе абзаца или строки ближе к середине (для :class:`ContextOverflow`).

    Args:
        text: Текст блока.

    Returns:
        List[str]: Две непустые части или исходный текст одним элементом, если делить нечего.
    """
    text = text or ""
    if len(text) < 200:
        return [text]
    mid = len(text) // 2
    cut = -1
    for sep in ("\n\n", "\n", ". ", " "):
        left, right = text.rfind(sep, 0, mid), text.find(sep, mid)
        cands = [c for c in (left, right) if c > len(text) // 5 and c < len(text) - len(text) // 5]
        if cands:
            cut = min(cands, key=lambda c: abs(c - mid)) + len(sep)
            break
    cut = cut if cut > 0 else mid
    return [p for p in (text[:cut], text[cut:]) if p.strip()]


def unavailable_text(text: str) -> bool:
    """Похожа ли текстовая причина отказа на недоступность сервера модели (для сводок без исключения)."""
    return bool(UNAVAILABLE_RE.search(text or ""))


def transient(error: BaseException) -> bool:
    """Временный сбой сервера: 5xx, 408/409/429, таймаут, обрыв соединения, а также 404 шлюза на
    ``/chat/completions`` — модель снята с шлюза на время перезапуска (в логах 12.10.2026: 504 → 500 → 503 → 404,
    после чего модель вернулась).

    Args:
        error: Исключение клиента.

    Returns:
        bool: ``True`` — имеет смысл повторить позже.
    """
    status = getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int):
        return status >= 500 or status in (404, 408, 409, 429)
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
        sem = _gates[(loop_id, key)] = asyncio.Semaphore(max(1, int(MAX_INFLIGHT)))
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


async def _bounded(coro: Any, what: str) -> Any:
    """Выполняет запрос к модели не дольше ``REQUEST_DEADLINE``; учитывает выполняющиеся запросы (``inflight``).

    Raises:
        asyncio.TimeoutError: Запрос превысил предел (временный сбой — вызывающий повторяет его).
    """
    CALLS["inflight"] += 1
    try:
        return await asyncio.wait_for(coro, timeout=REQUEST_DEADLINE)
    except asyncio.TimeoutError:
        CALLS["deadline"] += 1
        logger.warning(f"LLM {what}: запрос дольше {REQUEST_DEADLINE} с — прерван")
        raise
    finally:
        CALLS["inflight"] -= 1


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
    client = getattr(client, "raw_client", client)   # ResilientClient: без вложенных повторов
    kwargs = dict(kwargs)
    key = _server_key(client)
    stream = bool(STREAM)
    last: Optional[BaseException] = None
    retries = max(1, retries)
    extra = 0   # смена потока / уменьшение ответа — не считаются попытками
    for attempt in range(retries + 3):
        if attempt - extra >= retries:
            break
        await _breaker_wait(key)
        CALLS["requests"] += 1
        try:
            async with _gate(key):
                text = await _bounded(_create(client, stream, kwargs), key)
            _breaker_record(key, True)
            return text
        except Exception as e:  # noqa: BLE001
            if stream and getattr(e, "status_code", None) == 400 and "stream" in str(e).lower():
                logger.warning(f"LLM {key}: поток не поддерживается, перехожу на обычный ответ")
                stream = False
                extra += 1
                continue
            CALLS["failed"] += 1
            if fit_output(e, kwargs):   # переполнение контекста: меньший ответ или ContextOverflow
                extra += 1
                continue
            if not transient(e):
                raise
            last = e
            _breaker_record(key, False)
            if attempt - extra < retries - 1:
                delay = RETRY_DELAYS[min(attempt - extra, len(RETRY_DELAYS) - 1)] * random.uniform(0.8, 1.3)
                logger.warning(f"LLM {key}: {type(e).__name__} ({getattr(e, 'status_code', '')}), "
                               f"попытка {attempt - extra + 1}/{retries}, ждём {delay:.0f} с")
                await asyncio.sleep(delay)
    raise last or RuntimeError("LLM: попытки исчерпаны")  # type: ignore[misc]


class _Completions:
    """``chat.completions`` с устойчивым ``create``: лимит, предохранитель, паузы между попытками."""

    def __init__(self, raw: Any, retries: Optional[int] = None):
        """Запоминает исходный клиент (без повторов SDK) и число попыток (``None`` — ``PROXY_RETRIES``)."""
        self._raw = raw
        self._retries = retries

    async def create(self, **kwargs: Any) -> Any:
        """Как ``AsyncOpenAI.chat.completions.create``, но 504 / 5xx / таймауты повторяются с паузой (по умолчанию
        одна повторная попытка через ~5 с, ``PROXY_RETRIES``), не больше ``MAX_INFLIGHT`` запросов
        одновременно, при серии сбоев — общая пауза всех запросов процесса.

        Потоковый запрос (``stream=True``) передаётся как есть (повторяет его вызывающий, например ``call_llm``).

        Returns:
            Any: Ответ SDK (тот же тип, что у исходного клиента).
        """
        if kwargs.get("stream"):
            return await self._raw.chat.completions.create(**kwargs)
        kwargs = dict(kwargs)
        key = _server_key(self._raw)
        # Многие вызовы экспертизы уже обёрнуты своими повторами (async_retry): здесь по умолчанию одна
        # повторная попытка через ~5 с, чтобы повторы не перемножались (PROXY_RETRIES)
        retries = max(1, int(self._retries if self._retries is not None else PROXY_RETRIES))
        last: Optional[BaseException] = None
        extra = 0   # повторы из-за уменьшения ответа (не считаются попытками)
        for attempt in range(retries + 2):
            if attempt - extra >= retries:
                break
            await _breaker_wait(key)
            CALLS["requests"] += 1
            try:
                async with _gate(key):
                    resp = await _bounded(self._raw.chat.completions.create(**kwargs), key)
                _breaker_record(key, True)
                return resp
            except Exception as e:  # noqa: BLE001
                CALLS["failed"] += 1
                if fit_output(e, kwargs):   # переполнение контекста: меньший ответ (попытка не тратится)
                    extra += 1
                    continue
                if not transient(e):
                    raise
                last = e
                _breaker_record(key, False)
                if attempt - extra < retries - 1:
                    delay = RETRY_DELAYS[min(attempt - extra, len(RETRY_DELAYS) - 1)] * random.uniform(0.8, 1.3)
                    logger.warning(f"LLM {key}: {type(e).__name__} ({getattr(e, 'status_code', '')}), "
                                   f"попытка {attempt - extra + 1}/{retries}, ждём {delay:.0f} с")
                    await asyncio.sleep(delay)
        raise last or RuntimeError("LLM: попытки исчерпаны")  # type: ignore[misc]


class ResilientClient:
    """Обёртка ``AsyncOpenAI`` для всего сервиса: ``chat.completions.create`` устойчив к перегрузке шлюза,
    встроенные повторы SDK (через ~0,5–1 с) отключены. Остальные атрибуты — от исходного клиента.

    Args:
        raw: ``AsyncOpenAI``.
        retries: Попыток на запрос при 5xx / 404 перезапуска модели / таймауте (``None`` — ``PROXY_RETRIES``,
            по умолчанию 2). Ночной риск-мониторинг ждёт дольше (``risk_monitoring.settings.LLM_PROXY_RETRIES``, 6): перезапуск
            модели длится минуты, и с двумя попытками страницы OCR и блоки извлечения терялись.
    """

    def __init__(self, raw: Any, retries: Optional[int] = None):
        """Отключает повторы SDK и подменяет ``chat.completions``."""
        raw = getattr(raw, "raw_client", raw)
        self.raw_client = raw.with_options(max_retries=0) if hasattr(raw, "with_options") else raw
        self.retries = retries
        self.chat = type("Chat", (), {})()
        self.chat.completions = _Completions(self.raw_client, retries)

    def with_options(self, **kwargs: Any) -> "ResilientClient":
        """Копия с другими параметрами SDK (повторы SDK остаются выключенными)."""
        kwargs["max_retries"] = 0
        return ResilientClient(self.raw_client.with_options(**kwargs), self.retries)

    def __getattr__(self, name: str) -> Any:
        """Прочие атрибуты (``base_url``, ``models`` …) — от исходного клиента."""
        return getattr(self.raw_client, name)


def resilient(client: Any) -> Any:
    """Оборачивает клиент в ``ResilientClient`` (если ``RESILIENT``)."""
    if isinstance(client, ResilientClient) or not RESILIENT:
        return client
    return ResilientClient(client)
