from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List
from search_engine.database import QdrantDatabase
from search_engine.embeddings import QwenEmbedder
import uuid

app = FastAPI()

db = QdrantDatabase()
embedder = QwenEmbedder()

# Инициализация коллекции
vector_size = embedder.get_embedding_size()
try:
    db.create_collection(vector_size)
except Exception as e:
    print(f"Collection may already exist: {e}")

class Document(BaseModel):
    name: str
    legislation: str  # новое поле
    text: str

class SearchResult(BaseModel):
    name: str
    legislation: str
    text_snippet: str
    score: float

@app.post("/documents/")
async def upload_document(document: Document):
    try:
        embedding = embedder.embed(document.text)[0].tolist()
        payload = {
            "name": document.name,
            "legislation": document.legislation,
            "text": document.text
        }
        doc_id = str(uuid.uuid4())
        db.upload_document(doc_id, embedding, payload)
        return {"status": "success", "document_id": doc_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/documents/search/")
async def search_similar(query: str, limit: int = 3) -> List[SearchResult]:
    try:
        query_embedding = embedder.embed(query)[0].tolist()
        results = db.search_similar(query_embedding, limit=limit)
        formatted_results = []
        for result in results:
            text = result.payload["text"]
            snippet = text[:300] + "..." if len(text) > 300 else text
            formatted_results.append(SearchResult(
                name=result.payload["name"],
                legislation=result.payload["legislation"],
                text_snippet=snippet,
                score=result.score
            ))
        return formatted_results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def root():
    return {"message": "Assistant RAO Search Engine — Qdrant + Qwen Embeddings API"}