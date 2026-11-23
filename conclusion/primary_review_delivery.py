"""Доставка результата оценки первичных экспертов в портал РАО (вариант 2 ТЗ: сервис сам отправляет подсказку).

Портал запускает расчёт ``POST /evaluate-primary-opinions`` с ``send_to_external = true`` и ``request_id`` (UUID).
Сервис отвечает 202 сразу, считает в фоне и отправляет ``POST <портал>/api/expertise/set-hints-for-primary-experts``
с телом ``{"id": <число>, "request_id": "<uuid>", "data": <результат>}``. Приёмник подтверждает HTTP 200 и
``{"status": "success"}``. При 429, 5xx и обрыве связи тот же сохранённый результат отправляется повторно с тем же UUID
в пределах окна ``PRIMARY_REVIEW_DELIVERY_WINDOW``: повторный запуск модели изменил бы комментарий, и портал отклонил бы
повтор как отличающийся результат. Остальные коды (401/403/404/409/413/415/422) повторами не лечатся.
Ключ API в логи и в статус доставки не попадает.
"""
import asyncio
import json
import re
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, Optional

from configs import settings
from configs.config import Config
from configs.logger import get_logger

logger = get_logger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_STORED = 200                                  # сколько последних доставок помнить (статус и результат для повтора)
_DELIVERIES: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_TASKS: set = set()                                # ссылки на фоновые задачи: иначе их может собрать сборщик мусора


class DeliveryError(Exception):
    """Ошибка запуска доставки (неверный ``request_id`` и т. п.); ``http_status`` — код ответа сервиса."""

    def __init__(self, message: str, http_status: int = 422):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def validate_request_id(request_id: Optional[str]) -> str:
    """Проверяет, что ``request_id`` — UUID; возвращает его в нижнем регистре.

    Raises:
        DeliveryError: 422 — не задан или не UUID.
    """
    try:
        return str(uuid.UUID(str(request_id)))
    except (ValueError, AttributeError, TypeError):
        raise DeliveryError("При send_to_external = true нужен request_id — UUID, который создаёт портал") from None


def format_portal_time(value: Any) -> str:
    """Время версии итогового ЭЗ в формате ``YYYY-MM-DD HH:mm:ss`` («2026-11-20T14:05:00.123Z» → «2026-11-20 14:05:00»)."""
    text = str(value or "").strip().replace("T", " ")
    match = re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text)
    return match.group(0) if match else text


def build_payload(expertise_id: int, request_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Тело запроса к приёмнику: ``id`` числом, UUID и ``data`` — результат расчёта в формате ответа сервиса.

    Приводит типы по контракту: ``expert_id`` — число, ``categories`` — список, ``comment`` — строка; для числовой оценки
    (включая 0) ``weighted_percent`` — число (если его нет, берётся ``match_percent``); при ``suggested_range = null``
    оценка остаётся ``null``. ``summary_updated_at`` — время обновления выбранной формы итогового ЭЗ, а не время расчёта.
    """
    data = dict(result)
    data["expertise_id"] = int(expertise_id)
    data["summary_updated_at"] = format_portal_time(data.get("summary_updated_at"))
    reviews = []
    for review in data.get("reviews") or []:
        item = dict(review)
        item["expert_id"] = int(item["expert_id"])
        item["categories"] = list(item.get("categories") or [])
        item["comment"] = str(item.get("comment") or "")
        if item.get("suggested_range") is not None and not isinstance(item.get("weighted_percent"), (int, float)):
            item["weighted_percent"] = item.get("match_percent")
        reviews.append(item)
    data["reviews"] = reviews
    return {"id": int(expertise_id), "request_id": request_id, "data": data}


def receiver_config(environment: Optional[str]) -> Dict[str, Any]:
    """Адрес и заголовки приёмника для среды портала (``dev``/``stage``/``prod``); ключ — ``PRIMARY_REVIEW_API_KEY``."""
    env = (environment or Config.APP_ENV or "dev").lower()
    base = {"prod": Config.SYSTEM_URL_PROD, "production": Config.SYSTEM_URL_PROD, "stage": Config.SYSTEM_URL_STAGE,
            "staging": Config.SYSTEM_URL_STAGE, "neuro": Config.SYSTEM_URL_NEURO,
            "neuro_assistant_database": Config.SYSTEM_URL_NEURO}.get(env, Config.SYSTEM_URL_DEV)
    return {"url": f"{(base or '').rstrip('/')}{settings.PRIMARY_REVIEW_DELIVERY_PATH}",
            "headers": {"Content-Type": "application/json", "Accept": "application/json",
                        "X-API-Key": Config.PRIMARY_REVIEW_API_KEY or ""}}


def get_delivery(request_id: str) -> Optional[Dict[str, Any]]:
    """Статус доставки по ``request_id`` (без ключей и без тела результата); ``None`` — такого запуска нет."""
    state = _DELIVERIES.get(request_id)
    return {k: v for k, v in state.items() if k != "body"} if state else None


def _remember(request_id: str, **fields: Any) -> Dict[str, Any]:
    """Обновляет запись о доставке и ограничивает число хранимых записей."""
    state = _DELIVERIES.setdefault(request_id, {"request_id": request_id})
    state.update(fields)
    _DELIVERIES.move_to_end(request_id)
    while len(_DELIVERIES) > _MAX_STORED:
        _DELIVERIES.popitem(last=False)
    return state


async def _post_once(session: Any, cfg: Dict[str, Any], body: bytes) -> Dict[str, Any]:
    """Одна попытка доставки: ``{"status": HTTP-код|None, "ok": bool, "detail": str}`` (ошибки сети не бросаются)."""
    try:
        import aiohttp
        errors: tuple = (aiohttp.ClientError, asyncio.TimeoutError, OSError)
        timeout = aiohttp.ClientTimeout(total=settings.PRIMARY_REVIEW_DELIVERY_TIMEOUT)
    except (ImportError, AttributeError):   # нет aiohttp или он подменён заглушкой (тесты): сессия-заглушка таймаут не использует
        errors, timeout = (asyncio.TimeoutError, OSError), None
    try:
        async with session.post(cfg["url"], headers=cfg["headers"], data=body, timeout=timeout) as response:
            text = await response.text()
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None          # HTML от nginx и т. п.
            ok = response.status == 200 and isinstance(payload, dict) and payload.get("status") == "success"
            detail = "" if ok else (str(payload.get("detail"))[:300] if isinstance(payload, dict) and payload.get("detail")
                                    else f"ответ не JSON ({len(text)} байт)" if payload is None else "")
            return {"status": response.status, "ok": ok, "detail": detail}
    except errors as e:
        return {"status": None, "ok": False, "detail": type(e).__name__}


async def deliver(session: Any, request_id: str, body: bytes, environment: Optional[str], started: float) -> Dict[str, Any]:
    """Доставляет ``body`` в приёмник; при 429/5xx/обрыве повторяет тот же ``body`` с паузами до конца окна.

    Args:
        session: ``aiohttp.ClientSession`` (``HTTPClientManager.get_session()``).
        request_id: UUID запуска (для логов и статуса).
        body: Готовое тело (JSON в байтах): все повторы отправляют ровно его.
        environment: Среда портала.
        started: ``time.monotonic()`` момента запуска расчёта — от него считается окно.

    Returns:
        dict: Статус доставки: ``state`` (``delivered``/``failed``), ``attempts``, ``http_status``, ``detail``.
    """
    cfg = receiver_config(environment)
    deadline = started + settings.PRIMARY_REVIEW_DELIVERY_WINDOW
    delays = list(settings.PRIMARY_REVIEW_DELIVERY_RETRY_DELAYS)
    attempt = 0
    while True:
        attempt += 1
        outcome = await _post_once(session, cfg, body)
        logger.info(f"Доставка оценки первичных {request_id}: попытка {attempt}, HTTP {outcome['status']}")
        if outcome["ok"]:
            return _remember(request_id, state="delivered", attempts=attempt, http_status=outcome["status"], detail="")
        retry = outcome["status"] is None or outcome["status"] in RETRY_STATUSES
        if not retry or not delays or time.monotonic() + delays[0] >= deadline:
            return _remember(request_id, state="failed", attempts=attempt, http_status=outcome["status"],
                             detail=outcome["detail"])
        await asyncio.sleep(delays.pop(0))


async def _safe_deliver(session: Any, request_id: str, body: bytes, environment: Optional[str]) -> None:
    """Повторная доставка сохранённого тела в фоне: непредвиденная ошибка попадает в статус, а не теряется."""
    try:
        await deliver(session, request_id, body, environment, time.monotonic())
    except Exception as e:  # noqa: BLE001
        logger.error(f"Повторная доставка {request_id} не выполнена: {type(e).__name__}", exc_info=True)
        _remember(request_id, state="failed", detail=(str(e) or type(e).__name__)[:300])


async def run_and_deliver(compute: Any, session: Any, expertise_id: int, request_id: str, environment: Optional[str],
                          started: float) -> None:
    """Фоновая задача: считает результат (``await compute()``), собирает тело и доставляет его.

    Ошибки расчёта и доставки не бросаются, а попадают в статус (``GET /evaluate-primary-opinions/{request_id}``).
    """
    try:
        result = await compute()
        body = json.dumps(build_payload(expertise_id, request_id, result), ensure_ascii=False).encode("utf-8")
        if len(body) > settings.PRIMARY_REVIEW_DELIVERY_MAX_BYTES:
            _remember(request_id, state="failed", detail=f"тело {len(body)} байт больше предела приёмника")
            return
        _remember(request_id, state="delivering", body=body)
        await deliver(session, request_id, body, environment, started)
    except Exception as e:  # noqa: BLE001 — задача фоновая: причину отдаём в статус, текст ошибки усекаем
        logger.error(f"Доставка оценки первичных {request_id} не выполнена: {type(e).__name__}", exc_info=True)
        _remember(request_id, state="failed", detail=(getattr(e, "message", None) or str(e) or type(e).__name__)[:300])


def start(compute: Any, session: Any, expertise_id: int, request_id: str, environment: Optional[str]) -> Dict[str, Any]:
    """Запускает расчёт и доставку в фоне и возвращает ответ запуска (HTTP 202).

    Повторный запуск с тем же ``request_id``, пока расчёт идёт или результат доставлен, ничего не пересчитывает.
    """
    request_id = validate_request_id(request_id)
    state = _DELIVERIES.get(request_id)
    if state and state.get("state") != "failed":
        return {"status": "accepted", "request_id": request_id, "state": state["state"]}
    if state and state.get("body"):       # расчёт был, не доставилось: повторяем сохранённый результат, а не считаем заново
        _remember(request_id, state="delivering", attempts=0, http_status=None, detail="")
        task = asyncio.create_task(_safe_deliver(session, request_id, state["body"], environment))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        return {"status": "accepted", "request_id": request_id, "state": "delivering"}
    _remember(request_id, state="calculating", expertise_id=int(expertise_id), attempts=0, http_status=None, detail="")
    task = asyncio.create_task(run_and_deliver(compute, session, expertise_id, request_id, environment, time.monotonic()))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return {"status": "accepted", "request_id": request_id, "state": "calculating"}
