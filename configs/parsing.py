import os
import re
import logging
import tempfile

import requests
import pandas as pd
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

from configs.utils import read_file
from src.evaluator import analyze_document_chunks, split_large_text


logger = logging.getLogger(__name__)


class CloudStorageParser:
    """
    Парсер для извлечения и обработки файлов из популярных облачных хранилищ.
    Поддерживает Google Drive, Google Docs, Яндекс.Диск и Облако Mail.ru.
    """
    
    def __init__(self):
        """
        Инициализирует парсер с поддерживаемыми доменами и соответствующими методами обработки.
        """
        self.supported_domains = {
            'drive.google.com': self._parse_google_drive,
            'docs.google.com': self._parse_google_docs,
            'yadi.sk': self._parse_yandex_disk,
            'disk.yandex.ru': self._parse_yandex_disk,
            'cloud.mail.ru': self._parse_mail_cloud,
            'files.mail.ru': self._parse_mail_cloud
        }


    def is_cloud_link(self, url: str) -> bool:
        """
        Проверяет, является ли URL ссылкой на поддерживаемое облачное хранилище.

        Args:
            url (str): URL для проверки.

        Returns:
            bool: True, если домен поддерживается, иначе False.
        """
        parsed = urlparse(url)
        return parsed.netloc in self.supported_domains


    async def parse_cloud_link(self, url: str, procurement_id: str = None) -> Dict:
        """
        Асинхронно обрабатывает ссылку на облачное хранилище и возвращает результат анализа файла.

        Args:
            url (str): Ссылка на файл в облаке.
            procurement_id (str, optional): Идентификатор закупки для логирования и метаданных. Defaults to None.

        Returns:
            Dict: Результат обработки с ключами 'status', 'error' (при ошибке) или 'analysis' (при успехе).
        """
        if not self.is_cloud_link(url):
            return {
                "status": "error",
                "error": "Неподдерживаемый домен облачного хранилища"
            }
        
        parsed = urlparse(url)
        parser_func = self.supported_domains.get(parsed.netloc)
        
        if parser_func:
            return await parser_func(url, procurement_id)
        else:
            return {
                "status": "error", 
                "error": f"Парсер для домена {parsed.netloc} не реализован"
            }


    async def _parse_google_drive(self, url: str, procurement_id: str = None) -> Dict:
        """
        Обрабатывает ссылку на файл Google Drive: извлекает ID, формирует URL для скачивания и запускает обработку.

        Args:
            url (str): Ссылка на файл в Google Drive.
            procurement_id (str, optional): Идентификатор закупки. Defaults to None.

        Returns:
            Dict: Результат обработки файла.
        """
        try:
            # Извлекаем ID файла из URL
            file_id = self._extract_google_drive_file_id(url)
            if not file_id:
                return {
                    "status": "error",
                    "error": "Не удалось извлечь ID файла из Google Drive ссылки"
                }
            
            # Создаем прямую ссылку для скачивания
            download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
            
            # Скачиваем файл
            return await self._download_and_process_file(
                download_url, 
                "google_drive", 
                file_id,
                procurement_id
            )
            
        except Exception as e:
            logger.error(f"Ошибка парсинга Google Drive: {str(e)}")
            return {
                "status": "error",
                "error": f"Ошибка парсинга Google Drive: {str(e)}"
            }


    async def _parse_google_docs(self, url: str, procurement_id: str = None) -> Dict:
        """
        Обрабатывает ссылку на Google Docs, Sheets или Slides: определяет тип документа,
        формирует URL экспорта и запускает обработку.

        Args:
            url (str): Ссылка на документ Google.
            procurement_id (str, optional): Идентификатор закупки. Defaults to None.

        Returns:
            Dict: Результат обработки документа.
        """
        try:
            # Извлекаем ID документа и определяем тип
            doc_id, doc_type = self._extract_google_docs_id_and_type(url)
            if not doc_id:
                return {
                    "status": "error",
                    "error": "Не удалось извлечь ID документа Google Docs"
                }
            
            # Создаем ссылку для экспорта в зависимости от типа
            if doc_type == "spreadsheets":
                # Для таблиц пробуем разные форматы
                export_url = f"https://docs.google.com/spreadsheets/d/{doc_id}/export?format=xlsx"
                file_extension = ".xlsx"
            elif doc_type == "presentation":
                export_url = f"https://docs.google.com/presentation/d/{doc_id}/export?format=pptx"
                file_extension = ".pptx"
            else:  # document
                export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=pdf"
                file_extension = ".pdf"
            
            return await self._download_and_process_file(
                export_url,
                "google_docs",
                doc_id,
                procurement_id,
                file_extension
            )
            
        except Exception as e:
            logger.error(f"Ошибка парсинга Google Docs: {str(e)}")
            return {
                "status": "error",
                "error": f"Ошибка парсинга Google Docs: {str(e)}"
            }


    def _extract_google_docs_id_and_type(self, url: str) -> Tuple[Optional[str], str]:
        """
        Извлекает ID документа и его тип (документ, таблица, презентация) из URL Google Docs.

        Args:
            url (str): URL документа Google.

        Returns:
            Tuple[Optional[str], str]: Кортеж из ID документа и его типа.
        """
        patterns = [
            (r'/document/d/([a-zA-Z0-9_-]+)', 'document'),
            (r'/spreadsheets/d/([a-zA-Z0-9_-]+)', 'spreadsheets'),
            (r'/presentation/d/([a-zA-Z0-9_-]+)', 'presentation')
        ]

        for pattern, doc_type in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1), doc_type

        return None, 'document'


    async def _parse_yandex_disk(self, url: str, procurement_id: str = None) -> Dict:
        """
        Обрабатывает публичную ссылку на файл Яндекс.Диска: извлекает ресурс, получает прямую ссылку
        для скачивания и запускает обработку.

        Args:
            url (str): Публичная ссылка на файл Яндекс.Диска.
            procurement_id (str, optional): Идентификатор закупки. Defaults to None.

        Returns:
            Dict: Результат обработки файла.
        """
        try:
            # Извлекаем ID ресурса
            resource_id = self._extract_yandex_disk_resource(url)

            # Получаем информацию о файле и прямую ссылку для скачивания
            download_info = await self._get_yandex_disk_download_info(url, resource_id)

            if not download_info:
                return {
                    "status": "error",
                    "error": "Не удалось получить информацию о файле с Яндекс.Диска"
                }

            download_url = download_info['url']
            file_extension = download_info.get('extension', '.bin')

            return await self._download_and_process_file(
                download_url,
                "yandex_disk",
                resource_id,
                procurement_id,
                file_extension
            )

        except Exception as e:
            logger.error(f"Ошибка парсинга Yandex Disk: {str(e)}")
            return {
                "status": "error",
                "error": f"Ошибка парсинга Yandex Disk: {str(e)}"
            }


    async def _get_yandex_disk_download_info(self, url: str, resource_id: str) -> Dict:
        """
        Получает информацию о файле на Яндекс.Диске: прямую ссылку для скачивания и расширение файла.

        Args:
            url (str): Исходная публичная ссылка.
            resource_id (str): Идентификатор ресурса на Яндекс.Диске.

        Returns:
            Dict: Словарь с ключами 'url', 'extension', 'content_type'.
        """
        try:
            # Преобразуем ссылку просмотра в ссылку скачивания
            download_url = self._convert_yandex_disk_to_download(url)

            # Делаем HEAD запрос для получения информации о файле
            head_response = requests.head(download_url, allow_redirects=True, timeout=10)

            if head_response.status_code != 200:
                # Пробуем альтернативный метод
                return await self._try_yandex_disk_alternative_methods(url, resource_id)

            # Получаем информацию из заголовков
            content_type = head_response.headers.get('content-type', '')
            content_disposition = head_response.headers.get('content-disposition', '')

            # Извлекаем расширение из content-disposition
            file_extension = self._extract_extension_from_content_disposition(content_disposition)

            # Если не нашли в content-disposition, определяем по content-type
            if not file_extension:
                file_extension = self._get_extension_from_content_type(content_type)

            # Если все еще нет расширения, пробуем определить по URL
            if not file_extension:
                file_extension = self._get_extension_from_url(url)

            return {
                'url': download_url,
                'extension': file_extension,
                'content_type': content_type
            }

        except Exception as e:
            logger.error(f"Ошибка получения информации о файле Яндекс.Диска: {str(e)}")
            return await self._try_yandex_disk_alternative_methods(url, resource_id)


    def _extract_extension_from_content_disposition(self, content_disposition: str) -> str:
        """
        Извлекает расширение файла из заголовка Content-Disposition HTTP-ответа.

        Args:
            content_disposition (str): Значение заголовка Content-Disposition.

        Returns:
            str: Расширение файла (например, '.pdf'), либо пустая строка, если не удалось извлечь.
        """
        if not content_disposition:
            return ""

        # Ищем filename в content-disposition
        filename_match = re.search(r'filename[^;=\n]*=(([\'"]).*?\2|[^;\n]*)', content_disposition)
        if filename_match:
            filename = filename_match.group(1)
            # Убираем кавычки
            filename = filename.strip('\"\'')
            # Извлекаем расширение
            _, extension = os.path.splitext(filename)
            if extension:
                return extension.lower()

        return ""


    async def _try_yandex_disk_alternative_methods(self, url: str, resource_id: str) -> Dict:
        """
        Пробует альтернативные URL-адреса для скачивания файла с Яндекс.Диска, если основной метод не сработал.

        Args:
            url (str): Исходная ссылка.
            resource_id (str): Идентификатор ресурса.

        Returns:
            Dict: Информация о файле с рабочим URL или резервным вариантом.
        """
        alternative_urls = [
            # Основной метод скачивания
            f"https://disk.yandex.ru/d/{resource_id}?format=download",
            f"https://yadi.sk/d/{resource_id}?format=download",
            # Прямое скачивание
            f"https://disk.yandex.ru/dl/{resource_id}",
            f"https://yadi.sk/i/{resource_id}/download",
        ]

        for alt_url in alternative_urls:
            try:
                head_response = requests.head(alt_url, allow_redirects=True, timeout=5)
                if head_response.status_code == 200:
                    content_type = head_response.headers.get('content-type', '')
                    file_extension = self._get_extension_from_content_type(content_type)

                    return {
                        'url': alt_url,
                        'extension': file_extension or '.bin',
                        'content_type': content_type
                    }
            except Exception as e:
                logger.warning(f"Альтернативный URL {alt_url} не сработал: {str(e)}")
                continue
            
        # Если ничего не сработало, возвращаем базовую ссылку
        return {
            'url': f"https://disk.yandex.ru/d/{resource_id}?format=download",
            'extension': '.bin',
            'content_type': 'application/octet-stream'
        }


    async def _parse_mail_cloud(self, url: str, procurement_id: str = None) -> Dict:
        """
        Обрабатывает ссылку на файл в Облаке Mail.ru и запускает его обработку.

        Args:
            url (str): Ссылка на файл в Облаке Mail.ru.
            procurement_id (str, optional): Идентификатор закупки. Defaults to None.

        Returns:
            Dict: Результат обработки файла.
        """
        try:
            # Для Облака Mail.ru используем прямую ссылку
            download_url = url
            
            return await self._download_and_process_file(
                download_url,
                "mail_cloud",
                self._extract_mail_cloud_resource(url),
                procurement_id
            )
            
        except Exception as e:
            logger.error(f"Ошибка парсинга Mail.ru Cloud: {str(e)}")
            return {
                "status": "error",
                "error": f"Ошибка парсинга Mail.ru Cloud: {str(e)}"
            }


    async def _download_and_process_file(self, url: str, source: str, resource_id: str, 
                                       procurement_id: str = None, file_extension: str = None) -> Dict:
        """
        Скачивает файл по указанному URL во временный файл и запускает его анализ.

        Args:
            url (str): Прямая ссылка для скачивания.
            source (str): Источник файла (например, 'google_drive').
            resource_id (str): Уникальный идентификатор ресурса.
            procurement_id (str, optional): Идентификатор закупки. Defaults to None.
            file_extension (str, optional): Расширение файла. Defaults to None.

        Raises:
            Exception: Возникает при ошибках скачивания или обработки.

        Returns:
            Dict: Результат анализа файла.
        """
        try:
            # Скачиваем файл с таймаутом
            response = requests.get(url, stream=True, timeout=30)
            
            # Проверяем статус ответа
            if response.status_code != 200:
                # Пробуем альтернативные форматы для Google таблиц
                if "spreadsheets" in url and "export" in url:
                    return await self._try_alternative_google_sheets_formats(resource_id, procurement_id)
                else:
                    raise Exception(f"HTTP {response.status_code}: {response.reason}")
            
            # Определяем расширение файла из заголовков или URL
            if not file_extension:
                content_type = response.headers.get('content-type', '')
                file_extension = self._get_extension_from_content_type(content_type)
                
                if not file_extension:
                    file_extension = self._get_extension_from_url(url)
            
            # Создаем временный файл
            with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as tmp_file:
                for chunk in response.iter_content(chunk_size=8192):
                    tmp_file.write(chunk)
                tmp_path = tmp_file.name
            
            # Обрабатываем файл
            filename = f"{source}_{resource_id}{file_extension}"
            result = await self._process_downloaded_file(tmp_path, filename, procurement_id)
            
            # Очищаем временный файл
            os.unlink(tmp_path)
            
            return result
            
        except Exception as e:
            logger.error(f"Ошибка скачивания файла {url}: {str(e)}")
            
            # Для Google таблиц пробуем CSV как запасной вариант
            if "spreadsheets" in url:
                return await self._try_google_sheets_csv(resource_id, procurement_id)
            
            return {
                "status": "error",
                "error": f"Ошибка скачивания файла: {str(e)}"
            }


    def _try_alternative_google_sheets_formats(self, sheet_id: str, procurement_id: str = None) -> Dict:
        """
        Пробует альтернативные форматы экспорта Google Таблиц (xlsx, pdf, ods), если основной не сработал.

        Args:
            sheet_id (str): ID Google Таблицы.
            procurement_id (str, optional): Идентификатор закупки. Defaults to None.

        Returns:
            Dict: Результат обработки в первом успешном формате или ошибка.
        """
        formats_to_try = [
            ("https://docs.google.com/spreadsheets/d/{}/export?format=xlsx", ".xlsx"),
            ("https://docs.google.com/spreadsheets/d/{}/export?format=pdf", ".pdf"),
            ("https://docs.google.com/spreadsheets/d/{}/export?format=ods", ".ods"),
        ]

        for url_template, extension in formats_to_try:
            try:
                url = url_template.format(sheet_id)
                response = requests.get(url, stream=True, timeout=20)

                if response.status_code == 200:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=extension) as tmp_file:
                        for chunk in response.iter_content(chunk_size=8192):
                            tmp_file.write(chunk)
                        tmp_path = tmp_file.name

                    filename = f"google_sheets_{sheet_id}{extension}"
                    result = self._process_downloaded_file(tmp_path, filename, procurement_id)

                    os.unlink(tmp_path)
                    return result

            except Exception as e:
                logger.warning(f"Формат {extension} не сработал: {str(e)}")
                continue
            
        # Если все форматы не сработали, пробуем CSV
        return self._try_google_sheets_csv(sheet_id, procurement_id)


    def _try_google_sheets_csv(self, sheet_id: str, procurement_id: str = None) -> Dict:
        """
        Экспортирует Google Таблицу в формате CSV как последний резервный вариант.

        Args:
            sheet_id (str): ID Google Таблицы.
            procurement_id (str, optional): Идентификатор закупки. Defaults to None.

        Raises:
            Exception: Если CSV-экспорт завершился с ошибкой.

        Returns:
            Dict: Результат обработки CSV-файла или ошибка.
        """
        try:
            csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv"
            response = requests.get(csv_url, timeout=20)

            if response.status_code == 200:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp_file:
                    tmp_file.write(response.content)
                    tmp_path = tmp_file.name

                filename = f"google_sheets_{sheet_id}.csv"
                result = self._process_downloaded_file(tmp_path, filename, procurement_id)

                os.unlink(tmp_path)
                return result
            else:
                raise Exception(f"CSV export failed with status {response.status_code}")

        except Exception as e:
            logger.error(f"CSV export также не сработал: {str(e)}")
            return {
                "status": "error",
                "error": f"Не удалось экспортировать Google таблицу: {str(e)}"
            }


    async def _process_downloaded_file(self, file_path: str, filename: str, procurement_id: str = None) -> Dict:
        """
        Извлекает текст из локального файла, разбивает его на чанки и запускает семантический анализ.

        Args:
            file_path (str): Путь к временному файлу.
            filename (str): Имя файла (для метаданных).
            procurement_id (str, optional): Идентификатор закупки. Defaults to None.

        Returns:
            Dict: Результат анализа документа.
        """
        try:
            # Извлекаем текст из файла
            extracted_text = read_file(file_path, original_filename=filename)
            
            if not extracted_text or "[Нет читаемого текста]" in extracted_text:
                return {
                    "status": "error",
                    "error": "Не удалось извлечь текст из файла"
                }
            
            # Разделяем на чанки
            chunks = split_large_text(extracted_text, max_chunk_size=20000)
            
            # Анализируем документ
            analysis_result = await analyze_document_chunks(
                chunks=chunks,
                document_name=filename,
                document_type="",
                law_type="44-ФЗ",  # можно сделать параметром
                procurement_method="Конкурс"  # можно сделать параметром
            )
            
            return {
                "status": "success",
                "filename": filename,
                "source": "cloud_storage",
                "analysis": analysis_result,
                "procurement_id": procurement_id
            }
            
        except Exception as e:
            logger.error(f"Ошибка обработки файла: {str(e)}")
            return {
                "status": "error",
                "error": f"Ошибка обработки файла: {str(e)}"
            }


    def _extract_google_drive_file_id(self, url: str) -> Optional[str]:
        """
        Извлекает ID файла из различных форматов ссылок Google Drive.

        Args:
            url (str): Ссылка на файл Google Drive.

        Returns:
            Optional[str]: ID файла или None, если не найден.
        """
        patterns = [
            r'/file/d/([a-zA-Z0-9_-]+)',
            r'id=([a-zA-Z0-9_-]+)',
            r'/open\?id=([a-zA-Z0-9_-]+)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        return None


    def _extract_google_docs_id(self, url: str) -> Optional[str]:
        """
        Извлекает ID документа из ссылок Google Docs, Sheets или Slides.

        Args:
            url (str): Ссылка на Google-документ.

        Returns:
            Optional[str]: ID документа или None, если не найден.
        """
        patterns = [
            r'/document/d/([a-zA-Z0-9_-]+)',
            r'/presentation/d/([a-zA-Z0-9_-]+)',
            r'/spreadsheets/d/([a-zA-Z0-9_-]+)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        return None


    def _extract_yandex_disk_resource(self, url: str) -> str:
        """
        Извлекает идентификатор ресурса из публичной ссылки Яндекс.Диска.

        Args:
            url (str): Публичная ссылка на файл или папку Яндекс.Диска.

        Returns:
            str: Идентификатор ресурса.
        """
        patterns = [
            r'/i/([a-zA-Z0-9_-]+)',
            r'/d/([a-zA-Z0-9_-]+)',
            r'/client/disk/([a-zA-Z0-9_-]+)'
        ]

        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)

        # Если не нашли по шаблонам, берем последнюю часть URL
        parsed = urlparse(url)
        path_parts = parsed.path.split('/')
        return path_parts[-1] if path_parts else "yandex_resource"


    def _extract_mail_cloud_resource(self, url: str) -> str:
        """
        Извлекает имя файла или идентификатор из ссылки Облака Mail.ru.

        Args:
            url (str): Ссылка на файл в Облаке Mail.ru.

        Returns:
            str: Имя или идентификатор ресурса.
        """
        parsed = urlparse(url)
        path_parts = parsed.path.split('/')
        return path_parts[-1] if path_parts else "mail_cloud_resource"


    def _convert_yandex_disk_to_download(self, url: str) -> str:
        """
        Преобразует публичную ссылку Яндекс.Диска в прямую ссылку для скачивания.

        Args:
            url (str): Исходная публичная ссылка.

        Returns:
            str: URL с параметром скачивания.
        """
        # Извлекаем ID ресурса
        resource_id = self._extract_yandex_disk_resource(url)

        # Для публичных ссылок Яндекс.Диска
        if '/i/' in url or '/d/' in url:
            return f"https://disk.yandex.ru/d/{resource_id}?format=download"

        # Если ссылка уже содержит параметры скачивания
        if 'download' in url or 'format=download' in url:
            return url

        # Добавляем параметр скачивания к существующей ссылке
        if '?' in url:
            return f"{url}&format=download"
        else:
            return f"{url}?format=download"


    def _get_extension_from_content_type(self, content_type: str) -> str:
        """
        Определяет расширение файла по MIME-типу из HTTP-заголовка Content-Type.

        Args:
            content_type (str): MIME-тип файла.

        Returns:
            str: Расширение файла (например, '.pdf'), по умолчанию '.bin'.
        """
        extension_map = {
            # Документы
            'application/pdf': '.pdf',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
            'application/msword': '.doc',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
            'application/vnd.ms-excel': '.xls',
            'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
            'application/vnd.ms-powerpoint': '.ppt',

            # Текстовые файлы
            'text/plain': '.txt',
            'text/csv': '.csv',
            'text/html': '.html',
            'application/json': '.json',
            'application/xml': '.xml',

            # Изображения
            'image/jpeg': '.jpg',
            'image/jpg': '.jpg',
            'image/png': '.png',
            'image/gif': '.gif',
            'image/webp': '.webp',
            'image/svg+xml': '.svg',

            # Архивы
            'application/zip': '.zip',
            'application/x-rar-compressed': '.rar',
            'application/x-7z-compressed': '.7z',
            'application/gzip': '.gz',
            'application/x-tar': '.tar',

            # Яндекс-специфичные
            'application/x-yadisk-document': '.doc',  # Яндекс.Документы
            'application/x-yadisk-table': '.xls',     # Яндекс.Таблицы
            'application/x-yadisk-presentation': '.ppt',  # Яндекс.Презентации
        }

        # Берем основную часть content-type (игнорируем кодировку и т.д.)
        main_content_type = content_type.split(';')[0].strip().lower()

        return extension_map.get(main_content_type, '.bin')


    def _get_extension_from_url(self, url: str) -> str:
        """
        Определяет расширение файла по его URL, анализируя путь.

        Args:
            url (str): URL файла.

        Returns:
            str: Расширение файла или '.bin' по умолчанию.
        """
        parsed = urlparse(url)
        path = parsed.path.lower()

        extension_map = {
            '.pdf': '.pdf',
            '.docx': '.docx', '.doc': '.doc',
            '.xlsx': '.xlsx', '.xls': '.xls',
            '.pptx': '.pptx', '.ppt': '.ppt',
            '.txt': '.txt', '.csv': '.csv',
            '.zip': '.zip', '.rar': '.rar', '.7z': '.7z',
            '.jpg': '.jpg', '.jpeg': '.jpg', '.png': '.png', '.gif': '.gif',
            '.html': '.html', '.htm': '.html',
        }

        # Проверяем полное совпадение
        for ext, result_ext in extension_map.items():
            if path.endswith(ext):
                return result_ext

        # Проверяем частичное совпадение (например, в середине URL)
        for ext in extension_map.keys():
            if ext in path:
                return extension_map[ext]

        return '.bin'


# Глобальный экземпляр парсера
cloud_parser = CloudStorageParser()


async def parse_cloud_storage_link(url: str, procurement_id: str = None) -> Dict:
    """
    Удобная обёртка для асинхронного парсинга ссылки на облачное хранилище.

    Args:
        url (str): Ссылка на файл в облаке.
        procurement_id (str, optional): Идентификатор закупки. Defaults to None.

    Returns:
        Dict: Результат обработки.
    """
    return await cloud_parser.parse_cloud_link(url, procurement_id)


def is_cloud_storage_link(url: str) -> bool:
    """
    Проверяет, ведёт ли ссылка на поддерживаемое облачное хранилище.

    Args:
        url (str): Проверяемая ссылка.

    Returns:
        bool: True, если ссылка поддерживается.
    """
    return cloud_parser.is_cloud_link(url)


async def process_input_links(links: List[str], procurement_id: str = None) -> List[Dict]:
    """
    Обрабатывает список ссылок: определяет тип (облако или прямая ссылка) и запускает анализ.

    Args:
        links (List[str]): Список URL-адресов.
        procurement_id (str, optional): Идентификатор закупки. Defaults to None.

    Returns:
        List[Dict]: Список результатов обработки каждой ссылки.
    """
    results = []
    
    for link in links:
        try:
            if is_cloud_storage_link(link):
                # Обрабатываем как облачное хранилище
                result = await parse_cloud_storage_link(link, procurement_id)
            else:
                # Пробуем обработать как прямую ссылку на файл
                result = await cloud_parser._download_and_process_file(
                    link, 
                    "direct_link",
                    cloud_parser._extract_filename_from_url(link),
                    procurement_id
                )
            
            results.append({
                "url": link,
                "result": result
            })
            
        except Exception as e:
            logger.error(f"Ошибка обработки ссылки {link}: {str(e)}")
            results.append({
                "url": link,
                "result": {
                    "status": "error",
                    "error": f"Ошибка обработки: {str(e)}"
                }
            })
    
    return results


def _extract_filename_from_url(url: str) -> str:
    """
    Извлекает имя файла из URL, обрезая слишком длинные имена.

    Args:
        url (str): URL файла.

    Returns:
        str: Имя файла, пригодное для использования в файловой системе.
    """
    parsed = urlparse(url)
    path_parts = parsed.path.split('/')
    filename = path_parts[-1] if path_parts else "unknown_file"
    
    # Если имя файла слишком длинное, обрезаем
    if len(filename) > 50:
        name, ext = os.path.splitext(filename)
        filename = name[:45] + ext
    
    return filename