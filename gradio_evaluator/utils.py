import os
import json
import logging
from typing import List, Dict

from procurement_requirements import PROCUREMENT_REQUIREMENTS, DOCUMENT_CODE_TO_LABEL


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_required_documents(legislation: str, procurement_method: str, expertise_details: str) -> List[str]:
    """
    Возвращает список обязательных документов для заданной комбинации законодательства, способа закупки и типа экспертизы.

    Извлекает требования из иерархической структуры PROCUREMENT_REQUIREMENTS:
    сначала по типу законодательства (например, "44-ФЗ"), затем по способу закупки
    (например, "Электронный аукцион"), и, наконец, по описанию комплекта документов
    (например, "Полный комплект документов о закупке"). Если комбинация не найдена,
    возвращается пустой список.

    Args:
        legislation (str): Тип законодательства (например, "44-ФЗ", "223-ФЗ").
        procurement_method (str): Способ закупки (например, "Конкурс", "Запрос котировок").
        expertise_details (str): Описание комплекта документов для экспертизы.

    Returns:
        List[str]: Список наименований обязательных документов. Может быть пустым,
            если требуемая конфигурация не определена в PROCUREMENT_REQUIREMENTS.
    """
    return (
        PROCUREMENT_REQUIREMENTS
        .get(legislation, {})
        .get(procurement_method, {})
        .get(expertise_details, [])
    )


def get_document_code_by_label(label: str) -> str:
    """
    Возвращает код документа по его человекочитаемой метке.

    Использует обратное отображение из глобального словаря DOCUMENT_CODE_TO_LABEL,
    где ключи — коды (например, "IZV"), а значения — метки (например, "Извещение").
    Если метка не найдена, возвращает "unknown".

    Args:
        label (str): Человекочитаемое наименование типа документа.

    Returns:
        str: Код документа (например, "IZV", "DOK") или "unknown", если метка не распознана.
    """
    reverse_map = {v: k for k, v in DOCUMENT_CODE_TO_LABEL.items()}
    return reverse_map.get(label, "unknown")


def format_final_response(response: Dict) -> str:
    """
    Форматирует структурированный результат ИИ-анализа закупки в человекочитаемый Markdown-отчёт.

    Преобразует словарь с итоговым заключением (включая общий статус, сводку и детали по каждому
    документу) в текстовый отчёт с эмодзи и разметкой для удобного отображения в интерфейсах
    (например, в Gradio или телеграм-боте). При наличии ошибки возвращает краткое сообщение об ошибке.

    Args:
        response (Dict): Словарь с результатом анализа, содержащий поля:
            - procurement_id, overall_status, overall_summary,
            - documents (с вложенными readability, type_compliance, completeness).

    Returns:
        str: Отформатированная строка в стиле Markdown с итоговым отчётом по закупке.
    """
    if "error" in response:
        return f"**Ошибка**: {response['error']}"

    lines = []
    lines.append(f"## 📋 Итоговый анализ закупки `{response.get('procurement_id', 'N/A')}`")
    lines.append("")
    
    overall_status = response.get("overall_status", "deny")
    status_emoji = "✅" if overall_status == "allow" else "❌"
    lines.append(f"### {status_emoji} Общий статус: **{'Соответствует' if overall_status == 'allow' else 'Не соответствует'}**")
    lines.append(f"**Итог**: {response.get('overall_summary', 'Без комментария')}")
    lines.append("")

    lines.append("### 📄 Детали по документам")
    for doc in response.get("documents", []):
        doc_status = "✅" if doc["status"] == "allow" else "❌"
        doc_label = doc.get("document_label", doc.get("document_type", "N/A"))
        lines.append(f"**{doc_label}** {doc_status}")

        # Читаемость
        rd = doc["readability"]
        lines.append(f"  - **Читаемость**: {'✅' if rd['status'] == 'allow' else '❌'} {rd['description']}")

        # Соответствие типу
        tc = doc["type_compliance"]
        lines.append(f"  - **Тип**: {'✅' if tc['status'] == 'allow' else '❌'} {tc['description']}")

        # Полнота
        comp = doc["completeness"]
        issues = comp.get("description", [])
        comp_status = "✅" if comp["status"] == "allow" else "❌"
        if issues:
            lines.append(f"  - **Полнота**: {comp_status} Проблемы: " + "; ".join(issues))
        else:
            lines.append(f"  - **Полнота**: {comp_status} Без замечаний")
        lines.append("")

    return "\n".join(lines)