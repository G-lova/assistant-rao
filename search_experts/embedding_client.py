"""Совместимость: клиент эмбеддингов перенесён в ``configs/embedding_client.py``.

Старый путь импорта сохранён для ``search_experts/pipeline.py``, ``knowledge_store/indexer.py``,
``risk_monitoring/file_processor.py`` и ``risk_monitoring/xml_processor.py``.
"""
from configs.embedding_client import EmbeddingClient, EmbeddingError  # noqa: F401
