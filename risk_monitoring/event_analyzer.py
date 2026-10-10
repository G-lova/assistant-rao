"""LLM-анализ одного события ЕИС (XML-документа): суть, оценка изменений, значимость, риски.

Вход — компактный пакет, собранный кодом (``xml_events.build_llm_payload``): тип события,
ключевые сведения, изменения «было → стало», изменения приложенных файлов и их резюме.
Анализ комплекта документов закупки сюда не входит — он делается на уровне закупки.

Коды рисков берутся из единого каталога ``risk_monitoring/risk_catalog.py``.
"""
import json
from typing import Any, Dict

from configs.logger import get_logger
from risk_monitoring import risk_catalog
from risk_monitoring.llm_json import LLMJson, prompt_version

logger = get_logger(__name__)

PROMPT_PATH = "prompts/event_analyzer_prompt.txt"
SCHEMA_PATH = "schemas/event_analyzer_schema.json"
MAX_PAYLOAD_CHARS = 24000

# Совместимость: справочник кодов событийных рисков «код → название» (раньше был задан здесь)
EVENT_RISKS_KEYS = {code: risk_catalog.title(code) for code in risk_catalog.EVENT_RISK_CODES}


class EventAnalyzer:
    """Анализ события ЕИС с помощью LLM (guided JSON).

    Args:
        llm_client: OpenAI-совместимый асинхронный клиент.
        model: Модель.
    """

    def __init__(self, llm_client: Any, model: str):
        """Загружает промпт и схему, подставляет каталог кодов, считает версию промпта."""
        self.model = model
        self.llm = LLMJson(llm_client, model, rate=1.5, concurrency=3)
        with open(PROMPT_PATH, encoding="utf-8") as f:
            raw_prompt = f.read()
        with open(SCHEMA_PATH, encoding="utf-8") as f:
            raw_schema = f.read()
        codes = list(risk_catalog.EVENT_RISK_CODES)
        self.prompt = raw_prompt.replace("EVENT_RISKS_KEYS", risk_catalog.catalog_text(codes))
        self.schema = json.loads(raw_schema.replace('"EVENT_RISKS_KEYS"', json.dumps(codes)))
        self.prompt_version = prompt_version(self.prompt, self.schema)

    async def analyze(self, payload: Dict[str, Any], xml_name: str) -> Dict[str, Any]:
        """Анализирует событие.

        Args:
            payload: Пакет события (тип, сведения, изменения, документы).
            xml_name: Имя XML (для логов).

        Returns:
            Dict[str, Any]: Ответ модели с отфильтрованными рисками (только коды каталога,
            название риска — из каталога, если модель его не дала) и ``prompt_version``.
            При сбое модели — ``{"event_summary": None, "risks": [], "error": ...}``.
        """
        content = json.dumps(payload, ensure_ascii=False, default=str)
        if len(content) > MAX_PAYLOAD_CHARS:
            content = content[:MAX_PAYLOAD_CHARS] + " …[обрезано]"
        try:
            data = await self.llm.ask(self.prompt, f"Проанализируй событие ЕИС ({xml_name}):\n\n{content}",
                                      self.schema, max_tokens=3000)
        except Exception as e:  # noqa: BLE001 — событие без LLM-части всё равно записывается
            logger.error(f"EventAnalyzer: анализ {xml_name} не удался: {type(e).__name__}: {e}")
            return {"event_summary": None, "changes": [], "risks": [], "error": f"{type(e).__name__}: {e}",
                    "prompt_version": self.prompt_version}

        allowed = set(risk_catalog.EVENT_RISK_CODES)
        risks = []
        for r in data.get("risks") or []:
            if not isinstance(r, dict) or r.get("rule_id") not in allowed:
                continue
            r.setdefault("title", risk_catalog.title(r["rule_id"]))
            risks.append(r)
        data["risks"] = risks
        data["prompt_version"] = self.prompt_version
        return data

    async def check_event_risks(self, content: str, xml_name: str) -> Dict[str, Any]:
        """Совместимость со старым интерфейсом: строка JSON на входе.

        Args:
            content: JSON-строка пакета события.
            xml_name: Имя XML.

        Returns:
            Dict[str, Any]: См. :meth:`analyze`.
        """
        try:
            payload = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            payload = {"raw": content}
        return await self.analyze(payload, xml_name)
