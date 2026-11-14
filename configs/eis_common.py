"""Общие методы SOAP-сервиса ЕИС для ``CloudStorageParser`` (``configs/parsing.py``) и ``EISParser``
(``configs/eis_parsing.py``).

Раньше эти методы были скопированы в оба класса и расходились мелочами (в ``EISParser`` — разбор ответа
«нет данных», в ``CloudStorageParser`` — ``procurement_number`` в ответе и имя файла по умолчанию). Здесь — одна
версия с обоими улучшениями. Класс-примесь ожидает у наследника ``http_manager`` (``HTTPClientManager``) и
``eis_token``.
"""
import asyncio
import json
import logging
import os
import re
import tempfile
import uuid
from typing import Dict, Optional, Union
from urllib.parse import unquote

import aiohttp

from configs.eis_archive import download_to_tempfile, extract_zip, remove_quietly
from configs.logger import get_logger
from configs.retry_utils import EIS_RETRY_CONFIG, EISArchiveNotReadyError, async_retry

logger = get_logger(__name__)


class EISSoapMixin:
    """Разбор ответов SOAP ЕИС, скачивание и распаковка архивов, имена файлов (общее для двух парсеров)."""

    @staticmethod
    def _eis_no_data(response_dict: Dict) -> bool:
        """Ответ ЕИС «данных нет» (``dataInfo/noData = true``) — штатная ситуация, а не ошибка."""
        try:
            body = (response_dict.get('soap:Envelope') or {}).get('soap:Body') or {}
            for key, value in body.items():
                if 'Response' in key and isinstance(value, dict):
                    info = value.get('dataInfo')
                    if isinstance(info, dict) and str(info.get('noData', '')).lower() == 'true':
                        return True
        except AttributeError:
            return False
        return False

    def _extract_eis_archive_url(self, response_dict: Dict) -> Optional[str]:
        """Извлекает URL архива из ответа SOAP API ЕИС"""
        try:
            if logger.isEnabledFor(logging.DEBUG):   # json.dumps — только если запись действительно пишется
                logger.debug(f"Попытка извлечения URL архива из ответа: {json.dumps(response_dict, indent=2, ensure_ascii=False)}")

            # Основная структура SOAP ответа
            envelope = response_dict.get('soap:Envelope', {})
            body = envelope.get('soap:Body', {})

            # Ищем в различных возможных структурах ответа

            for key, value in body.items():
                if 'Response' in key:
                    data_info = value.get('dataInfo', {})

                    # Случай 1: dataInfo - строка (прямой URL)
                    if isinstance(data_info, str) and data_info.startswith('http'):
                        return data_info

                    # Случай 2: archiveUrl внутри dataInfo (строка или список)
                    if isinstance(data_info, dict):
                        archive_url = data_info.get('archiveUrl')
                        if archive_url:
                            if isinstance(archive_url, str) and archive_url.startswith('http'):
                                return archive_url
                            elif isinstance(archive_url, list):
                                # Возвращаем список валидных URL
                                return [url for url in archive_url if isinstance(url, str) and url.startswith('http')]                            

                    # Случай 3: archiveUrl на уровне ответа (строка или список)
                    archive_url = value.get('archiveUrl')
                    if archive_url:
                        if isinstance(archive_url, str) and archive_url.startswith('http'):
                            return archive_url
                        elif isinstance(archive_url, list):
                            return [url for url in archive_url if isinstance(url, str) and url.startswith('http')]

                    # Случай 4: archiveUrl внутри nsiArchiveInfo (для getNsiResponse)
                    nsi_archive = data_info.get('nsiArchiveInfo', {})
                    if isinstance(nsi_archive, dict):
                        archive_url = nsi_archive.get('archiveUrl')
                        if archive_url and isinstance(archive_url, str) and archive_url.startswith('http'):
                            return archive_url
                    elif isinstance(nsi_archive, list):
                        return [archive.get('archiveUrl') for archive in nsi_archive if isinstance(archive.get('archiveUrl', ''), str) and archive.get('archiveUrl', '').startswith('http')]
                            
                    
            if self._eis_no_data(response_dict):
                logger.info("ЕИС: данных за запрошенный период нет (noData)")
            else:
                logger.error("Не удалось найти URL архива в ответе ЕИС")
            return None

        except Exception as e:
            logger.error(f"Ошибка извлечения URL архива: {str(e)}")
            return None

    @async_retry(EIS_RETRY_CONFIG)
    async def _download_eis_archive(self, archive_url: str, identifier: str, procurement_id: str = None) -> Dict:
        """Скачивает и обрабатывает архив из ЕИС"""
        try:
            session = self.http_manager.get_session()
            headers = {
                'individualPerson_token': self.eis_token,
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
        
            logger.info(f"→ Запрос архива: {archive_url[:100]}...")
            
            async with session.get(
                archive_url, 
                headers=headers, 
                timeout=aiohttp.ClientTimeout(total=120)
            ) as response:
            
                # Статус 425 — превращаем в исключение для retry
                if response.status == 425:
                    raise EISArchiveNotReadyError(
                        f"Архив ещё не готов (статус 425), ссылка: {archive_url[:80]}..."
                    )
                
                # Другие ошибки HTTP
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"❌ Ошибка скачивания: {response.status}, тело: {error_text[:300]}")
                    return {
                        "status": "error",
                        "procurement_number": identifier,
                        "error": f"Не удалось скачать архив ЕИС: статус {response.status}"
                    }
                
                # Успех: архив пишется на диск порциями, а не читается в память целиком
                archive_path, size = await download_to_tempfile(response)
                logger.info(f"Архив скачан: {size} байт")

            try:
                return await self._process_eis_archive(archive_path, identifier, archive_url, procurement_id)
            finally:
                remove_quietly(archive_path)
                        
        except EISArchiveNotReadyError:
            # Переподнимаем, чтобы декоратор retry мог перехватить
            raise

        except Exception as e:
            # Все остальные ошибки — логируем и возвращаем как error-ответ
            logger.error(f"❌ Исключение при скачивании архива ЕИС: {type(e).__name__}: {str(e)}", exc_info=True)
            return {
                "status": "error", 
                "procurement_number": identifier,
                "error": f"Ошибка скачивания архива ЕИС: {str(e)}"
            }

    async def _process_eis_archive(self, archive: Union[str, bytes], identifier: str,
                                 original_url: str, procurement_id: str = None) -> Dict:
        """Распаковывает ZIP-архив ЕИС во временные файлы (потоком, вне цикла событий).

        Args:
            archive: Путь к архиву на диске (так его передаёт ``_download_eis_archive``) или содержимое архива
                (прежний формат вызова — содержимое сначала записывается во временный файл).
            identifier: Реестровый номер / идентификатор запроса.
            original_url: Ссылка на архив.
            procurement_id: ID экспертизы.

        Returns:
            Dict: ``status``, ``files`` (``file_path`` — временный файл), ``archive_size``, ``file_count``; при
            ошибке — ``status = "error"`` и ``error``. Файлы читаются с диска по ``file_path`` (раньше в
            ``eis_parsing`` к каждому добавлялась base64-копия содержимого).
        """
        tmp_archive = None
        try:
            if isinstance(archive, (bytes, bytearray)):
                fd, tmp_archive = tempfile.mkstemp(suffix=".zip")
                with os.fdopen(fd, "wb") as f:
                    f.write(archive)
                archive = tmp_archive
            archive_size = os.path.getsize(archive)
            files = await asyncio.to_thread(extract_zip, archive, original_url, procurement_id)

            if not files:
                return {
                    "status": "error",
                    "procurement_number": identifier,
                    "error": "В архиве ЕИС не найдено файлов"
                }

            return {
                "status": "success",
                "procurement_number": identifier,
                "files": files,
                "source": "eis_soap",
                "original_url": original_url,
                "procurement_id": procurement_id,
                "archive_size": archive_size,
                "file_count": len(files)
            }

        except Exception as e:
            logger.error(f"Ошибка обработки архива ЕИС: {str(e)}")
            return {
                "status": "error",
                "error": f"Ошибка обработки архива ЕИС: {str(e)}"
            }
        finally:
            remove_quietly(tmp_archive)

    def _sanitize_filename(self, filename: str) -> str:
        """Очищает имя файла от недопустимых символов"""
        # Удаляем проблемные символы для разных ОС
        sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '_', filename)
        # Ограничиваем длину
        if len(sanitized) > 200:
            name, ext = os.path.splitext(sanitized)
            sanitized = name[:190] + ext
        return sanitized.strip() or f"file_{uuid.uuid4().hex[:8]}"

    def _extract_eis_error_info(self, response_dict: Dict) -> Optional[str]:
        """Извлекает информацию об ошибке из ответа ЕИС"""
        try:
            envelope = response_dict.get('soap:Envelope', {})
            body = envelope.get('soap:Body', {})
            
            for key, value in body.items():
                if 'Response' in key:
                    data_info = value.get('dataInfo', {})
                    error_info = data_info.get('errorInfo', {})
                    if error_info:
                        code = error_info.get('code', '')
                        message = error_info.get('message', '')
                        return f"Код {code}: {message}"
            
            return None
        except Exception as e:
            logger.error(f"Ошибка извлечения информации об ошибке: {str(e)}")
            return None

    def extract_urls_from_dict(self, data):
        """Рекурсивно собирает все значения из словаря (или списка), 
        где ключ заканчивается на 'url' (регистронезависимо)."""
        urls = []
        
        if isinstance(data, dict):
            for key, value in data.items():
                # Проверяем окончание ключа (учитываем namespace-префиксы, например ns2:url)
                if isinstance(key, str) and key.lower().endswith('url'):
                    if isinstance(value, str):
                        urls.append(value.strip())
                
                # Рекурсивно обходим вложенные значения
                urls.extend(self.extract_urls_from_dict(value))
                
        elif isinstance(data, list):
            for item in data:
                urls.extend(self.extract_urls_from_dict(item))
                
        return urls

    def _extract_filename_from_headers(self, headers: dict, fallback_url: str) -> str:
        """
        Извлекает имя файла из заголовка Content-Disposition.
        
        Поддерживает:
        - filename="name.docx"
        - filename*=UTF-8''name.docx (RFC 5987)
        """
        content_disposition = headers.get('Content-Disposition', '')
        
        if not content_disposition:
            # Fallback: извлекаем из URL
            return self._extract_filename_from_url(fallback_url)
        
        # Пробуем filename* (RFC 5987, приоритетный)
        # Пример: filename*=UTF-8''%D0%A4%D0%B0%D0%B9%D0%BB.docx
        filename_star_match = re.search(r"filename\*\s*=\s*([^;]+)", content_disposition, re.IGNORECASE)
        if filename_star_match:
            value = filename_star_match.group(1).strip()
            # Формат: UTF-8''encoded_name или utf-8''encoded_name
            if "''" in value:
                encoded_part = value.split("''", 1)[1]
                try:
                    return unquote(encoded_part)
                except:
                    pass
        
        # Пробуем обычный filename
        # Пример: filename="%D0%A4%D0%B0%D0%B9%D0%BB.docx"
        filename_match = re.search(r'filename\s*=\s*["\']?([^";\'\n]+)["\']?', content_disposition, re.IGNORECASE)
        if filename_match:
            filename = filename_match.group(1).strip()
            try:
                # URL-декодируем (кириллица в заголовках часто закодирована)
                return unquote(filename)
            except:
                return filename
        
        # Fallback: из URL
        return self._extract_filename_from_url(fallback_url)
