"""AI-паспорт документа (концепция, гл. 6.4.6.10, 18.4–18.6, 18.12): заключение, назначение,
структура, ключевые условия, тематика, технические объекты, итоговая оценка и рекомендации.

Один запрос к LLM на документ: начало документа (до ``MAX_CHARS``) + оглавление, найденное кодом
по всему тексту, + краткое содержание из ``DataExtractor``. Длинные документы целиком не передаются —
для рисков по всему тексту есть ``DocumentRiskAnalyzer``.
"""
import json
import re
from typing import Any, Dict, List, Optional

from configs.logger import get_logger

logger = get_logger(__name__)

MAX_CHARS = 14000
HEADING_RE = re.compile(
    r"^\s*(?:(?:раздел|глава|часть|приложение)\s*(?:№\s*)?[\dIVXА-Я]+[.)]?\s+.{3,120}|\d{1,2}(?:\.\d{1,2})?\.?\s+[А-ЯЁA-Z][^\n]{3,120})\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def find_headings(text: str, limit: int = 40) -> List[str]:
    """Заголовки разделов по всему тексту (нумерованные строки и «Раздел / Глава / Приложение …»).

    Args:
        text: Текст документа.
        limit: Максимум заголовков.

    Returns:
        List[str]: Заголовки в порядке следования, без повторов.
    """
    seen, out = set(), []
    for m in HEADING_RE.finditer(text or ""):
        h = re.sub(r"\s+", " ", m.group(0)).strip()
        if len(h) > 140 or h.lower() in seen:
            continue
        seen.add(h.lower())
        out.append(h)
        if len(out) >= limit:
            break
    return out


class DocumentProfiler:
    """Формирует AI-паспорт документа через LLM.

    Args:
        llm_client: OpenAI-совместимый асинхронный клиент.
        model: Модель.
    """

    PROMPT_PATH = "prompts/document_profile_prompt.txt"
    SCHEMA_PATH = "schemas/document_profile_schema.json"

    def __init__(self, llm_client: Any, model: str):
        """Загружает промпт и схему, считает версию промпта."""
        from risk_monitoring.llm_json import LLMJson, prompt_version

        with open(self.PROMPT_PATH, encoding="utf-8") as f:
            self.prompt = f.read()
        with open(self.SCHEMA_PATH, encoding="utf-8") as f:
            self.schema = json.load(f)
        self.prompt_version = prompt_version(self.prompt, self.schema)
        self.llm = LLMJson(llm_client, model, rate=1.0, concurrency=3)

    async def profile(self, text: str, file_name: str, doc_type: Optional[str], summary: Optional[str],
                      purchase: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Строит паспорт документа.

        Args:
            text: Текст документа.
            file_name: Имя файла.
            doc_type: Название типа документа.
            summary: Краткое содержание из ``DataExtractor``.
            purchase: Паспорт закупки (контекст).

        Returns:
            Optional[Dict[str, Any]]: Ответ модели по схеме ``document_profile_schema.json``
            или ``None``, если модель не ответила.
        """
        head = (text or "")[:MAX_CHARS]
        truncated = len(text or "") > MAX_CHARS
        user = (
            f"Документ: {file_name}\nТип документа: {doc_type or 'не определён'}\n"
            f"Закупка: {json.dumps(purchase or {}, ensure_ascii=False, default=str)[:1500]}\n"
            f"Краткое содержание (из извлечения): {summary or 'нет'}\n"
            f"Заголовки разделов по всему документу: {json.dumps(find_headings(text), ensure_ascii=False)}\n"
            f"Текст{' (начало документа, полный текст длиннее)' if truncated else ''}:\n\n{head}"
        )
        try:
            return await self.llm.ask(self.prompt, user, self.schema, max_tokens=3000)
        except Exception as e:  # noqa: BLE001 — паспорт без профиля всё равно записывается
            logger.error(f"DocumentProfiler: {file_name}: {type(e).__name__}: {e}")
            return None
