"""LLM-анализ одного события ЕИС (XML-документа): суть, оценка изменений, значимость, риски.

Вход — компактный пакет, собранный кодом (``xml_events.build_llm_payload``): тип события,
ключевые сведения, изменения «было → стало», изменения приложенных файлов и их резюме.
Анализ комплекта документов закупки сюда не входит — он делается на уровне закупки.

Коды рисков берутся из единого каталога ``risk_monitoring/risk_catalog.py``.

Пакетный режим (``analyze_batch``): события одной закупки (контракта) за выгрузку разбираются одним
запросом — меньше вызовов модели, и модель видит последовательность событий. Ответ — массив
``events`` с ``event_id``; событие, которого нет в ответе, разбирается отдельным запросом.
"""
import json
from typing import Any, Dict, List

from configs.logger import get_logger
from risk_monitoring import risk_catalog
from risk_monitoring.llm_json import LLMJson, prompt_version

logger = get_logger(__name__)

PROMPT_PATH = "prompts/event_analyzer_prompt.txt"
SCHEMA_PATH = "schemas/event_analyzer_schema.json"
MAX_PAYLOAD_CHARS = 24000
BATCH_MAX_EVENTS = 6         # событий в одном запросе
BATCH_MAX_CHARS = 18000      # суммарный размер пакетов событий в одном запросе (контекст модели 16 384 токена)
BATCH_PROMPT = """

## НЕСКОЛЬКО СОБЫТИЙ
Передано несколько событий одной закупки (контракта) в порядке версий: массив `events`, у каждого `event_id`.
Разбери каждое событие отдельно по правилам выше, учитывая контекст соседних событий (например, серию
изменений сроков). Верни `{"events": [...]}`: по одному объекту на каждое переданное событие, с тем же `event_id`.
"""

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

    def _clean(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Оставляет риски только с кодами каталога, подставляет название риска, добавляет версию промпта."""
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

    @staticmethod
    def batches(items: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """Делит события одной закупки на пакеты по ``BATCH_MAX_EVENTS`` и ``BATCH_MAX_CHARS``.

        Args:
            items: ``{"event_id", "payload"}`` в порядке версий.

        Returns:
            List[List[Dict[str, Any]]]: Пакеты.
        """
        out: List[List[Dict[str, Any]]] = []
        cur: List[Dict[str, Any]] = []
        size = 0
        for it in items:
            n = len(json.dumps(it["payload"], ensure_ascii=False, default=str))
            if cur and (len(cur) >= BATCH_MAX_EVENTS or size + n > BATCH_MAX_CHARS):
                out.append(cur)
                cur, size = [], 0
            cur.append(it)
            size += n
        if cur:
            out.append(cur)
        return out

    async def analyze_batch(self, items: List[Dict[str, Any]], name: str) -> Dict[Any, Dict[str, Any]]:
        """Разбирает несколько событий одной закупки одним запросом.

        Args:
            items: ``{"event_id", "payload"}``.
            name: Подпись для логов (номер закупки).

        Returns:
            Dict[Any, Dict[str, Any]]: ``event_id → ответ`` (как у :meth:`analyze`). Если пакет из одного
            события или модель пропустила событие — оно разбирается отдельным запросом.
        """
        if len(items) == 1:
            return {items[0]["event_id"]: await self.analyze(items[0]["payload"], name)}
        schema = {"type": "object", "required": ["events"], "properties": {"events": {"type": "array", "items": {
            **self.schema, "required": ["event_id"] + list(self.schema.get("required", [])),
            "properties": {"event_id": {"type": "integer"}, **self.schema.get("properties", {})}}}}}
        content = json.dumps({"events": [{"event_id": it["event_id"], **it["payload"]} for it in items]},
                             ensure_ascii=False, default=str)
        out: Dict[Any, Dict[str, Any]] = {}
        try:
            data = await self.llm.ask(self.prompt + BATCH_PROMPT,
                                      f"Проанализируй события ЕИС ({name}, {len(items)} шт.):\n\n{content}",
                                      schema, max_tokens=min(8000, 1500 * len(items) + 1000))
            ids = {it["event_id"] for it in items}
            for ev in data.get("events") or []:
                if isinstance(ev, dict) and ev.get("event_id") in ids and ev["event_id"] not in out:
                    out[ev["event_id"]] = self._clean(ev)
        except Exception as e:  # noqa: BLE001 — разбираем по одному
            logger.error(f"EventAnalyzer: пакет {name} ({len(items)} соб.) не разобран: {type(e).__name__}: {e}")
        for it in items:
            if it["event_id"] not in out:
                out[it["event_id"]] = await self.analyze(it["payload"], name)
        return out

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

        return self._clean(data)

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
