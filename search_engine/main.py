import uuid

from fastapi import FastAPI, HTTPException
from typing import List

from search_engine.service import search_similar_documents
from search_engine.database import QdrantDatabase
from search_engine.embeddings import QwenEmbedder
from search_engine.schemas import Document, SearchResult


app = FastAPI()


# Инициализация БД и эмбеддингов
db = QdrantDatabase()
embedder = QwenEmbedder()
vector_size = embedder.get_embedding_size()


try:
    db.create_collection(vector_size)
except Exception as e:
    print(f"Collection may already exist: {e}")


@app.post("/documents/")
async def upload_document(document: Document):
    """
    Загружает документ в векторную базу данных для последующего семантического поиска.

    Преобразует текст документа в эмбеддинг с помощью модели Qwen, затем сохраняет его
    вместе с метаданными (имя, законодательство) в Qdrant. Генерирует уникальный ID.

    Args:
        document (Document): Объект документа, содержащий поля:
            - name: имя документа;
            - legislation: тип законодательства (например, "44-ФЗ");
            - text: полный текст документа.

    Raises:
        HTTPException: В случае ошибки при обработке или сохранении (код 500).

    Returns:
        dict: Словарь с ключами:
            - status: "success" при успехе;
            - id: уникальный идентификатор загруженного документа.
    """
    try:
        embedding = embedder.embed([document.text])[0].tolist()
        doc_id = str(uuid.uuid4())
        db.upload_document(doc_id, embedding, {
            "name": document.name,
            "legislation": document.legislation,
            "text": document.text
        })
        return {"status": "success", "id": doc_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/documents/search/", response_model=List[SearchResult])
async def search_similar(query: str, legislation: str = None, limit: int = 3):
    """
    Выполняет поиск похожих документов по текстовому запросу.

    Находит векторно близкие документы в базе, опционально фильтруя по законодательству.
    Возвращает ограниченное количество результатов с фрагментами текста и метаданными.

    Args:
        query (str): Поисковый запрос.
        legislation (str, optional): Фильтр по типу законодательства (например, "44-ФЗ").
        limit (int, optional): Максимальное число результатов. По умолчанию 3.

    Returns:
        List[SearchResult]: Список объектов с информацией о найденных документах.
    """
    results = search_similar_documents(query, legislation=legislation, limit=limit)
    return results


@app.get("/")
async def root():
    """
    Корневой эндпоинт, возвращает приветственное сообщение сервиса.

    Returns:
        dict: Простой словарь с полем "message", содержащим название и назначение сервиса.
    """
    return {"message": "Assistant RAO Search Engine — Qdrant + Qwen Embeddings API"}