"""Отправка черновика сводного ЭЗ в основную БД (этап 5.7, по умолчанию выключена).

Используется существующий :class:`conclusion.external_api_service.ExternalAPIService`: он берёт адрес
и заголовки из ``Config.get_external_api_config`` (эндпоинт ``set-hint-for-rao-expert``) и отправляет
тело ``{"id", "data"}``. Ключ API в результат и логи этого модуля не попадает.
"""
from typing import Any, Dict, Optional

from configs.logger import get_logger

logger = get_logger(__name__)


async def send_draft(http_manager: Any, expertise_id: int, data: Dict[str, Any],
                     environment: Optional[str] = None) -> Dict[str, Any]:
    """Отправляет ``data`` сводного ЭЗ как черновик заключения через ``ExternalAPIService``.

    Тело запроса: ``{"id": expertise_id, "data": {...}}`` — те же ключи формы, что в
    ``expertise_expert_opinion7s.data``. Ошибка отправки не бросается: она возвращается в результате,
    чтобы не терять собранное заключение.

    Args:
        http_manager: ``HTTPClientManager`` (``get_session()`` возвращает ``aiohttp``-сессию).
        expertise_id: ID экспертизы.
        data: JSON заключения ровно с ключами формы.
        environment: Окружение (``dev``/``stage``/``prod``); ``None`` — ``APP_ENV``.

    Returns:
        dict: ``{"ok": True}`` либо ``{"ok": False, "error": описание}`` (описание усечено до 300 символов).
    """
    try:
        from conclusion.external_api_service import ExternalAPIService  # ленивый импорт: тянет aiohttp/requests
        service = ExternalAPIService(http_manager, environment)
        await service.send_expertise_data(int(expertise_id), data)
        logger.info(f"knowledge_store: черновик ЭЗ {expertise_id} отправлен в основную БД")
        return {"ok": True}
    except Exception as e:  # noqa: BLE001 — сеть, формат ответа, ошибка API: заключение не теряем
        logger.error(f"knowledge_store: отправка черновика ЭЗ {expertise_id} не удалась: {type(e).__name__}")
        return {"ok": False, "error": str(e)[:300] or type(e).__name__}
