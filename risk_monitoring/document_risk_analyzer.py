import asyncio
import hashlib
import json
import re
from typing import Any, Dict, List, Optional

from fuzzywuzzy import fuzz
from json_repair import repair_json

from configs.logger import get_logger
from configs.rate_limiter import TokenBucket
from configs.retry_utils import LLM_RETRY_CONFIG, async_retry
from configs.utils import split_large_text

logger = get_logger(__name__)

PROMPT_PATH = "prompts/risk_analysis_prompt.txt"
SCHEMA_PATH = "schemas/risk_analysis_schema.json"
CHUNK_TOKENS = 10000
MIN_CONFIDENCE = 0.7
FUZZY_THRESHOLD = 90
SEVERITY_WEIGHT = {"low": 10, "medium": 25, "high": 40}
CATEGORY_BY_PREFIX = {"DOC": "Документные", "AI": "AI"}


def _norm(s: str) -> str:
    """Нормализация для сверки цитаты: OCR-текст приходит с литеральными \\n и \\"."""
    s = (s or "").replace("\\n", " ").replace('\\"', '"')
    return re.sub(r"\s+", " ", s).strip().lower()


class DocumentRiskAnalyzer:
    """
    Поиск признаков риска в тексте документа.

    LLM только находит признаки и цитирует текст. Всё остальное делает код:
      * цитата обязана реально присутствовать в тексте (иначе риск отбрасывается как галлюцинация);
      * балл риска считается формулой (вес severity × confidence), а не выдумывается моделью.
    """

    def __init__(self, llm_client, model: str):
        self.client = llm_client
        self.model = model
        self.rate_limiter = TokenBucket(rate=1.5)
        self.semaphore = asyncio.Semaphore(3)

        with open(PROMPT_PATH, "rb") as f:
            raw = f.read()
        self.prompt = raw.decode("utf-8")
        with open(SCHEMA_PATH, encoding="utf-8") as f:
            self.schema = json.load(f)

        # Изменили промпт или схему -> кэш риск-анализа инвалидируется сам
        self.prompt_version = hashlib.sha256(raw + json.dumps(self.schema, sort_keys=True).encode()).hexdigest()[:12]

    # ------------------------------------------------------------------ public

    async def analyze(self, text: str, file_name: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        chunks = split_large_text(text, max_chunk_size=CHUNK_TOKENS)
        if not chunks:
            return self._empty(partial=True)

        async def _one(i: int, chunk: str):
            async with self.semaphore:
                return await self._analyze_chunk(chunk, file_name, context or {}, i + 1, len(chunks))

        results = await asyncio.gather(*(_one(i, c) for i, c in enumerate(chunks)), return_exceptions=True)

        ok = [r for r in results if isinstance(r, dict)]
        for r in results:
            if isinstance(r, BaseException):
                logger.error(f"Риск-анализ чанка не удался ({file_name}): {r}")
        if not ok:
            raise RuntimeError("Риск-анализ не выполнен ни для одного чанка")

        risks = self._merge([risk for r in ok for risk in r["risks"]])
        score = self._score(risks)
        dropped = sum(r["dropped"] for r in ok)

        summary = ok[0]["summary"] if len(ok) == 1 else self._build_summary(risks)
        return {
            "risk_score": score,
            "risk_level": self._level(score),
            "summary": summary,
            "risks": risks,
            "partial": len(ok) < len(chunks),          # часть чанков не проанализирована
            "dropped_unverified": dropped,             # цитаты, которых нет в тексте
            "prompt_version": self.prompt_version,
        }

    @staticmethod
    def to_compact(result: Dict[str, Any], top: int = 15) -> Dict[str, Any]:
        """Компактный вид для внешней БД."""
        return {
            "risk_score": result["risk_score"],
            "risk_level": result["risk_level"],
            "summary": result["summary"],
            "partial": result["partial"],
            "risks": [
                {
                    "code": r["code"],
                    "category": r["category"],
                    "severity": r["severity"],
                    "title": r["title"],
                    "explanation": r["explanation"],
                    "fragment": r["evidence"]["fragment"],
                    "section": r["evidence"].get("section"),
                    "page": r["evidence"].get("page"),
                    "confidence": r["evidence"]["confidence"],
                }
                for r in result["risks"][:top]
            ],
        }

    # ----------------------------------------------------------------- private

    @async_retry(LLM_RETRY_CONFIG)
    async def _analyze_chunk(self, chunk: str, file_name: str, context: Dict[str, Any], n: int, total: int) -> Dict[str, Any]:
        await self.rate_limiter.acquire()

        hint = json.dumps(context, ensure_ascii=False)[:1500] if context else "нет"
        user = (
            f"Документ: {file_name} (часть {n} из {total}).\n"
            f"Контекст извлечения (тип, краткое содержание): {hint}\n\n"
            f"Текст:\n\n{chunk}"
        )
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": self.prompt}, {"role": "user", "content": user}],
            max_tokens=3000,
            temperature=0.1,
            extra_body={"guided_json": self.schema},
        )
        raw = response.choices[0].message.content.strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = json.loads(repair_json(raw))   # ValueError -> сработает retry
        if not isinstance(data, dict):
            raise ValueError("Ответ LLM не является объектом")

        norm_chunk = _norm(chunk)
        verified, dropped = [], 0
        for r in data.get("risks", []):
            ev = r.get("evidence") or {}
            frag = ev.get("fragment", "")
            if float(ev.get("confidence", 0) or 0) < MIN_CONFIDENCE or not frag.strip():
                continue
            if not self._fragment_in_text(frag, norm_chunk):
                dropped += 1
                continue
            r["category"] = CATEGORY_BY_PREFIX.get(r["code"].split("-")[0], "Прочие")
            verified.append(r)

        return {"summary": data.get("summary", ""), "risks": verified, "dropped": dropped}

    @staticmethod
    def _fragment_in_text(fragment: str, norm_chunk: str) -> bool:
        nf = _norm(fragment)
        if not nf:
            return False
        if nf in norm_chunk:
            return True
        return fuzz.partial_ratio(nf, norm_chunk) >= FUZZY_THRESHOLD

    @staticmethod
    def _weight(r: Dict[str, Any]) -> float:
        return SEVERITY_WEIGHT.get(r["severity"], 10) * float(r["evidence"]["confidence"])

    def _merge(self, risks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen, out = set(), []
        for r in sorted(risks, key=self._weight, reverse=True):
            key = (r["code"], _norm(r["evidence"]["fragment"])[:80])
            if key not in seen:
                seen.add(key)
                out.append(r)
        return out

    def _score(self, risks: List[Dict[str, Any]]) -> int:
        return min(100, round(sum(self._weight(r) for r in risks)))

    @staticmethod
    def _level(score: int) -> str:
        if score == 0:
            return "none"
        return "low" if score < 25 else "medium" if score < 60 else "high"

    @staticmethod
    def _build_summary(risks: List[Dict[str, Any]]) -> str:
        if not risks:
            return "Признаков риска не выявлено."
        titles = "; ".join(r["title"] for r in risks[:3])
        return f"Выявлено признаков риска: {len(risks)}. Наиболее значимые: {titles}."

    def _empty(self, partial: bool) -> Dict[str, Any]:
        return {"risk_score": 0, "risk_level": "none", "summary": "Нет текста для анализа.",
                "risks": [], "partial": partial, "dropped_unverified": 0, "prompt_version": self.prompt_version}