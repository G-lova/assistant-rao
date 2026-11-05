"""Типовые формулировки: цитата риска, которая почти дословно встречается в документах многих закупок.

Модель не отличает требование заказчика от пересказа нормы закона или типового текста инструкции
(«документы должны быть чётко напечатаны», «или эквивалент», ст. 31, условия независимой гарантии).
Но типовой текст повторяется у разных заказчиков, а требование конкретного заказчика — нет. Поэтому:

1. Для цитаты каждого риска считается эмбеддинг и ищутся ближайшие чанки документов (и риск-мониторинга,
   и экспертиз) — ``AIRepo.find_chunks_many`` (все цитаты файла одним запросом на нарезку).
2. Чанк засчитывается, если он почти дословно содержит цитату: ≥ 80 % шинглов из 5 слов цитаты есть в чанке
   (:func:`contains`).
3. Для документов таких чанков определяются закупки (карточки РМ и экспертизы) и, для карточек, заказчики.
   Документы той же закупки и того же заказчика не считаются.
4. Цитата типовая, если она встречается в закупках не менее чем ``settings.BOILERPLATE_MIN_SOURCES`` (3) разных
   заказчиков. Заказчик экспертизы — организация сопоставленной карточки РМ (``procurement_identity``), иначе
   организация из паспорта экспертизы.

Запросов к модели нет; нужен один запрос эмбеддингов на документ. Пока документов в базе мало, фильтр
срабатывает редко — с ростом базы точнее.
"""
import re
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence

from configs.logger import get_logger

logger = get_logger(__name__)

WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
SHINGLE = 5
CONTAIN_SHARE = 0.8
MIN_WORDS = 8           # короче — слишком общая цитата, по ней ничего не решаем


def _shingles(text: str, n: int = SHINGLE) -> set:
    """Шинглы из ``n`` слов (нижний регистр)."""
    words = WORD_RE.findall((text or "").lower().replace("ё", "е"))
    return {" ".join(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def contains(fragment: str, chunk_text: str, share: float = CONTAIN_SHARE) -> bool:
    """Содержит ли текст чанка цитату почти дословно (доля шинглов цитаты в чанке ≥ ``share``).

    Args:
        fragment: Цитата риска.
        chunk_text: Текст чанка другого документа.
        share: Порог доли.

    Returns:
        bool: ``True`` — цитата есть в чанке.
    """
    if len(WORD_RE.findall(fragment or "")) < MIN_WORDS:
        return False
    a = _shingles(fragment)
    if not a:
        return False
    return len(a & _shingles(chunk_text)) / len(a) >= share


def min_sources() -> int:
    """Порог числа разных заказчиков (``settings.BOILERPLATE_MIN_SOURCES``)."""
    from configs import settings

    return max(2, int(settings.BOILERPLATE_MIN_SOURCES))


def _org_name(name: Optional[str]) -> str:
    """Название организации для сравнения: нижний регистр, без кавычек и лишних пробелов."""
    return re.sub(r"\s+", " ", re.sub(r"[\"«»'`]", "", (name or "").lower().replace("ё", "е"))).strip()


def count_sources(doc_ids: Iterable[int], sources: Sequence[Dict[str, Any]], orgs: Dict[int, Any],
                  own_card: Optional[int], own_org: Optional[Any], own_org_name: Optional[str] = None) -> int:
    """Сколько разных заказчиков используют документы с цитатой.

    Заказчик: для карточки РМ — организация карточки во внешней БД; для экспертизы — организация
    сопоставленной карточки (``rm_card_id``), иначе организация из паспорта экспертизы (``org_name``).

    Args:
        doc_ids: Документы, в чанках которых найдена цитата.
        sources: ``document_id``, ``kind`` (``rm`` / ``pe``), ``source_id``, ``rm_card_id``, ``org_name``.
        orgs: ``id карточки → id заказчика``.
        own_card: Карточка текущего файла (её документы не считаются).
        own_org: Заказчик текущего файла (его закупки не считаются).
        own_org_name: Название заказчика текущего файла (для экспертиз без карточки).

    Returns:
        int: Число разных заказчиков.
    """
    docs = {int(d) for d in doc_ids}
    own_name = _org_name(own_org_name)
    keys = set()
    for s in sources:
        if int(s["document_id"]) not in docs:
            continue
        card = s.get("rm_card_id") if s.get("rm_card_id") is not None else (s["source_id"] if s["kind"] == "rm" else None)
        if card is not None:
            card = int(card)
            if own_card is not None and card == int(own_card):
                continue
            org = orgs.get(card)
            if own_org is not None and org is not None and str(org) == str(own_org):
                continue
            keys.add(("org", str(org)) if org is not None else ("card", card))
            continue
        name = _org_name(s.get("org_name"))
        if name:
            if own_name and name == own_name:
                continue
            keys.add(("name", name))
        else:
            keys.add(("pe", int(s["source_id"])))
    return len(keys)


async def typical_fragments(fragments: List[str], *, embedder: Any, repo: Any, env: str, document_id: int,
                            own_card: Optional[int], own_org: Optional[Any], embedding_model: str,
                            fetch_orgs: Callable[[List[int]], Awaitable[Dict[int, Any]]],
                            own_org_name: Optional[str] = None, cache: Any = None) -> Dict[str, int]:
    """Число разных заказчиков для каждой цитаты (только цитаты, найденные хотя бы у одного другого).

    С кэшем прогона (``cache`` — ``RunCache``) цитата, уже проверенная в этом прогоне (типовой текст повторяется
    у файлов одного заказчика и у редакций документа), не отправляется ни на эмбеддинги, ни в поиск, а закупки
    документа запрашиваются один раз. Документы, сохранённые позже в том же прогоне, для такой цитаты не
    учитываются — заказчиков может оказаться на одного-двух меньше, фильтр от этого только осторожнее.

    Args:
        fragments: Цитаты рисков.
        embedder: Клиент эмбеддингов (``get_embeddings``).
        repo: ``AIRepo`` (``find_chunks_many`` или ``find_chunks_all``, ``document_sources``).
        env: Окружение.
        document_id: Текущий документ (его чанки не считаются).
        own_card: Карточка текущего файла.
        own_org: Заказчик текущего файла.
        embedding_model: Модель эмбеддингов.
        fetch_orgs: ``список карточек → {карточка: заказчик}`` (запрос к внешней БД).
        own_org_name: Название заказчика текущего файла.
        cache: Кэш прогона (``fragment_docs``, ``doc_sources``, ``count_internal``); ``None`` — без кэша.

    Returns:
        Dict[str, int]: ``цитата → число заказчиков``.
    """
    frags = [f for f in dict.fromkeys(fragments) if len(WORD_RE.findall(f or "")) >= MIN_WORDS]
    if not frags:
        return {}
    known: Dict[str, set] = cache.fragment_docs if cache is not None else {}
    count = cache.count_internal if cache is not None else (lambda name, n=1: None)
    todo = [f for f in frags if _key(f) not in known]
    if todo:
        vectors = await embedder.get_embeddings(todo, strict=True)
        count("boilerplate.embeddings")
        per_frag = None
        if hasattr(repo, "find_chunks_many"):
            try:
                per_frag = await repo.find_chunks_many([list(v) for v in vectors], embedding_model, limit=30)
                count("boilerplate.chunk_search", 2)
            except Exception as e:  # noqa: BLE001 — поиск по одной цитате работает как раньше
                logger.warning(f"Пакетный поиск чанков не выполнен, ищем по одной цитате: {type(e).__name__}: {e}")
        if per_frag is None:
            per_frag = []
            for vec in vectors:
                per_frag.append(await repo.find_chunks_all(list(vec), embedding_model, limit=30))
                count("boilerplate.chunk_search", 2)
        for frag, rows in zip(todo, per_frag):
            # свой документ тоже запоминается: кэш общий для всех файлов прогона, свой исключается ниже
            known[_key(frag)] = {int(r["document_id"]) for r in rows if contains(frag, r["text"])}
    found = {f: known[_key(f)] - {int(document_id)} for f in frags}
    found = {f: d for f, d in found.items() if d}
    if not found:
        return {}
    all_docs = sorted({d for docs in found.values() for d in docs})
    sources = await _sources(repo, env, all_docs, cache, count)
    cards = sorted({int(s["rm_card_id"]) for s in sources if s.get("rm_card_id") is not None}
                   | {int(s["source_id"]) for s in sources if s["kind"] == "rm"})
    orgs = await fetch_orgs(cards) if cards else {}
    return {frag: count_sources(docs, sources, orgs, own_card, own_org, own_org_name) for frag, docs in found.items()}


def _key(fragment: str) -> str:
    """Ключ цитаты в кэше прогона: слова в нижнем регистре (пунктуация и регистр не важны)."""
    return " ".join(WORD_RE.findall((fragment or "").lower().replace("ё", "е")))


async def _sources(repo: Any, env: str, doc_ids: List[int], cache: Any,
                   count: Callable[..., None]) -> List[Dict[str, Any]]:
    """Закупки документов (``AIRepo.document_sources``); с кэшем прогона — каждый документ один раз."""
    if cache is None:
        count("boilerplate.document_sources")
        return [dict(r) for r in await repo.document_sources(env, doc_ids)]
    store: Dict[int, List[Dict[str, Any]]] = cache.doc_sources
    missing = [d for d in doc_ids if d not in store]
    if missing:
        count("boilerplate.document_sources")
        for d in missing:
            store[d] = []
        for r in await repo.document_sources(env, missing):
            store.setdefault(int(r["document_id"]), []).append(dict(r))
    return [r for d in doc_ids for r in store.get(d, [])]
