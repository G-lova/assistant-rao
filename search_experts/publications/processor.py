"""Обработка очереди источников публикаций: скачивание, чтение, разбор, проверка авторства, эмбеддинги.

Источник берётся из очереди (``pending``, неудавшиеся — через ``settings.EXPERT_PUB_RETRY_HOURS``), разбирается
и записывается целиком: публикации источника заменяются результатом. Ошибка одного источника не останавливает
остальные (``failed`` с причиной, повтор позже). За запуск обрабатывается не больше ``EXPERT_PUB_BATCH``
источников и не дольше ``EXPERT_PUB_BUDGET_SEC`` секунд; не начатые возвращаются в очередь.
"""
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from configs import settings
from configs.logger import get_logger
from knowledge_store import documents
from search_experts.publications import extract, repo
from search_experts.publications.authors import author_matches
from search_experts.publications.links import extract_doi

logger = get_logger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; RAO-expert-publications/1.0)"
MAX_HTML_BYTES = 3 * 1024 * 1024
EMBED_BATCH = 64


class SourceError(Exception):
    """Источник не удалось разобрать (недоступен, нет текста, ответ без данных)."""


class PublicationProcessor:
    """Разбирает источники очереди окружения и пишет публикации во внутреннюю БД.

    Зависимости создаются лениво (модель, чтение файлов, облака, эмбеддинги), поэтому в тестах их можно подменить.

    Args:
        env: Окружение (``dev`` / ``stage`` / ``prod`` / ``neuro``).
        http_manager: ``HTTPClientManager`` в открытом контексте.
        connection: Фабрика соединений с внутренней БД (по умолчанию ``knowledge_store.db.connection``).
    """

    def __init__(self, env: str, http_manager, connection: Optional[Callable] = None):
        """Запоминает окружение и зависимости."""
        self.env = env
        self.http_manager = http_manager
        if connection is None:
            from knowledge_store.db import connection as default_connection
            connection = default_connection
        self.connection = connection
        self._llm = None
        self._file_reader = None
        self._cloud_parser = None
        self._embedder = None
        self.embedding_model: Optional[str] = None

    # ---------------------------------------------------------------- зависимости

    @property
    def llm(self):
        """Обёртка модели с JSON-ответом (``LLMJson`` поверх ``get_llm()``)."""
        if self._llm is None:
            from configs.llm_client import get_llm
            from risk_monitoring.llm_json import LLMJson
            client, model = get_llm()
            self._llm = LLMJson(client, model, rate=1.0, concurrency=2)
        return self._llm

    @property
    def file_reader(self):
        """Чтение файлов с OCR (``configs.file_reader.FileReader``)."""
        if self._file_reader is None:
            from configs.file_reader import FileReader
            from configs.llm_client import get_llm
            self._file_reader = FileReader(*get_llm())
        return self._file_reader

    @property
    def cloud_parser(self):
        """Скачивание файлов портала, облаков и прямых ссылок (``configs.parsing.CloudStorageParser``)."""
        if self._cloud_parser is None:
            from configs.parsing import CloudStorageParser
            self._cloud_parser = CloudStorageParser(self.http_manager, None)
        return self._cloud_parser

    @property
    def embedder(self):
        """Клиент эмбеддингов (та же модель, что у сходства анкеты в скоринге)."""
        if self._embedder is None:
            from configs.config import Config
            from configs.embedding_client import EmbeddingClient
            cfg = Config.get_embedding_config()
            self.embedding_model = cfg.model
            self._embedder = EmbeddingClient(cfg.api_url, cfg.api_key, cfg.model, batch_size=cfg.batch_size,
                                             http_manager=self.http_manager)
        return self._embedder

    # ---------------------------------------------------------------- очередь

    async def process_batch(self, limit: Optional[int] = None, budget_sec: Optional[float] = None) -> Dict[str, Any]:
        """Обрабатывает пачку источников очереди.

        Args:
            limit: Сколько источников взять (по умолчанию ``EXPERT_PUB_BATCH``).
            budget_sec: Время на пачку (по умолчанию ``EXPERT_PUB_BUDGET_SEC``).

        Returns:
            Dict[str, Any]: ``claimed``, ``done``, ``empty``, ``failed``, ``released``, ``publications``.
        """
        limit = int(limit or settings.EXPERT_PUB_BATCH)
        budget = float(budget_sec or settings.EXPERT_PUB_BUDGET_SEC)
        started = time.monotonic()
        async with self.connection() as conn:
            sources = await repo.claim_sources(conn, self.env, limit, settings.EXPERT_PUB_RETRY_HOURS,
                                               settings.EXPERT_PUB_MAX_ATTEMPTS)
        st = dict(claimed=len(sources), done=0, empty=0, failed=0, released=0, publications=0)
        for i, source in enumerate(sources):
            if time.monotonic() - started > budget:
                rest = [s["id"] for s in sources[i:]]
                async with self.connection() as conn:
                    await repo.release_sources(conn, rest)
                st["released"] = len(rest)
                break
            try:
                pubs, document_id = await self.process_source(source)
                async with self.connection() as conn:
                    ids = await repo.save_source_result(conn, source, pubs, document_id)
                st["done" if pubs else "empty"] += 1
                st["publications"] += len(ids)
                if ids:
                    await self.embed_publications([source["id"]])
            except Exception as e:  # noqa: BLE001 — источник неудачен, остальные продолжаются
                logger.warning(f"Публикации эксперта {source['expert_id']}: источник {source['id']} "
                               f"({source['link_type']}, {source.get('url')}) не разобран: {type(e).__name__}: {e}")
                async with self.connection() as conn:
                    await repo.fail_source(conn, source["id"], f"{type(e).__name__}: {e}")
                st["failed"] += 1
        return st

    async def process_source(self, source: Dict[str, Any]) -> Tuple[List[dict], Optional[int]]:
        """Разбирает один источник.

        Args:
            source: Строка ``expert_pub_sources`` с ``expert_name``.

        Returns:
            Tuple[List[dict], Optional[int]]: Записи публикаций с ``author_verified`` и ``ai_documents.id`` (для файлов).

        Raises:
            SourceError: Источник недоступен или не содержит данных.
        """
        link_type = source.get("link_type")
        document_id = None
        if link_type in ("media", "file", "cloud"):
            pubs, document_id = await self.from_files(source["url"], source.get("file_name"), source["collection"],
                                                      portal_media=link_type == "media")
        elif link_type == "doi":
            pubs = await self.from_doi(extract_doi(source["url"]))
        elif link_type == "journal":
            pubs, document_id = await self.from_journal_page(source["url"], source["collection"])
        else:
            raise SourceError(f"тип ссылки {link_type} не обрабатывается")
        pubs = extract.dedupe(pubs, settings.EXPERT_PUB_MAX_PER_SOURCE)
        for p in pubs:
            p["author_verified"] = author_matches(source.get("expert_name"), p.get("authors"))
        return pubs, document_id

    # ---------------------------------------------------------------- файлы

    async def from_files(self, url: str, file_name: Optional[str], collection: str,
                         portal_media: bool = False) -> Tuple[List[dict], Optional[int]]:
        """Скачивает файл(ы) по ссылке и разбирает их.

        Args:
            url: Адрес файла портала, облака или прямая ссылка.
            file_name: Имя файла анкеты (подсказка модели).
            collection: ``publication`` | ``monograph``.
            portal_media: Файл анкеты на портале — скачивается через ``/api/media`` с ключом портала
                (``CloudStorageParser.download_media``, общий ограничитель частоты запросов к порталу).

        Returns:
            Tuple[List[dict], Optional[int]]: Публикации и ``ai_documents.id`` первого файла.
        """
        if portal_media:
            one = await self.cloud_parser.download_media(url, "expert-publications")
            result = {"status": one.get("status"), "files": [one], "error": one.get("error")}
        else:
            result = await self.cloud_parser.parse_cloud_storage_link(url, "expert-publications")
        if result.get("status") != "success" or not result.get("files"):
            raise SourceError(f"файл не скачан: {result.get('error') or 'нет файлов'}")
        pubs: List[dict] = []
        first_doc = None
        errors = []
        for f in result["files"]:
            path = f.get("file_path")
            name = f.get("filename") or file_name
            try:
                if not path or not os.path.exists(path):
                    raise SourceError("нет скачанного файла")
                doc_id, parsed = await self.parse_file(path, name or os.path.basename(path), collection)
                first_doc = first_doc or doc_id
                pubs.extend(parsed)
            except Exception as e:  # noqa: BLE001 — другие файлы облачной папки разбираются
                errors.append(f"{name}: {e}")
            finally:
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        if not pubs and errors and first_doc is None:
            raise SourceError("; ".join(errors)[:500])
        return pubs, first_doc

    async def parse_file(self, path: str, name: str, collection: str) -> Tuple[int, List[dict]]:
        """Текст файла (общий документ по SHA-256) и его разбор (кэш по документу и версии промпта).

        Args:
            path: Локальный путь.
            name: Имя файла.
            collection: ``publication`` | ``monograph``.

        Returns:
            Tuple[int, List[dict]]: ``ai_documents.id`` и записи публикаций.
        """
        from knowledge_store.pages import split_pages

        sha = documents.file_sha256(path)
        async with self.connection() as conn:
            found = await documents.find_text(conn, sha)
        if found:
            doc_id, text = found["id"], found["text"]
        else:
            text = await self.file_reader.read_file(path, name)
            if not text or "[Нет читаемого текста]" in text:
                raise SourceError("не удалось извлечь текст")
            async with self.connection() as conn:
                doc_id = await documents.save_text(conn, sha, path, name, text, documents.READER_FILE, len(split_pages(text)))

        version, model = extract.prompt_version(), self.llm.model
        async with self.connection() as conn:
            rows = await conn.fetch(documents.SQL_GET_ANALYSIS, int(doc_id), extract.ANALYSIS_TYPE, version, model or "")
        if rows:
            cached = documents._load(dict(rows[0])["result"])
            return int(doc_id), [p for p in (extract.normalize_publication(r) for r in cached.get("publications", [])) if p]

        parsed = await extract.parse_text_with_llm(
            self.llm, text, name, collection, settings.EXPERT_PUB_LLM_CHUNK_CHARS, settings.EXPERT_PUB_LLM_MAX_CHUNKS,
            settings.EXTRACTION_MAX_TOKENS)
        import json
        async with self.connection() as conn:
            await conn.execute(documents.SQL_SAVE_ANALYSIS, int(doc_id), extract.ANALYSIS_TYPE, version, model or "",
                               json.dumps(documents.strip_nul(parsed), ensure_ascii=False, default=str))
        return int(doc_id), parsed["publications"]

    # ---------------------------------------------------------------- DOI и страницы журналов

    async def _get(self, url: str, accept: str = "*/*") -> Tuple[int, str, bytes]:
        """GET-запрос с таймаутом ``EXPERT_PUB_HTTP_TIMEOUT`` и ограничением размера ответа.

        Returns:
            Tuple[int, str, bytes]: Код ответа, Content-Type, тело (не больше 3 МБ).
        """
        import aiohttp

        session = self.http_manager.get_session()
        async with session.get(url, headers={"User-Agent": USER_AGENT, "Accept": accept},
                               timeout=aiohttp.ClientTimeout(total=settings.EXPERT_PUB_HTTP_TIMEOUT),
                               allow_redirects=True) as resp:
            body = await resp.content.read(MAX_HTML_BYTES)
            return resp.status, resp.headers.get("Content-Type", ""), body

    async def from_doi(self, doi: Optional[str]) -> List[dict]:
        """Метаданные публикации по DOI: OpenAlex, при отказе — Crossref.

        Args:
            doi: DOI.

        Returns:
            List[dict]: Одна запись (или пусто, если DOI нигде не найден).

        Raises:
            SourceError: Нет DOI или оба сервиса недоступны.
        """
        import json

        if not doi:
            raise SourceError("в ссылке нет DOI")
        errors = []
        try:
            status, _, body = await self._get(settings.EXPERT_PUB_OPENALEX_URL + doi, "application/json")
            if status == 200:
                pub = extract.from_openalex(json.loads(body))
                if pub:
                    return [pub]
            errors.append(f"OpenAlex {status}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"OpenAlex {type(e).__name__}")
        try:
            status, _, body = await self._get(settings.EXPERT_PUB_CROSSREF_URL + doi, "application/json")
            if status == 200:
                pub = extract.from_crossref(json.loads(body).get("message") or {})
                return [pub] if pub else []
            if status == 404:
                return []
            errors.append(f"Crossref {status}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"Crossref {type(e).__name__}")
        raise SourceError("метаданные DOI недоступны: " + ", ".join(errors))

    async def from_journal_page(self, url: str, collection: str) -> Tuple[List[dict], Optional[int]]:
        """Публикация со страницы журнала / репозитория по мета-тегам ``citation_*``.

        Ссылка, отдающая файл (PDF / DOC), разбирается как файл. DOI на странице дополняет запись из OpenAlex
        (аннотация, ключевые слова), если их нет в мета-тегах.

        Args:
            url: Ссылка.
            collection: ``publication`` | ``monograph``.

        Returns:
            Tuple[List[dict], Optional[int]]: Публикации и ``ai_documents.id`` (если ссылка вела на файл).

        Raises:
            SourceError: Страница недоступна.
        """
        status, ctype, body = await self._get(url, "text/html,application/xhtml+xml,*/*")
        if status != 200:
            raise SourceError(f"страница недоступна: HTTP {status}")
        if "html" not in ctype.lower():
            if any(t in ctype.lower() for t in ("pdf", "msword", "officedocument", "octet-stream")):
                return await self.from_files(url, None, collection)
            return [], None
        page = body.decode("utf-8", errors="replace")
        pub, _pdf = extract.from_html_meta(page)
        if not pub:
            return [], None
        if pub.get("doi") and not pub.get("abstract"):
            try:
                extra = await self.from_doi(pub["doi"])
                if extra:
                    for k in ("abstract", "keywords", "year", "venue"):
                        pub[k] = pub.get(k) or extra[0].get(k)
                    pub["authors"] = pub.get("authors") or extra[0].get("authors")
            except Exception:  # noqa: BLE001 — запись со страницы уже есть
                pass
        return [pub], None

    # ---------------------------------------------------------------- эмбеддинги

    async def embed_publications(self, source_ids: Optional[List[int]] = None, limit: int = 2000) -> int:
        """Считает эмбеддинги публикаций без вектора текущей модели.

        Args:
            source_ids: Только публикации этих источников (``None`` — все окружения ``env``).
            limit: Максимум публикаций за вызов.

        Returns:
            int: Сколько векторов записано.
        """
        embedder = self.embedder
        async with self.connection() as conn:
            rows = await repo.publications_without_embedding(conn, self.env, self.embedding_model, limit, source_ids)
        done = 0
        for i in range(0, len(rows), EMBED_BATCH):
            batch = rows[i:i + EMBED_BATCH]
            texts = [extract.embed_text(r) for r in batch]
            vectors = await embedder.get_embeddings(texts)
            if len(vectors) != len(batch):
                logger.warning(f"Эмбеддинги публикаций: получено {len(vectors)} из {len(batch)}")
                continue
            async with self.connection() as conn:
                await repo.save_embeddings(conn, [(r["id"], extract.text_sha(t), v) for r, t, v in zip(batch, texts, vectors)],
                                           self.embedding_model)
            done += len(batch)
        return done
