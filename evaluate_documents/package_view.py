"""Чистые вспомогательные функции представления комплекта документов в ``/evaluate-documents``.

Вынесены из пайплайна, чтобы логику можно было тестировать без LLM, сети и БД.
"""
from typing import Any, Dict, List

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
