"""Сравнение редакций документа (концепция, гл. 18.7–18.8): текстовый diff + смысловой diff LLM.

Текстовый diff считает код (по абзацам, ``difflib``): что удалено, добавлено, заменено и насколько
редакции похожи. LLM получает только различающиеся фрагменты и определяет, что изменилось
по смыслу (появилось ограничение, изменился срок, исчезла гарантия …) и влияет ли это на риск.
"""
import difflib
import hashlib
import re
from typing import Any, Dict, List, Optional

from risk_monitoring import risk_catalog

PAGE_MARKER_RE = re.compile(r"^(?:Страница|Page)\s+\d+(?:\s*\(OCR\))?\s*:\s*", re.MULTILINE)
MAX_FRAGMENTS = 40
MAX_FRAGMENT_CHARS = 1200


def paragraphs(text: str) -> List[str]:
    """Абзацы текста без маркеров страниц и лишних пробелов (пустые и однобуквенные пропускаются).

    Args:
        text: Текст документа.

    Returns:
        List[str]: Абзацы.
    """
    text = PAGE_MARKER_RE.sub("", text or "")
    out = []
    for p in re.split(r"\n\s*\n|\n", text):
        p = re.sub(r"\s+", " ", p).strip()
        if len(p) > 2:
            out.append(p)
    return out


def text_diff(old_text: str, new_text: str, max_fragments: int = MAX_FRAGMENTS) -> Dict[str, Any]:
    """Текстовый diff двух редакций по абзацам.

    Args:
        old_text: Текст предыдущей редакции.
        new_text: Текст новой редакции.
        max_fragments: Сколько различающихся блоков вернуть (по убыванию объёма).

    Returns:
        Dict[str, Any]: ``identical``, ``similarity`` (0–1), ``stats`` (абзацев добавлено / удалено /
        заменено), ``fragments`` (``op``, ``before``, ``after``).
    """
    if hashlib.sha256((old_text or "").encode()).digest() == hashlib.sha256((new_text or "").encode()).digest():
        return {"identical": True, "similarity": 1.0, "stats": {"added": 0, "removed": 0, "replaced": 0}, "fragments": []}
    a, b = paragraphs(old_text), paragraphs(new_text)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    stats = {"added": 0, "removed": 0, "replaced": 0}
    fragments: List[Dict[str, Any]] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        before = " ¶ ".join(a[i1:i2])
        after = " ¶ ".join(b[j1:j2])
        if op == "insert":
            stats["added"] += j2 - j1
        elif op == "delete":
            stats["removed"] += i2 - i1
        else:
            stats["replaced"] += max(i2 - i1, j2 - j1)
        fragments.append({
            "op": op,
            "before": before[:MAX_FRAGMENT_CHARS] or None,
            "after": after[:MAX_FRAGMENT_CHARS] or None,
            "_size": len(before) + len(after),
        })
    fragments.sort(key=lambda f: f["_size"], reverse=True)
    for f in fragments:
        f.pop("_size", None)
    total = sum(len(x) for x in a) + sum(len(x) for x in b) or 1
    same = sum(sum(len(x) for x in a[blk.a:blk.a + blk.size]) * 2 for blk in sm.get_matching_blocks())
    return {
        "identical": not fragments,
        "similarity": round(same / total, 4),
        "stats": stats,
        "fragments": fragments[:max_fragments],
        "fragments_total": len(fragments),
    }


def version_risks(llm_diff: Optional[Dict[str, Any]], previous_file_id: Optional[int]) -> List[Dict[str, Any]]:
    """Риски по смысловым изменениям редакции (DOC-011) во внешнем формате.

    Риск ставится, если LLM отметила ``risk_relevant`` и есть изменения, повышающие риск.

    Args:
        llm_diff: Ответ ``DocumentDiffAnalyzer``.
        previous_file_id: id предыдущей версии файла (в доказательную базу).

    Returns:
        List[Dict[str, Any]]: Ноль или один риск.
    """
    if not llm_diff or not llm_diff.get("risk_relevant"):
        return []
    worse = [c for c in llm_diff.get("changes") or [] if c.get("risk_effect") == "increases"]
    if not worse:
        return []
    level = max(float(c.get("significance") or 0) for c in worse)
    return [{
        "rule_id": "DOC-011",
        "title": risk_catalog.title("DOC-011"),
        "description": llm_diff.get("summary") or "; ".join(c.get("subject", "") for c in worse),
        "level": round(min(1.0, max(0.3, level)), 4),
        "confidence": 0.8,
        "law": None,
        "evidence": [{
            "previous_file_id": previous_file_id,
            "section": c.get("section"),
            "before": c.get("before"),
            "after": c.get("after"),
            "fragment": c.get("after") or c.get("before"),
            "impact": c.get("impact"),
        } for c in worse[:5]],
        "verification_needed": ["Сравнить редакции документа и оценить обоснованность изменений"],
        "source": "version_diff",
    }]


class DocumentDiffAnalyzer:
    """Смысловое сравнение редакций документа (LLM).

    Args:
        llm_client: OpenAI-совместимый асинхронный клиент.
        model: Модель.
    """

    PROMPT_PATH = "prompts/document_diff_prompt.txt"
    SCHEMA_PATH = "schemas/document_diff_schema.json"

    def __init__(self, llm_client: Any, model: str):
        """Загружает промпт и схему."""
        import json

        from risk_monitoring.llm_json import LLMJson, prompt_version

        with open(self.PROMPT_PATH, encoding="utf-8") as f:
            self.prompt = f.read()
        with open(self.SCHEMA_PATH, encoding="utf-8") as f:
            self.schema = json.load(f)
        self.prompt_version = prompt_version(self.prompt, self.schema)
        self.llm = LLMJson(llm_client, model, rate=1.0, concurrency=2)

    async def analyze(self, diff: Dict[str, Any], file_name: str, doc_type: Optional[str]) -> Dict[str, Any]:
        """Определяет смысловые изменения по различающимся фрагментам.

        Args:
            diff: Результат :func:`text_diff`.
            file_name: Имя файла.
            doc_type: Тип документа.

        Returns:
            Dict[str, Any]: ``summary``, ``risk_relevant``, ``changes``.
        """
        import json

        payload = {"document": file_name, "doc_type": doc_type, "stats": diff.get("stats"),
                   "fragments": diff.get("fragments")}
        return await self.llm.ask(self.prompt, "Различия между редакциями:\n\n" + json.dumps(payload, ensure_ascii=False),
                                  self.schema, max_tokens=3000)
