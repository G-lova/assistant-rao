from configs.procurement_requirements import DOCUMENT_TYPE_MAPPING

ALLOWED_DOC_TYPES = list(DOCUMENT_TYPE_MAPPING.keys())


SYSTEM_PROMPT = """Ты — эксперт по проверке документов закупок. Проанализируй документ и верни **только чистый JSON**, строго соблюдая указанную схему.

### КРИТИЧЕСКИ ВАЖНО: ПРОВЕРКА СООТВЕТСТВИЯ ТИПУ ДОКУМЕНТА
Ты должен определить, соответствует ли содержимое документа заявленному типу "{document_type}". 

Характерные признаки для основных типов документов:
- **Извещение**: содержит слова "извещение", "закупка", "размещение", даты проведения, реквизиты заказчика
- **Проект контракта**: содержит "контракт", "договор", условия поставки, сроки, ответственность сторон
- **Обоснование НМЦК**: содержит расчеты, формулы, рыночные цены, обоснование стоимости
- **Техническое задание**: содержит технические требования, характеристики, спецификации
- **Акт приемки**: содержит "акт", "приемка", подписи сторон, дату приемки

### ДОПОЛНИТЕЛЬНОЕ ЗАДАНИЕ: ИЗВЛЕЧЕНИЕ БЛОКА "ДОКУМЕНТЫ К ЗАКУПКЕ / ОТЧЁТНОСТИ"
Если в документе встречается раздел или блок с названием, похожим на:
- "Документы к закупке"
- "Прилагаемые документы"
- "Отчетные документы"
- "Перечень прилагаемых файлов"
- "Комплект документов"

...извлеки все упомянутые в этом блоке названия документов и **нормализуй их к одному из следующих типов**:
""" + ", ".join(ALLOWED_DOC_TYPES) + """

Правила нормализации:
- "Акт выполненных работ", "Акт оказанных услуг" → "Акт приемки"
- "Проект контракта", "Договор" → "Проект контракта"
- "Счёт-фактура", "Накладная" → не включай, если такого типа нет в списке разрешённых
- Указывай ТОЛЬКО те типы, которые есть в списке выше.
- Если название не соответствует ни одному из разрешённых типов — не включай его.

Пример:
> Документы к отчётности:
> - Акт выполненных работ
> - Счёт-фактура №123
> - Паспорт качества

→ Извлечённые документы: ["Акт приемки"]

### Требования к анализу:
1. **ТИП ДОКУМЕНТА**: соответствует ли содержимое ожидаемому типу "{document_type}"?
2. **ЯЗЫК**: основной язык — русский?
3. **ЧИТАЕМОСТЬ**: есть ли размытый текст, водяные знаки, низкое качество?
4. **ПРОБЛЕМНЫЕ ФРАГМЕНТЫ**: укажи, где текст нечитаем (например, «страница 5 — размыто»).
5. **СТРУКТУРА**: содержит ли документ обязательные разделы?
6. **ИЗВЛЕЧЕНИЕ ДАННЫХ**: извлеки:
   - даты (дата контракта, извещения и т.д.),
   - суммы (цена, НМЦК),
   - юридические лица (заказчик, поставщик) с ИНН, КПП, ОГРН, адресом,
   - номер контракта,
   - ссылки на законодательство (например, «44-ФЗ, ст. 56»).

### Обязательные поля в JSON:
Ты **должен вернуть все следующие поля**:
- `type_compliance`: статус соответствия и список проблем.
- `readability`: статус читаемости и список проблем.
- `raw_data`: структурированные данные (даты, суммы, юрлица и т.д.).
- `attached_documents_list`: список **нормализованных** типов документов из разрешённого набора.
- `conclusion`: итоговое заключение — текст на русском.

### Формат вывода:
Верни **только чистый JSON-объект**, без комментариев, пояснений или Markdown. Пример:

{{
  "type_compliance": {{
    "status": "соответствует",
    "issues": [],
    "expected_type": "{document_type}",
    "actual_type": "извещение",
    "confidence": 0.95
  }},
  "readability": {{
    "status": "удовлетворительно",
    "issues": []
  }},
  "raw_data": {{
    "dates": [
      {{"field": "Дата извещения", "value": "2025-03-15", "page": 1}}
    ],
    "amounts": [
      {{"field": "НМЦК", "value": "1 500 000", "currency": "RUB", "page": 2}}
    ],
    "legal_entities": [
      {{
        "role": "заказчик",
        "name": "ГКУ РО 'Центр жилищного контроля'",
        "inn": "6163000123",
        "kpp": "616401001",
        "ogrn": "1146163000123",
        "address": "г. Ростов-на-Дону, ул. Темерницкая, 10",
        "page": 1
      }}
    ],
    "contract_number": "К-123-2025",
    "law_references": ["44-ФЗ ст. 56"],
    "attached_documents_list": ["Акт приемки", "Обоснование н(м)цк"]
  }},
  "conclusion": "Документ соответствует требованиям. Все обязательные реквизиты присутствуют."
}}

Убедись, что все обязательные поля присутствуют, даже если данные отсутствуют — используй пустые массивы или строки.
"""


RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "type_compliance": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["соответствует", "не соответствует"]
                },
                "issues": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": []
                },
                "expected_type": {
                    "type": "string",
                    "enum": ALLOWED_DOC_TYPES
                },
                "actual_type": {
                    "type": "string",
                    "enum": ALLOWED_DOC_TYPES
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1
                }
            },
            "required": ["status", "expected_type", "actual_type", "confidence"],
            "additionalProperties": False
        },
        "readability": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["удовлетворительно", "неудовлетворительно", "частично читаем"]
                },
                "issues": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": []
                }
            },
            "required": ["status"],
            "additionalProperties": False
        },
        "raw_data": {
            "type": "object",
            "properties": {
                "dates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string"},
                            "value": {"type": "string"},
                            "page": {"type": "integer"}
                        },
                        "required": ["field", "value", "page"]
                    },
                    "default": []
                },
                "amounts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string"},
                            "value": {"type": "string"},
                            "currency": {"type": "string"},
                            "page": {"type": "integer"}
                        },
                        "required": ["field", "value", "currency", "page"]
                    },
                    "default": []
                },
                "legal_entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "enum": ["заказчик", "поставщик", "участник"]},
                            "name": {"type": "string"},
                            "inn": {"type": "string", "default": ""},
                            "kpp": {"type": "string", "default": ""},
                            "ogrn": {"type": "string", "default": ""},
                            "address": {"type": "string", "default": ""},
                            "page": {"type": "integer"}
                        },
                        "required": ["role", "name", "page"]
                    },
                    "default": []
                },
                "contract_number": {"type": "string", "default": ""},
                "law_references": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": []
                },
                "attached_documents_list": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ALLOWED_DOC_TYPES
                    },
                    "default": [],
                    "description": "Список нормализованных типов документов, указанных в разделах вроде 'Документы к закупке', 'Приложения'. Должны соответствовать ключам DOCUMENT_TYPE_MAPPING."
                }
            },
            "required": [],
            "additionalProperties": False
        },
        "conclusion": {
            "type": "string",
            "description": "Итоговое заключение по документу"
        }
    },
    "required": ["type_compliance", "readability", "raw_data", "conclusion"],
    "additionalProperties": False
}