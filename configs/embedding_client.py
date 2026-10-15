"""Клиент сервиса эмбеддингов (OpenAI-совместимый API, Qwen3-Embedding-0.6B).

Перенесён из ``search_experts/embedding_client.py`` и доработан:

* ``EmbeddingError`` — единое исключение клиента;
* строгий режим ``get_embeddings(texts, strict=True)``: длина ответа всегда равна числу текстов,
  иначе исключение (нужен там, где вектор сопоставляется с конкретным чанком — ``AIFileAnalyzer``);
* мягкий режим (по умолчанию) сохраняет прежнее поведение: упавшие пакеты пропускаются;
* повторы с экспоненциальной задержкой, рекурсивное деление пакета при HTTP 400;
* общий ``HTTPClientManager`` (сессия не создаётся на каждый запрос).

Старый путь ``search_experts.embedding_client`` оставлен реэкспортом этого модуля.
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Не удалось получить эмбеддинги (сеть, формат ответа, несовпадение числа векторов)."""


class EmbeddingClient:
    """Получение векторных эмбеддингов текстов через внешний API.

    Args:
        api_url: URL эндпоинта эмбеддингов.
        api_key: Ключ (передаётся в ``Authorization: Bearer`` и ``X-API-Key``).
        model: Название модели.
        batch_size: Размер пакета текстов в одном запросе.
        http_manager: Общий ``HTTPClientManager`` (метод ``get_session()``).
        max_retries: Число попыток на пакет.
        concurrency: Максимум одновременных запросов к сервису.
        timeout: Общий таймаут запроса, секунд.
    """

    def __init__(
        self,
        api_url: str,
        api_key: str,
        model: str,
        batch_size: int,
        http_manager: Any,
        max_retries: int = 3,
        concurrency: int = 10,
        timeout: float = 30.0,
    ):
        """Сохраняет настройки клиента; соединение берётся из ``http_manager`` при каждом запросе."""
        self.api_url = (api_url or "").rstrip("/")
        self.api_key = api_key
        self.model = model
        self.batch_size = max(1, int(batch_size or 1))
        self.http_manager = http_manager
        self.max_retries = max(1, int(max_retries))
        self.timeout = timeout
        self._semaphore = asyncio.Semaphore(concurrency)
        logger.info(f"EmbeddingClient: {self.api_url}, модель {model}, пакет {self.batch_size}")

    async def get_embeddings(self, texts: List[str], strict: bool = False) -> np.ndarray:
        """Возвращает эмбеддинги для списка текстов.

        Args:
            texts: Тексты (чанки).
            strict: ``True`` — любой упавший пакет или несовпадение числа векторов дают ``EmbeddingError``;
                ``False`` — упавшие пакеты пропускаются (прежнее поведение, порядок векторов не гарантирует
                соответствие текстам).

        Returns:
            np.ndarray: Матрица ``(len(texts), dim)`` в строгом режиме; в мягком — по числу полученных векторов.

        Raises:
            EmbeddingError: Только в строгом режиме.
        """
        if not texts:
            return np.array([])
        batches = [texts[i:i + self.batch_size] for i in range(0, len(texts), self.batch_size)]
        results = await asyncio.gather(*(self._get_batch(b) for b in batches), return_exceptions=True)

        vectors: List[List[float]] = []
        for batch, res in zip(batches, results):
            if isinstance(res, BaseException):
                if strict:
                    raise EmbeddingError(f"Пакет из {len(batch)} текстов не обработан: {res}") from res
                logger.error(f"Ошибка в пакете эмбеддингов: {res}")
                continue
            if strict and len(res) != len(batch):
                raise EmbeddingError(f"Сервис вернул {len(res)} векторов на {len(batch)} текстов")
            vectors.extend(res)

        if strict and len(vectors) != len(texts):
            raise EmbeddingError(f"Получено {len(vectors)} векторов на {len(texts)} текстов")
        return np.array(vectors) if vectors else np.array([])

    async def _get_batch(self, texts: List[str]) -> List[List[float]]:
        """Эмбеддинги одного пакета с повторами; при HTTP 400 пакет делится пополам.

        Args:
            texts: Тексты пакета.

        Returns:
            List[List[float]]: Векторы в порядке текстов.

        Raises:
            EmbeddingError: Исчерпаны попытки или неизвестный формат ответа.
        """
        from aiohttp import ClientTimeout  # ленивый импорт: модуль используется и в тестах без aiohttp

        payload = {"model": self.model, "input": texts[0] if len(texts) == 1 else texts}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key

        last_error: Optional[BaseException] = None
        for attempt in range(self.max_retries):
            try:
                async with self._semaphore:
                    session = self.http_manager.get_session()
                    async with session.post(self.api_url, json=payload, headers=headers,
                                            timeout=ClientTimeout(total=self.timeout, connect=5)) as resp:
                        if resp.status == 400 and len(texts) > 1:
                            logger.warning(f"Эмбеддинги: 400 на пакете из {len(texts)}, делим пополам")
                            mid = len(texts) // 2
                            return await self._get_batch(texts[:mid]) + await self._get_batch(texts[mid:])
                        if resp.status >= 400:
                            raise EmbeddingError(f"HTTP {resp.status}: {(await resp.text())[:200]}")
                        data = await resp.json(content_type=None)
                vectors = self.extract_embeddings(data)
                if vectors is None:
                    raise EmbeddingError(f"Неизвестный формат ответа: {str(data)[:200]}")
                return vectors
            except Exception as e:  # noqa: BLE001 — повторяем любые сбои сети и формата
                last_error = e
                if attempt < self.max_retries - 1:
                    wait = 2 ** attempt
                    logger.warning(f"Эмбеддинги: попытка {attempt + 1}/{self.max_retries} не удалась ({e}), ждём {wait} с")
                    await asyncio.sleep(wait)
        raise EmbeddingError(f"Исчерпаны попытки ({self.max_retries}): {last_error}") from last_error

    @staticmethod
    def extract_embeddings(data: Dict[str, Any]) -> Optional[List[List[float]]]:
        """Достаёт векторы из ответа сервиса (поддерживает три формата).

        Args:
            data: JSON-ответ: ``{"embedding": [...]}``, ``{"embeddings": [[...]]}`` или
                OpenAI-формат ``{"data": [{"embedding": [...], "index": i}]}``.

        Returns:
            Optional[List[List[float]]]: Векторы или ``None``, если формат не распознан.
        """
        if not isinstance(data, dict):
            return None
        if isinstance(data.get("embedding"), list):
            return [data["embedding"]]
        if isinstance(data.get("embeddings"), list):
            return data["embeddings"]
        if isinstance(data.get("data"), list):
            items = sorted(data["data"], key=lambda x: x.get("index", 0)) if all(
                isinstance(x, dict) and "index" in x for x in data["data"]) else data["data"]
            return [item["embedding"] for item in items if isinstance(item, dict) and "embedding" in item]
        return None

    # Совместимость со старым именем метода
    _extract_embeddings = extract_embeddings
