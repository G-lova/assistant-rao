import logging

from typing import List

from .database import QdrantDatabase
from .embeddings import QwenEmbedder


db = QdrantDatabase()
embedder = QwenEmbedder()


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def search_similar_documents(query: str, legislation: str = None, limit: int = 3) -> List[dict]:
    """
    Выполняет семантический поиск похожих документов в векторной базе на основе текстового запроса.

    Функция преобразует запрос в векторное представление (эмбеддинг), затем ищет ближайшие
    по схожести документы в Qdrant. При необходимости фильтрует результаты по типу законодательства.

    Args:
        query (str): Текстовый запрос, для которого ищутся похожие документы.
        legislation (str, optional): Ограничение по типу законодательства (например, "44-ФЗ"). 
                                    Если указано, в результаты попадают только документы, соответствующие этому типу.
        limit (int, optional): Максимальное количество возвращаемых результатов. По умолчанию 3.

    Returns:
        List[dict]: Список словарей с информацией о похожих документах, каждый содержит:
            - name: имя документа;
            - legislation: тип законодательства;
            - text_snippet: фрагмент текста документа (первые 300 символов);
            - score: оценка схожести (чем выше, тем ближе документ к запросу).
            В случае ошибки возвращается пустой список.
    """
    try:
        query_embedding = embedder.embed([query])[0]
        results = db.search_similar(query_embedding, limit=limit)
        filtered = []

        for hit in results:
            payload = hit.payload
            if legislation and payload.get("legislation") != legislation:
                continue
            text = payload.get("text", "")
            snippet = text[:300] + "..." if len(text) > 300 else text
            filtered.append({
                "name": payload["name"],
                "legislation": payload["legislation"],
                "text_snippet": snippet,
                "score": hit.score
            })
        return filtered
    
    except Exception as e:
        logger.warning(f"Search failed: {e}")
        return []