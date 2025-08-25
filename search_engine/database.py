from qdrant_client import QdrantClient
from qdrant_client.http import models
from dotenv import load_dotenv
import os

load_dotenv()

class QdrantDatabase:
    def __init__(self):
        self.client = QdrantClient(
            host=os.getenv("QDRANT_HOST"),
            port=int(os.getenv("QDRANT_PORT")),
            api_key=os.getenv("QDRANT_API_KEY"),
            https=False,
        )
        self.collection_name = os.getenv("COLLECTION_NAME")

    def create_collection(self, vector_size):
        self.client.recreate_collection(
            collection_name=self.collection_name,
            vectors_config=models.VectorParams(
                size=vector_size,
                distance=models.Distance.COSINE
            )
        )

    def upload_document(self, document_id, vector, payload):
        self.client.upsert(
            collection_name=self.collection_name,
            points=[
                models.PointStruct(
                    id=document_id,
                    vector=vector,
                    payload=payload
                )
            ]
        )

    def search_similar(self, vector, limit=3):
        return self.client.search(
            collection_name=self.collection_name,
            query_vector=vector,
            limit=limit,
            with_payload=True
        )