SYSTEM_PROMPT = """
Ты — эксперт по проверке документов закупок. Проанализируй документ и ответь в JSON.
Проверь:
1. **ТИП ДОКУМЕНТА**: соответствует ли содержимое типу (например, «Извещение», «Проект контракта»)?
2. **ЯЗЫК**: основной язык — русский?
3. **ЧИТАЕМОСТЬ**: есть ли страницы с размытым текстом, водяными знаками, низким качеством?
4. **ПРОБЛЕМНЫЕ ФРАГМЕНТЫ**: укажи, где текст нечитаем (например, «страница 5 — размыто», «фрагмент 3 — OCR не распознал»).
5. **СТРУКТУРА**: содержит ли документ обязательные разделы?

Учти контекст похожих документов:
{similar_docs}

Формат вывода: только чистый JSON.
"""


RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "type_compliance": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["соответствует", "не соответствует"]},
                "issues": {"type": "array", "items": {"type": "string"}},
                "recommendations": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["status", "issues", "recommendations"]
        },
        "readability": {
            "type": "object",
            "properties": {
                "is_readable": {"type": "boolean"},
                "language": {"type": "string", "enum": ["русский", "английский", "смешанный"]},
                "issues": {"type": "array", "items": {"type": "string"}},
                "problematic_fragments": {
                    "type": "array",
                    "items": {"type": "string"}
                }
            },
            "required": ["is_readable", "language", "issues", "problematic_fragments"]
        }
    },
    "required": ["type_compliance", "readability"]
}