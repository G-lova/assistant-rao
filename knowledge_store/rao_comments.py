"""Черновик комментариев РАО (``data["rao_comment"]``) к заключениям первичных экспертов.

В заключении сводного эксперта (роль ``raoExpert``) ``rao_comment`` — словарь ``{"<поле>-<expert_id>": "текст"}``:
комментарий к пункту заключения первичного эксперта с этим ``expert_id`` (``users.id``), у которого обнаружено
расхождение с итоговым заключением или ненадлежащее заключение по пункту. Модуль строит такой словарь по результату
:func:`conclusion.primary_review_pipeline.review_expertise` — шаблонными фразами, без модели; сводный эксперт
правит текст сам.

Комментарии включаются по запросу (``with_rao_comments`` в ``POST /generate-summary-opinion``): сводное ЭЗ собирается вместе
с ними, а без них то же заключение служит подсказкой для заполнения первичного ЭЗ.
"""
from typing import Any, Dict, List

# Текст комментария по виду расхождения (см. ``primary_compare.Difference.kind``)
TEXT_BY_KIND = {
    "missed": "Нарушение по пункту не выявлено, хотя в итоговом заключении оно отмечено.",
    "false_alarm": "Указано нарушение, которое не подтверждается итоговым заключением.",
    "other": "Оценка по пункту расходится с итоговым заключением.",
}


def _sentence(text: str) -> str:
    """Текст замечания с заглавной буквы и точкой в конце."""
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


def from_reviews(reviews: List[Dict[str, Any]]) -> Dict[str, str]:
    """Собирает ``rao_comment`` по результатам оценки первичных заключений.

    Комментарий создаётся к каждому пункту, по которому у первичного эксперта есть собственное расхождение
    (расхождения, общие для всех первичных экспертов, пропускаются: это, скорее, решение сводного эксперта) или замечание
    с привязкой к полю (``issues[].keys``: неверный факт, сумма неустойки и т. п.). Несколько замечаний к одному пункту
    склеиваются в один текст.

    Args:
        reviews: ``reviews`` из результата ``review_expertise``.

    Returns:
        dict: ``{"<поле>-<expert_id>": "текст"}``; пустой, если расхождений нет.
    """
    out: Dict[str, List[str]] = {}
    for review in reviews:
        expert_id = review["expert_id"]
        for diff in review.get("differences", []):
            if diff.get("shared"):
                continue
            text = TEXT_BY_KIND.get(diff["kind"], TEXT_BY_KIND["other"])
            out.setdefault(f"{diff['key']}-{expert_id}", []).append(text)
        for issue in review.get("issues", []):
            if issue.get("keys") and _sentence(issue.get("message")):
                out.setdefault(f"{issue['keys'][0]}-{expert_id}", []).append(_sentence(issue["message"]))
    return {key: " ".join(dict.fromkeys(parts)) for key, parts in out.items()}
