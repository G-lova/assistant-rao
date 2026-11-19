"""Повторный разбор фактов контрольных проверок без модели (18.11.2026).

Зачем. Факты проверок (``ai_analysis.facts``: номер, список закупок, ИНН заказчиков, результат, предписание)
строятся при анализе XML один раз. Исправления разбора (``xml_events.check_facts``) не меняют факты уже
загруженных документов — их нужно пересобрать. Пересборка не требует модели и не трогает остальной анализ:
``risk_monitoring.control_checks`` читает только ``ai_analysis.facts`` этих XML.

Что делает. Для документов ``checkResult`` / ``unplannedCheck`` / ``unplannedCheckCancel`` / ``eventResult`` за
период: читает XML из архива ЕИС, заново строит факты (:func:`xml_events.extract_facts`) и перезаписывает только
ветку ``facts`` в сохранённом ``ai_analysis``, оставляя остальное без изменений. Идемпотентно: повторный запуск
даёт тот же результат. Модель и эмбеддинги не вызываются.

Запуск — через эндпоинт ``POST /risk-monitoring/checks/reparse`` или задачу ``rm_reparse_checks_task`` (окружение —
как у остального API, заголовком ``X-API-Database``), поэтому выкладка через CI/CD достаточна: отдельный скрипт в
контейнере прода не нужен.
"""
import json
from typing import Any, Dict, Optional

from risk_monitoring import progress, xml_events
from risk_monitoring.xml_event_analyzer import EXTERNAL_TABLE


def rebuilt_ai_analysis(current: Any, tag: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Пересобирает ветку ``facts`` в сохранённом ``ai_analysis`` по заново разобранному XML.

    Перезаписывается только ``facts``; все остальные ветки (``pipeline``, ``event``, ``risks`` …) сохраняются как
    есть. Если по XML фактов нет (например, извещение без закупок) — ветка ``facts`` удаляется. Возвращает ``None``,
    когда перезаписывать нечего (факты не изменились, либо их нет и в сохранённом анализе не было): лишнюю запись
    не делаем.

    Args:
        current: Текущий ``ai_analysis`` — словарь или строка JSON (или ``None`` / пусто).
        tag: Тег документа (:func:`xml_events.parse_xml`).
        body: Нормализованное тело XML.

    Returns:
        Optional[Dict[str, Any]]: Новый ``ai_analysis`` словарём или ``None``, если изменений не требуется.
    """
    if isinstance(current, str):
        try:
            ai = json.loads(current) if current else {}
        except (TypeError, ValueError):
            ai = {}
    else:
        ai = current if isinstance(current, dict) else {}
    facts = xml_events.extract_facts(tag, body)
    old = ai.get("facts") if isinstance(ai.get("facts"), dict) else {}
    if facts == old:
        return None  # ничего не изменилось
    if not facts and not old:
        return None
    new = dict(ai)
    if facts:
        new["facts"] = facts
    else:
        new.pop("facts", None)
    return new


async def reparse(analyzer: Any, since: str, *, push: bool = True) -> Dict[str, Any]:
    """Пересобирает факты проверок за период и записывает изменившиеся обратно.

    Переиспользует низкоуровневые средства :class:`xml_event_analyzer.XmlEventAnalyzer`: ``_query`` (чтение из
    внешней БД), ``_parse`` (чтение и разбор XML из архива) и ``risk_api.patch_ai_analysis`` (запись только
    ``ai_analysis``). Своего транспорта и SQL не заводит. Анализатор создавать с ``use_llm=False``.

    Args:
        analyzer: Экземпляр ``XmlEventAnalyzer``.
        since: Начало периода ``ГГГГ-ММ-ДД``.
        push: Записывать результат (``False`` — только посчитать, для проверки).

    Returns:
        Dict[str, Any]: ``scanned`` (просмотрено), ``updated`` (переписано), ``unchanged``, ``errors``.
    """
    # два позиционных биндинга без list-маркеров: префикс пути архива (CONCAT) и начало периода
    rows = await analyzer._query("checks_for_reparse.sql", head=[analyzer.storage_path, since])
    scanned = updated = unchanged = errors = 0
    progress.subject(f"пересборка фактов проверок с {since} ({len(rows)})")
    for r in rows:
        scanned += 1
        try:
            tag, _, body, _ = await analyzer._parse(r)
            new_ai = rebuilt_ai_analysis(r.get("ai_analysis"), tag, body)
            if new_ai is None:
                unchanged += 1
                continue
            if push:
                await analyzer.risk_api.patch_ai_analysis(EXTERNAL_TABLE, int(r["id"]), new_ai)
            updated += 1
        except Exception as e:  # noqa: BLE001 — один битый XML не должен останавливать пересборку
            errors += 1
            progress.note(f"XML {r.get('id')}: {e}")
    return {"scanned": scanned, "updated": updated, "unchanged": unchanged, "errors": errors}
