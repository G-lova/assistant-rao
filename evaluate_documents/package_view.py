"""Чистые вспомогательные функции представления комплекта документов в ``/evaluate-documents``.

Вынесены из пайплайна, чтобы логику можно было тестировать без LLM, сети и БД.
"""
from typing import Any, Dict, List, Optional

# Ключи общего контекста, которые НЕ должны попадать в проверку полноты отдельного типа документов:
# отсутствие документов других типов и факты по комплекту влияют только на итоговые overall_*.
PACKAGE_LEVEL_KEYS = ("missed_documents", "documents", "facts")


def completeness_input(base: Dict[str, Any], doc_code: str, doc_type: str, documents: List[dict]) -> Dict[str, Any]:
    """Формирует вход проверки полноты для одного типа документов.

    Берётся контекст экспертизы (параметры, закон и т.п.), но без сведений об отсутствующих документах
    других типов и без фактов по комплекту: полнота документов данного типа оценивается только по ним.

    Args:
        base: Общий контекст (``data_for_final_evaluation``).
        doc_code: Код типа документа.
        doc_type: Название типа документа.
        documents: Результаты анализа файлов этого типа.

    Returns:
        dict: Вход для ``CompletenessChecker.check_doc_completeness``.
    """
    context = {k: v for k, v in base.items() if k not in PACKAGE_LEVEL_KEYS}
    return {**context, "doc_code": doc_code, "doc_type": doc_type, "documents": documents}


def eis_link_assessment(documents: List[dict]) -> Optional[Dict[str, Any]]:
    """Оценка ссылки на ЕИС без модели, если документы закупки из ЕИС получить не удалось.

    Раньше модель полноты по такой ссылке писала три «критические ошибки» (тип, читаемость, полнота) из одного
    технического сбоя. Теперь — одна формулировка: ссылка указана, содержимое ЕИС не проверено.

    Args:
        documents: Результаты анализа ссылок типа ``linkDocs``.

    Returns:
        dict | None: Результат в формате ``CompletenessChecker`` (``type_compliance``, ``readability``,
        ``completeness``, ``raw_data``) или ``None``, если ссылка доступна (тогда оценивает модель).
    """
    raws = [d.get("raw_data") for d in documents if isinstance(d, dict) and isinstance(d.get("raw_data"), dict)]
    failed = [r for r in raws if r.get("eis_status") and r.get("eis_status") != "available"]
    if not raws or len(failed) != len(raws):
        return None
    reason = "; ".join(sorted({str(r.get("eis_error") or r.get("eis_status")) for r in failed}))
    numbers = sorted({str(r["eis_procurement_number"]) for r in raws if r.get("eis_procurement_number")})
    link = f" (закупка № {', '.join(numbers)})" if numbers else ""
    not_checked = (f"Документы закупки из ЕИС получить не удалось: {reason}. Соответствие комплекта данным ЕИС "
                   f"не проверено; документы закрытых закупок в ЕИС могут не публиковаться.")
    return {
        "type_compliance": {"status": "allow", "description": f"Указана ссылка на закупку в ЕИС{link}."},
        "readability": {"status": "allow", "description": "Не оценивалась: документы закупки из ЕИС не получены."},
        "completeness": {"status": "deny", "description": not_checked},
        "raw_data": raws,
    }
