"""Сравнение нарезок на чанки перед переводом риск-мониторинга на ``pages_v2``.

Два замера (решение 13.10.2026: одна нарезка ``pages_v2``, РМ переводится, если она не хуже):

1. **Поиск фактов** (``recall``): для фактов модели с подтверждённой цитатой известно, где лежит правильный
   фрагмент. Для каждой нарезки считается, на каком месте в выдаче поиска по критерию оказывается первый
   чанк, содержащий цитату: recall@4/8/16 и MRR. Старая нарезка экспертизы — ``pe_chunks``
   (1500/150 символов), новая — ``ai_document_chunks`` с ``chunker = 'pages_v2'``.
2. **Похожие документы** (``similar``): по CSV пар документов ``doc_a,doc_b,label`` (``ai_documents.id``;
   ``label``: ``same`` — редакции одного документа, ``related`` — типовой документ у разных заказчиков,
   ``unrelated`` — несвязанные) считается ``coverage`` как в ``AIRepo.find_similar_documents`` для нарезки РМ
   ``rm_tokens_800`` и для ``pages_v2``, и насколько хорошо она отделяет связанные пары от несвязанных (AUC).
   Пороги РМ (0,85 / 0,9 / 0,95) подобраны на ``rm_tokens_800`` — для ``pages_v2`` их нужно подобрать заново
   по этому же отчёту.

Запуск (внутри контейнера приложения, параметры БД — ``DB_*``)::

    python -m scripts.eval_chunkers recall --env dev --ids 5885,5887
    python -m scripts.eval_chunkers similar --pairs pairs.csv [--build-missing]
"""
import argparse
import asyncio
import csv
import json
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from knowledge_store.facts import criterion_text, find_quote
from knowledge_store.pages import CHUNKER_PAGES_V2, chunk_structured, embedding_text, split_pages

RM_CHUNKER = "rm_tokens_800"
KS = (4, 8, 16)


def parse_vector(value) -> np.ndarray:
    """Вектор из ответа Postgres (``'[0.1,0.2]'`` pgvector или список).

    Args:
        value: Значение колонки ``embedding``.

    Returns:
        np.ndarray: Вектор ``float32``.
    """
    if isinstance(value, str):
        value = json.loads(value.replace("{", "[").replace("}", "]"))
    return np.asarray(value, dtype=np.float32)


def cosine_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Матрица косинусного сходства строк ``a`` и ``b``.

    Args:
        a: Матрица ``(n, d)``.
        b: Матрица ``(m, d)``.

    Returns:
        np.ndarray: ``(n, m)``.
    """
    an = a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-12)
    bn = b / np.maximum(np.linalg.norm(b, axis=1, keepdims=True), 1e-12)
    return an @ bn.T


def rank_of_quote(ranked_texts: Sequence[str], quote: str) -> Optional[int]:
    """Место (с 1) первого чанка выдачи, содержащего цитату, или ``None``.

    Args:
        ranked_texts: Тексты чанков по убыванию сходства с запросом.
        quote: Подтверждённая цитата факта.

    Returns:
        Optional[int]: Место в выдаче.
    """
    for i, text in enumerate(ranked_texts, 1):
        if find_quote(quote, text):
            return i
    return None


def recall_summary(ranks: Sequence[Optional[int]], ks: Iterable[int] = KS) -> Dict[str, float]:
    """recall@k и MRR по местам правильных фрагментов.

    Args:
        ranks: Места (``None`` — цитата не найдена ни в одном чанке).
        ks: Значения ``k``.

    Returns:
        dict: ``{"n", "recall@k"..., "mrr"}``.
    """
    n = len(ranks)
    out: Dict[str, float] = {"n": n}
    for k in ks:
        out[f"recall@{k}"] = round(sum(1 for r in ranks if r and r <= k) / n, 3) if n else 0.0
    out["mrr"] = round(sum(1 / r for r in ranks if r) / n, 3) if n else 0.0
    return out


def coverage(src: np.ndarray, other: np.ndarray, threshold: float = 0.85) -> Tuple[float, float]:
    """``coverage`` и среднее сходство, как в ``AIRepo.find_similar_documents``.

    Args:
        src: Векторы чанков исходного документа.
        other: Векторы чанков второго документа.
        threshold: Порог близости чанка.

    Returns:
        tuple: ``(доля чанков src с близким аналогом, среднее сходство таких пар)``.
    """
    if not len(src) or not len(other):
        return 0.0, 0.0
    best = cosine_matrix(src, other).max(axis=1)
    hit = best[best >= threshold]
    return round(float(len(hit) / len(src)), 3), round(float(hit.mean()) if len(hit) else 0.0, 3)


def auc(positive: Sequence[float], negative: Sequence[float]) -> Optional[float]:
    """Вероятность, что связанная пара получит больший балл, чем несвязанная (ничьи — пополам).

    Args:
        positive: Баллы связанных пар.
        negative: Баллы несвязанных пар.

    Returns:
        Optional[float]: AUC или ``None``, если одной из групп нет.
    """
    if not positive or not negative:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in positive for n in negative)
    return round(wins / (len(positive) * len(negative)), 3)


SQL_FACTS = """
SELECT f.fact_key, f.quote, ff.label
FROM pe_facts f
JOIN pe_procurements p ON p.env = f.env AND p.expertise_id = f.expertise_id
JOIN pe_form_fields ff ON ff.form_code = p.form_code AND ff.field_key = f.fact_key
WHERE f.env = $1 AND f.expertise_id = $2 AND f.source = 'fact_extractor' AND f.quote IS NOT NULL
  AND (f.value->>'verified')::boolean
"""
SQL_OLD_CHUNKS = "SELECT text, embedding::text AS embedding FROM pe_chunks WHERE env = $1 AND expertise_id = $2"
SQL_NEW_CHUNKS = """
SELECT c.text, c.embedding::text AS embedding FROM pe_documents d JOIN ai_document_chunks c ON c.document_id = d.ai_document_id
WHERE d.env = $1 AND d.expertise_id = $2 AND c.chunker = $3
"""
SQL_DOC_CHUNKS = "SELECT embedding::text AS embedding FROM ai_document_chunks WHERE document_id = $1 AND chunker = $2 ORDER BY chunk_no"
SQL_DOC_TEXT = "SELECT text FROM ai_documents WHERE id = $1"


async def run_recall(conn, embedder, env: str, ids: Sequence[int]) -> Dict[str, Dict[str, float]]:
    """Замер 1 по экспертизам: места правильных фрагментов в выдаче старой и новой нарезки.

    Args:
        conn: Соединение ``asyncpg``.
        embedder: Клиент эмбеддингов.
        env: Окружение.
        ids: ID экспертиз.

    Returns:
        dict: ``{"pe_chunks": recall_summary, "pages_v2": recall_summary}``.
    """
    ranks: Dict[str, List[Optional[int]]] = {"pe_chunks": [], CHUNKER_PAGES_V2: []}
    for expertise_id in ids:
        facts = await conn.fetch(SQL_FACTS, env, int(expertise_id))
        sets = {"pe_chunks": await conn.fetch(SQL_OLD_CHUNKS, env, int(expertise_id)),
                CHUNKER_PAGES_V2: await conn.fetch(SQL_NEW_CHUNKS, env, int(expertise_id), CHUNKER_PAGES_V2)}
        if not facts or not all(sets.values()):
            print(f"{env}:{expertise_id}: нет фактов с цитатами или чанков одной из нарезок — пропущено", file=sys.stderr)
            continue
        queries = np.asarray(await embedder.get_embeddings([criterion_text(f["label"]) for f in facts]), dtype=np.float32)
        for name, rows in sets.items():
            matrix = np.vstack([parse_vector(r["embedding"]) for r in rows])
            sims = cosine_matrix(queries, matrix)
            for fact, row in zip(facts, sims):
                order = np.argsort(-row)
                ranks[name].append(rank_of_quote([rows[i]["text"] for i in order], fact["quote"]))
    return {name: recall_summary(r) for name, r in ranks.items()}


async def _vectors(conn, embedder, document_id: int, chunker: str, build_missing: bool) -> np.ndarray:
    """Векторы чанков документа для нарезки (при ``build_missing`` ``pages_v2`` строится в памяти, без записи)."""
    rows = await conn.fetch(SQL_DOC_CHUNKS, int(document_id), chunker)
    if rows:
        return np.vstack([parse_vector(r["embedding"]) for r in rows])
    if chunker != CHUNKER_PAGES_V2 or not build_missing:
        return np.zeros((0, 1), dtype=np.float32)
    text = await conn.fetchval(SQL_DOC_TEXT, int(document_id))
    chunks = chunk_structured(split_pages(text or ""))
    if not chunks:
        return np.zeros((0, 1), dtype=np.float32)
    return np.asarray(await embedder.get_embeddings([embedding_text(c) for c in chunks]), dtype=np.float32)


async def run_similar(conn, embedder, pairs: Sequence[Tuple[int, int, str]], build_missing: bool,
                      threshold: float = 0.85) -> Dict[str, dict]:
    """Замер 2: ``coverage`` пар документов для нарезки РМ и ``pages_v2`` и AUC связанных против несвязанных.

    Args:
        conn: Соединение ``asyncpg``.
        embedder: Клиент эмбеддингов (нужен при ``build_missing``).
        pairs: ``(doc_a, doc_b, label)``.
        build_missing: Строить чанки ``pages_v2`` в памяти, если их нет.
        threshold: Порог близости чанка.

    Returns:
        dict: По нарезке — средний ``coverage`` по меткам и ``auc``.
    """
    report: Dict[str, dict] = {}
    for chunker in (RM_CHUNKER, CHUNKER_PAGES_V2):
        by_label: Dict[str, List[float]] = {}
        for a, b, label in pairs:
            va = await _vectors(conn, embedder, a, chunker, build_missing)
            vb = await _vectors(conn, embedder, b, chunker, build_missing)
            if not len(va) or not len(vb):
                continue
            by_label.setdefault(label, []).append(coverage(va, vb, threshold)[0])
        positive = by_label.get("same", []) + by_label.get("related", [])
        report[chunker] = {"mean_coverage": {k: round(sum(v) / len(v), 3) for k, v in by_label.items()},
                           "pairs": {k: len(v) for k, v in by_label.items()},
                           "auc": auc(positive, by_label.get("unrelated", []))}
    return report


def read_pairs(path: str) -> List[Tuple[int, int, str]]:
    """Читает CSV пар ``doc_a,doc_b,label``.

    Args:
        path: Путь к CSV.

    Returns:
        list: Пары.
    """
    with open(path, encoding="utf-8") as fh:
        return [(int(r["doc_a"]), int(r["doc_b"]), r["label"].strip()) for r in csv.DictReader(fh)]


async def main(argv=None) -> int:
    """Точка входа.

    Args:
        argv: Аргументы командной строки.

    Returns:
        int: Код возврата.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    r = sub.add_parser("recall", help="поиск фактов: pe_chunks против pages_v2")
    r.add_argument("--env", required=True)
    r.add_argument("--ids", required=True, help="ID экспертиз через запятую")
    s = sub.add_parser("similar", help="похожие документы: rm_tokens_800 против pages_v2")
    s.add_argument("--pairs", required=True, help="CSV doc_a,doc_b,label")
    s.add_argument("--build-missing", action="store_true", help="строить pages_v2 в памяти, если чанков нет")
    s.add_argument("--threshold", type=float, default=0.85)
    args = parser.parse_args(argv)

    from configs.http_client_manager import HTTPClientManager
    from knowledge_store import db
    from knowledge_store.indexer import build_embedder
    from knowledge_store.scope import normalize_env

    async with HTTPClientManager(timeout=120.0) as mgr, db.task_pool():
        embedder = build_embedder(mgr)
        async with db.connection() as conn:
            if args.mode == "recall":
                ids = [int(x) for x in args.ids.split(",") if x.strip()]
                result = await run_recall(conn, embedder, normalize_env(args.env), ids)
            else:
                result = await run_similar(conn, embedder, read_pairs(args.pairs), args.build_missing, args.threshold)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
