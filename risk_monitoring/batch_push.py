"""Запись пачки пересчитанных результатов риск-мониторинга: во внешнюю БД (PATCH ai_analysis) и к нам.

Используется массовыми пересчётами без модели — затухание рисков (``rm_decay_task``) и решения аналитиков
(``rm_decisions_task``). Раньше закупки обрабатывались строго по одной: PATCH, затем INSERT в ``ai_rm_results``,
затем следующая. Теперь:

* PATCH идут с ограничением одновременности ``RM_PUSH_CONCURRENCY``. Частоту всё равно держит лимитер
  ``RiskMonitoringAPI`` (у API 60 запросов в минуту с IP, ``rate = 0.9``/с): одновременность лишь не даёт
  простаивать, пока ждём ответа, так что скорость растёт до этого лимита, но не выше;
* результаты пачки сохраняются у нас одним ``executemany`` (:meth:`configs.ai_repo.AIRepo.save_rm_results`).

Ошибка PATCH одной закупки не останавливает остальные: результат сохраняется с ``pushed = False`` и текстом
ошибки и дозаписывается позже, как и раньше.
"""
import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from configs.logger import get_logger

logger = get_logger(__name__)

RM_PUSH_CONCURRENCY_DEFAULT = 3


def push_concurrency() -> int:
    """Одновременных PATCH во внешнюю БД (``configs.settings.RM_PUSH_CONCURRENCY``, по умолчанию 3)."""
    try:
        from configs import settings
    except Exception:  # noqa: BLE001 — модуль используется и без настроек
        return RM_PUSH_CONCURRENCY_DEFAULT
    return max(1, int(getattr(settings, "RM_PUSH_CONCURRENCY", RM_PUSH_CONCURRENCY_DEFAULT) or 1))


async def push_and_save(repo: Any, patch: Optional[Callable[[str, int, Dict[str, Any]], Awaitable[Any]]],
                        env: str, kind: str, table: str, items: Sequence[Tuple[int, Dict[str, Any]]],
                        push: bool, out: Dict[str, int], concurrency: Optional[int] = None) -> None:
    """Записывает пачку результатов во внешнюю БД (если ``push``) и сохраняет её у нас одним запросом.

    Args:
        repo: ``AIRepo`` (``save_rm_results``; без него — ``save_rm_result`` по одному).
        patch: ``RiskMonitoringAPI.patch_ai_analysis`` (``table, id, ai_analysis``); ``None`` — не записывать.
        env: Окружение (``ai_rm_results.env``).
        kind: Вид результата (``contract``).
        table: Таблица внешней БД.
        items: ``(external_id, ai_analysis)``.
        push: Записывать во внешнюю БД.
        out: Счётчики задачи (``updated``, ``pushed``, ``errors``) — увеличиваются на месте.
        concurrency: Одновременных PATCH (по умолчанию :func:`push_concurrency`).
    """
    if not items:
        return
    out["updated"] = out.get("updated", 0) + len(items)
    sem = asyncio.Semaphore(concurrency or push_concurrency())

    async def one(external_id: int, ai: Dict[str, Any]) -> Tuple[int, Dict[str, Any], bool, Optional[str]]:
        """PATCH одной закупки; ошибка — в результат, а не исключением."""
        if not push or patch is None:
            return external_id, ai, False, None
        async with sem:
            try:
                await patch(table, external_id, ai)
                return external_id, ai, True, None
            except Exception as e:  # noqa: BLE001 — сохраним у себя, дозапишется позже
                return external_id, ai, False, str(e)

    results: List[Tuple[int, Dict[str, Any], bool, Optional[str]]] = list(
        await asyncio.gather(*(one(eid, ai) for eid, ai in items)))
    out["pushed"] = out.get("pushed", 0) + sum(1 for r in results if r[2])
    out["errors"] = out.get("errors", 0) + sum(1 for r in results if r[3] is not None)

    if hasattr(repo, "save_rm_results"):
        await repo.save_rm_results(env, kind, results)
    else:  # хранилище без пакетной записи (заглушки, старые реализации)
        for eid, ai, pushed, err in results:
            await repo.save_rm_result(env, kind, eid, ai, pushed, err)
