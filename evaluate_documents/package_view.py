"""Чистые вспомогательные функции представления комплекта документов в ``/evaluate-documents``.

Вынесены из пайплайна, чтобы логику можно было тестировать без LLM, сети и БД.
"""
from typing import Any, Dict, Iterable, List

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


def files_view(documents_results: Iterable[dict]) -> List[Dict[str, Any]]:
    """Список файлов типа документа с именем и ссылкой для итогового ответа.

    Args:
        documents_results: Результаты анализа файлов (``filename`` и ``url`` проставляет ``process_link``).

    Returns:
        list[dict]: По элементу на файл, в том же порядке, что и ``raw_data``:
        ``filename``, ``url``, ``detected_type``, ``readability`` (статус).
    """
    files = []
    for item in documents_results or []:
        if not isinstance(item, dict):
            continue
        files.append({
            "filename": item.get("filename"),
            "url": item.get("url"),
            "detected_type": (item.get("type_compliance") or {}).get("detected_type"),
            "readability": (item.get("readability") or {}).get("status"),
        })
    return files


def with_source(result: Any, filename: Any, url: Any) -> Any:
    """Проставляет ``filename`` и ``url`` в результат анализа файла (словарь или список словарей).

    Уже заполненные значения не затираются.

    Args:
        result: Результат ``process_parse_result`` (``dict`` или ``list[dict]``).
        filename: Имя файла.
        url: Ссылка на файл/источник.

    Returns:
        Any: Тот же объект (изменён на месте); значения других типов возвращаются как есть.
    """
    items = result if isinstance(result, list) else [result]
    for item in items:
        if isinstance(item, dict):
            item.setdefault("filename", filename)
            item.setdefault("url", url)
    return result
