"""Отправка черновика сводного ЭЗ в основную БД (этап 5.7, по умолчанию выключена).

Адрес и заголовки берутся из ``Config.get_external_api_config`` (эндпоинт ``set-hint-for-rao-expert``),
отдельный сервисный класс не используется. Ключ API в логи не пишется.
"""
from typing import Any, Dict, Optional

from configs.config import Config
from configs.logger import get_logger

logger = get_logger(__name__)


async def send_draft(http_manager: Any, expertise_id: int, data: Dict[str, Any],
                     environment: Optional[str] = None) -> Dict[str, Any]:
    """Отправляет ``data`` сводного ЭЗ как черновик заключения.

    Тело запроса: ``{"id": expertise_id, "data": {...}}`` — те же ключи формы, что в
    ``expertise_expert_opinion7s.data``. Ошибка отправки не бросается: она возвращается в результате,
    чтобы не терять собранное заключение.

    Args:
        http_manager: ``HTTPClientManager`` (``get_session()`` возвращает ``aiohttp``-сессию).
        expertise_id: ID экспертизы.
        data: JSON заключения ровно с ключами формы.
        environment: Окружение (``dev``/``stage``/``prod``); ``None`` — ``APP_ENV``.

    Returns:
        dict: ``{"ok": True, "status": код}`` либо ``{"ok": False, "error": описание}``.
    """
    cfg = Config.get_external_api_config(environment)
    payload = {"id": int(expertise_id), "data": data}
    try:
        session = http_manager.get_session()
        async with session.post(cfg["url"], headers=cfg["headers"], json=payload) as response:
            status = response.status
            try:
                body = await response.json(content_type=None)
            except Exception:  # noqa: BLE001 — тело ответа может быть не JSON
                body = None
        if status not in (200, 201):
            detail = (body.get("error") or body.get("message")) if isinstance(body, dict) else None
            logger.error(f"knowledge_store: черновик ЭЗ {expertise_id} не принят: HTTP {status}")
            return {"ok": False, "status": status, "error": detail or f"HTTP {status}"}
        logger.info(f"knowledge_store: черновик ЭЗ {expertise_id} отправлен в основную БД")
        return {"ok": True, "status": status}
    except Exception as e:  # noqa: BLE001 — сеть/сессия
        logger.error(f"knowledge_store: отправка черновика ЭЗ {expertise_id} не удалась: {type(e).__name__}")
        return {"ok": False, "error": type(e).__name__}
