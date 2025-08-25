import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    QDRANT_URL = os.getenv("QDRANT_URL")
    QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
    EMBEDDING_MODEL_URL = os.getenv("EMBEDDING_MODEL_URL")
    EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY")
    EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", 1024))
    COLLECTION_NAME = os.getenv("COLLECTION_NAME", "documents")
    API_KEY = os.getenv("API_KEY")