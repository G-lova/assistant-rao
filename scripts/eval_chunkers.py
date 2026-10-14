"""Сравнение нарезок на чанки перед переводом риск-мониторинга на ``pages_v2``.

Два замера (решение 13.10.2026: одна нарезка ``pages_v2``, РМ переводится, если она не хуже):

1. **Поиск фактов** (``recall``): для фактов модели с подтверждённой цитатой известно, где лежит правильный
   фрагмент. Для каждой нарезки считается, на каком месте в выдаче поиска по критерию оказывается первый
   чанк, содержащий цитату: recall@4/8/16 и MRR. Старая нарезка экспертизы — ``pe_chunks``
   (1500/150 символов), новая — ``ai_document_chunks`` с ``chunker = 'pages_v2'``.
2. **Похожие документы** (``similar``): по CSV пар документов ``doc_a,doc_b,label`` (``ai_documents.id``;
   ``label``: ``same`` — редакции одного документа, ``related`` — типовой документ у разных заказчиков,
   ``unrelated`` — несвязанные) считается ``coverage`` как в ``AIRepo.find_similar_documents`` для нарезки РМ
   ``rm_tokens_800`` и для ``pages_v2`` при порогах близости чанка 0,75–0,95, насколько хорошо он отделяет
   связанные пары от несвязанных (AUC) и какое правило «почти копия» лучше всего находит редакции (F1).
   Пороги РМ (0,85 / 0,9 / 0,95) подобраны на ``rm_tokens_800`` — для ``pages_v2`` их нужно подобрать заново
   по этому же отчёту.

Запуск (внутри контейнера приложения, параметры БД — ``DB_*``)::

    python -m scripts.eval_chunkers recall --env dev --ids 5885,5887
    python -m scripts.eval_chunkers pairs --out pairs.csv -n 50        # заготовка пар, related? — разметить
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


THRESHOLDS = (0.75, 0.8, 0.85, 0.9, 0.95)
COVERAGE_CUTS = tuple(round(0.5 + 0.05 * i, 2) for i in range(10))      # 0.50 … 0.95
AVG_CUTS = (0.85, 0.88, 0.9, 0.92, 0.95, 0.97)


def best_same_rule(points: Sequence[Tuple[float, float, str]]) -> Optional[Dict[str, float]]:
    """Лучшее правило «почти копия» (``coverage >= c`` и ``avg >= a``) по F1 для пар ``same`` против остальных.

    Так подбираются ``NEAR_DUPLICATE_COVERAGE`` / ``NEAR_DUPLICATE_AVG_SIM`` риск-мониторинга.

    Args:
        points: ``(coverage, avg_similarity, label)`` по парам.

    Returns:
        Optional[dict]: ``{"coverage", "avg", "f1", "precision", "recall"}`` или ``None``, если нет пар ``same``.
    """
    positives = sum(1 for p in points if p[2] == "same")
    if not positives:
        return None
    best = None
    for c in COVERAGE_CUTS:
        for a in AVG_CUTS:
            hit = [p for p in points if p[0] >= c and p[1] >= a]
            tp = sum(1 for p in hit if p[2] == "same")
            if not tp:
                continue
            precision, recall = tp / len(hit), tp / positives
            f1 = 2 * precision * recall / (precision + recall)
            if best is None or f1 > best["f1"]:
                best = {"coverage": c, "avg": a, "f1": round(f1, 3), "precision": round(precision, 3),
                        "recall": round(recall, 3)}
    return best


def similarity_report(best_rows: Sequence[Tuple[np.ndarray, str]]) -> Dict[str, dict]:
    """Отчёт по порогам близости чанка для одной нарезки.

    Args:
        best_rows: По паре — максимальное сходство каждого чанка ``doc_a`` с чанками ``doc_b`` и метка.

    Returns:
        dict: По порогу — средний ``coverage`` по меткам, AUC «связанные против несвязанных», AUC «редакции
        против похожих» и лучшее правило «почти копия».
    """
    report: Dict[str, dict] = {}
    for t in THRESHOLDS:
        points = []
        for best, label in best_rows:
            hit = best[best >= t]
            points.append((float(len(hit) / len(best)), float(hit.mean()) if len(hit) else 0.0, label))
        by_label: Dict[str, List[float]] = {}
        for cov, _, label in points:
            by_label.setdefault(label, []).append(cov)
        report[str(t)] = {
            "mean_coverage": {k: round(sum(v) / len(v), 3) for k, v in by_label.items()},
            "auc_linked_vs_unrelated": auc(by_label.get("same", []) + by_label.get("related", []),
                                           by_label.get("unrelated", [])),
            "auc_same_vs_related": auc(by_label.get("same", []), by_label.get("related", [])),
            "near_duplicate_rule": best_same_rule(points),
        }
    return report


async def run_similar(conn, embedder, pairs: Sequence[Tuple[int, int, str]], build_missing: bool) -> Dict[str, dict]:
    """Замер 2: ``coverage`` пар документов для нарезки РМ и ``pages_v2`` при разных порогах близости чанка.

    Векторы каждого документа читаются (или строятся) один раз на нарезку.

    Args:
        conn: Соединение ``asyncpg``.
        embedder: Клиент эмбеддингов (нужен при ``build_missing``).
        pairs: ``(doc_a, doc_b, label)``.
        build_missing: Строить чанки ``pages_v2`` в памяти, если их нет.

    Returns:
        dict: По нарезке — число пар по меткам и :func:`similarity_report`.
    """
    report: Dict[str, dict] = {}
    for chunker in (RM_CHUNKER, CHUNKER_PAGES_V2):
        cache: Dict[int, np.ndarray] = {}
        rows: List[Tuple[np.ndarray, str]] = []
        for a, b, label in pairs:
            for doc in (a, b):
                if doc not in cache:
                    cache[doc] = await _vectors(conn, embedder, doc, chunker, build_missing)
            va, vb = cache[a], cache[b]
            if len(va) and len(vb):
                rows.append((cosine_matrix(va, vb).max(axis=1), label))
        counts: Dict[str, int] = {}
        for _, label in rows:
            counts[label] = counts.get(label, 0) + 1
        report[chunker] = {"pairs": counts, "by_threshold": similarity_report(rows)}
    return report


SQL_VERSION_PAIRS = """
SELECT DISTINCT a.document_id AS doc_a, split_part(a.analysis_type, ':', 2)::bigint AS doc_b
FROM ai_document_analysis a
JOIN ai_documents d1 ON d1.id = a.document_id AND d1.text IS NOT NULL
JOIN ai_documents d2 ON d2.id = split_part(a.analysis_type, ':', 2)::bigint AND d2.text IS NOT NULL
WHERE a.analysis_type ~ '^version_diff:[0-9]+$' AND a.document_id <> split_part(a.analysis_type, ':', 2)::bigint
LIMIT $1
"""
SQL_TYPED_SAMPLE = """
SELECT t.document_id, t.detected_type FROM (
    SELECT DISTINCT ON (a.document_id) a.document_id, a.result->>'detected_type' AS detected_type
    FROM ai_document_analysis a JOIN ai_documents d ON d.id = a.document_id AND d.text IS NOT NULL
    WHERE a.analysis_type = 'doc_type' AND a.result ? 'detected_type'
    ORDER BY a.document_id, a.created_at DESC
) t
ORDER BY random()
LIMIT $1
"""


def pair_sample(typed: Sequence[Tuple[int, str]], n: int, skip: set) -> Tuple[List[tuple], List[tuple]]:
    """Пары из случайной выборки документов: разного типа — ``unrelated``, одного типа — кандидаты ``related?``.

    Args:
        typed: ``(document_id, detected_type)`` в случайном порядке.
        n: Сколько пар каждого вида нужно.
        skip: Пары, уже размеченные как ``same`` (не повторять).

    Returns:
        tuple: ``(unrelated, related_candidates)`` — списки ``(doc_a, doc_b, label, type_a, type_b)``.
    """
    unrelated, related = [], []
    by_type: Dict[str, List[int]] = {}
    for i in range(0, len(typed) - 1, 2):
        (a, ta), (b, tb) = typed[i], typed[i + 1]
        if ta != tb and len(unrelated) < n and (min(a, b), max(a, b)) not in skip:
            unrelated.append((a, b, "unrelated", ta, tb))
    for doc, t in typed:
        by_type.setdefault(t, []).append(doc)
    for t, docs in by_type.items():
        for a, b in zip(docs[::2], docs[1::2]):
            if len(related) < n and (min(a, b), max(a, b)) not in skip:
                related.append((a, b, "related?", t, t))
    return unrelated, related


async def make_pairs(conn, n: int) -> List[tuple]:
    """Заготовка CSV пар для замера 2.

    * ``same`` — редакции одного документа: пары, для которых РМ уже строил сравнение редакций
      (``ai_document_analysis.analysis_type = 'version_diff:<id прошлой редакции>'``);
    * ``unrelated`` — случайные документы разного типа (по ``doc_type`` РМ);
    * ``related?`` — документы одного типа: **разметить вручную** (``related`` — типовой документ с общим
      текстом, ``unrelated`` — просто тот же тип, ``same`` — редакции); строки с ``?`` при замере пропускаются.

    Args:
        conn: Соединение ``asyncpg``.
        n: Сколько пар каждого вида.

    Returns:
        list: Строки ``(doc_a, doc_b, label, type_a, type_b)``.
    """
    same = [(int(r["doc_a"]), int(r["doc_b"]), "same", "", "") for r in await conn.fetch(SQL_VERSION_PAIRS, int(n))]
    skip = {(min(a, b), max(a, b)) for a, b, *_ in same}
    typed = [(int(r["document_id"]), r["detected_type"]) for r in await conn.fetch(SQL_TYPED_SAMPLE, int(n) * 8)]
    unrelated, related = pair_sample(typed, n, skip)
    return same + unrelated + related


def read_pairs(path: str) -> List[Tuple[int, int, str]]:
    """Читает CSV пар ``doc_a,doc_b,label``.

    Args:
        path: Путь к CSV.

    Returns:
        list: Пары.
    """
    with open(path, encoding="utf-8") as fh:
        rows = [(int(r["doc_a"]), int(r["doc_b"]), (r.get("label") or "").strip()) for r in csv.DictReader(fh)]
    return [r for r in rows if r[2] in ("same", "related", "unrelated")]   # «related?» не размечены — пропуск


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
    p = sub.add_parser("pairs", help="заготовка CSV пар для similar (same / unrelated / related? на разметку)")
    p.add_argument("--out", required=True, help="куда записать CSV")
    p.add_argument("-n", type=int, default=50, help="пар каждого вида")
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
            elif args.mode == "pairs":
                rows = await make_pairs(conn, args.n)
                with open(args.out, "w", encoding="utf-8", newline="") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(["doc_a", "doc_b", "label", "type_a", "type_b"])
                    writer.writerows(rows)
                result = {"written": args.out, "rows": len(rows),
                          "by_label": {k: sum(1 for r in rows if r[2] == k) for k in ("same", "unrelated", "related?")}}
            else:
                result = await run_similar(conn, embedder, read_pairs(args.pairs), args.build_missing)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
