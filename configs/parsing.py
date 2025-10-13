import os
import re
import logging
import tempfile
import asyncio
import json
import httpx

import aiohttp
import magic
from typing import Dict
from typing import Dict, List, Optional, Tuple
from playwright.async_api import async_playwright, Browser
from urllib.parse import urljoin, urlparse, parse_qs
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)


class CloudStorageParser:
    """
    Парсер для извлечения документов из популярных облачных хранилищ.

    Поддерживает Google Drive, Google Docs (включая таблицы и презентации),
    Яндекс.Диск и облако Mail.ru. Предоставляет единый интерфейс для проверки
    и асинхронной обработки ссылок, включая скачивание файлов и подготовку
    их для дальнейшего анализа.
    """
    
    def __init__(self):
        """Инициализирует парсер с маппингом поддерживаемых доменов и соответствующих методов обработки."""
        self.supported_domains = {
            'drive.google.com': self._parse_google_drive,
            'docs.google.com': self._parse_google_docs,
            'yadi.sk': self._parse_yandex_disk,
            'disk.yandex.ru': self._parse_yandex_disk,
            'cloud.mail.ru': self._parse_mail_cloud,
            'files.mail.ru': self._parse_mail_cloud,
            'zakupki.gov.ru': self._parse_eis,
        }


    def is_cloud_link(self, url: str) -> bool:
        """
        Проверяет, относится ли URL к поддерживаемому облачному хранилищу.

        Args:
            url (str): Проверяемый URL.

        Returns:
            bool: True, если домен URL поддерживается, иначе False.
        """
        parsed = urlparse(url)
        return parsed.netloc in self.supported_domains


    async def parse_cloud_link(self, url: str, procurement_id: str = None) -> Dict:
        """
        Асинхронно обрабатывает ссылку на документ в облаке и возвращает информацию для скачивания.

        Выполняет маршрутизацию к соответствующему парсеру по домену.

        Args:
            url (str): Ссылка на документ в облачном хранилище.
            procurement_id (str, optional): Идентификатор закупки для логирования.
                По умолчанию None.

        Returns:
            Dict: Результат обработки: либо данные для скачивания файла, либо ошибка.
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
        Обрабатывает ссылку на файл в Google Drive и формирует прямую ссылку для скачивания.

        Args:
            url (str): Ссылка на файл в Google Drive.
            procurement_id (str, optional): Идентификатор закупки. По умолчанию None.

        Returns:
            Dict: Результат с прямой ссылкой на скачивание или ошибкой.
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
            return await self._download_file_only(
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
        Обрабатывает ссылку на Google Docs, Sheets или Slides и формирует ссылку для экспорта.

        Args:
            url (str): Ссылка на документ Google Docs.
            procurement_id (str, optional): Идентификатор закупки. По умолчанию None.

        Returns:
            Dict: Результат с ссылкой на экспорт в подходящем формате или ошибкой.
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
                export_url = f"https://docs.google.com/spreadsheets/d/{doc_id}/export?format=xlsx"
                file_extension = ".xlsx"
            elif doc_type == "presentation":
                export_url = f"https://docs.google.com/presentation/d/{doc_id}/export?format=pptx"
                file_extension = ".pptx"
            else:  # document
                export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=pdf"
                file_extension = ".pdf"
            
            return await self._download_file_only(
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
        Извлекает идентификатор и тип документа из URL Google Docs.

        Args:
            url (str): URL на документ Google Docs.

        Returns:
            Tuple[Optional[str], str]: Кортеж из ID документа (или None) и типа документа
                ("document", "spreadsheets", "presentation").
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
        Обрабатывает ссылку на файл в Яндекс.Диске и получает прямую ссылку для скачивания.

        Args:
            url (str): Ссылка на файл или папку в Яндекс.Диске.
            procurement_id (str, optional): Идентификатор закупки. По умолчанию None.

        Returns:
            Dict: Результат с прямой ссылкой на скачивание или ошибкой.
        """
        try:
            # ОЧИЩАЕМ URL ОТ ЛИШНИХ ПРОБЕЛОВ
            clean_url = url.strip()

            # Извлекаем ID ресурса из ОЧИЩЕННОГО URL
            resource_id = self._extract_yandex_disk_resource(clean_url)

            # Получаем информацию о файле и прямую ссылку для скачивания
            download_info = await self._get_yandex_disk_download_info(clean_url, resource_id)
            if not download_info:
                return {
                    "status": "error",
                    "error": "Не удалось получить информацию о файле с Яндекс.Диска"
                }
            download_url = download_info['url']
            file_extension = download_info.get('extension', '.bin')
            return await self._download_file_only(
                download_url,
                "yandex_disk",
                resource_id,
                procurement_id,
                file_extension,
                original_filename=download_info.get('original_filename')
            )
        except Exception as e:
            logger.error(f"Ошибка парсинга Yandex Disk: {str(e)}")
            return {
                "status": "error",
                "error": f"Ошибка парсинга Yandex Disk: {str(e)}"
            }


    async def _get_yandex_disk_download_info(self, url: str, resource_id: str) -> Dict:
        """
        Запрашивает метаданные файла с API Яндекс.Диска для получения прямой ссылки.

        Args:
            url (str): Оригинальная публичная ссылка на ресурс.
            resource_id (str): Идентификатор ресурса.

        Returns:
            Dict: Информация для скачивания: URL, расширение, тип контента и имя файла.
        """
        try:
            async with aiohttp.ClientSession() as session:
                # Передаём ПОЛНУЮ ОЧИЩЕННУЮ ссылку как public_key
                params = {"public_key": url}
                api_url = "https://cloud-api.yandex.net/v1/disk/public/resources/download"
                async with session.get(api_url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status != 200:
                        logger.error(f"API Яндекс.Диска вернул статус {response.status} для {url}")
                        return await self._try_yandex_disk_alternative_methods(url, resource_id)

                    data = await response.json()
                    download_url = data.get("href")
                    if not download_url:
                        logger.error("API не вернул поле 'href'")
                        return await self._try_yandex_disk_alternative_methods(url, resource_id)

                    file_extension = self._get_extension_from_url(download_url)
                    if not file_extension:
                        file_extension = ".bin"

                    return {
                        'url': download_url,
                        'extension': file_extension,
                        'content_type': 'application/octet-stream',
                        'original_filename': self._extract_filename_from_url(download_url)
                    }
        except Exception as e:
            logger.error(f"Ошибка при обращении к API Яндекс.Диска для {url}: {str(e)}")
            return await self._try_yandex_disk_alternative_methods(url, resource_id)


    def _extract_extension_from_content_disposition(self, content_disposition: str) -> str:
        """
        Извлекает расширение файла из заголовка Content-Disposition.

        Args:
            content_disposition (str): Значение заголовка Content-Disposition.

        Returns:
            str: Расширение файла (например, ".pdf"), либо пустая строка, если не удалось извлечь.
        """
        if not content_disposition:
            return ""

        filename_match = re.search(r'filename[^;=\n]*=(([\'"]).*?\2|[^;\n]*)', content_disposition)
        if filename_match:
            filename = filename_match.group(1)
            filename = filename.strip('\"\'')
            _, extension = os.path.splitext(filename)
            if extension:
                return extension.lower()

        return ""


    async def _try_yandex_disk_alternative_methods(self, url: str, resource_id: str) -> Dict:
        """
        Пробует альтернативные URL-форматы для скачивания с Яндекс.Диска при отказе официального API.

        Args:
            url (str): Оригинальная ссылка.
            resource_id (str): Идентификатор ресурса.

        Returns:
            Dict: Информация для скачивания по альтернативному URL или заглушка.
        """
        alternative_urls = [
            f"https://disk.yandex.ru/d/{resource_id}?format=download",
            f"https://yadi.sk/d/{resource_id}?format=download",
            f"https://disk.yandex.ru/dl/{resource_id}",
            f"https://yadi.sk/i/{resource_id}/download",
        ]

        async with aiohttp.ClientSession() as session:
            for alt_url in alternative_urls:
                try:
                    async with session.head(alt_url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=5)) as response:
                        if response.status == 200:
                            content_type = response.headers.get('content-type', '')
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
        Обрабатывает публичную ссылку на файл в Облаке Mail.ru с использованием Playwright.

        Использует headless-браузер для загрузки страницы и извлечения клиентского состояния
        (__PRELOADED_STATE__), из которого определяется pageId. На основе pageId выполняется
        запрос к официальному API Mail.ru для получения прямой ссылки на скачивание файла.
        Поддерживает обработку ошибок недоступности файла (удалён, приватный, требует авторизацию).

        Args:
            url (str): Публичная ссылка на файл в формате https://cloud.mail.ru/public/...
            procurement_id (str, optional): Идентификатор закупки для логирования и привязки результата.
                По умолчанию None.

        Returns:
            Dict: Словарь с результатом операции. При успехе — содержит данные скачанного файла;
                при ошибке — статус "error" и понятное сообщение об ошибке.
        """
        try:
            clean_url = url.strip().rstrip('/')
            if '/public/' not in clean_url:
                raise Exception("Некорректный формат ссылки Mail.ru")

            weblink = clean_url.split('/public/', 1)[1]
            if not weblink:
                raise Exception("Не удалось извлечь weblink")

            # Формируем URL с weblink-параметром — как делает браузер
            target_url = f"https://cloud.mail.ru/public/{weblink}?weblink={weblink}"

            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-extensions",
                        "--mute-audio",
                        "--disable-web-security",
                    ]
                )
                page = await browser.new_page()
                await page.set_extra_http_headers({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                })

                try:
                    # Переходим на страницу
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)

                    # Ждём до 15 секунд, пока появится __PRELOADED_STATE__ ИЛИ ошибка
                    await page.wait_for_function(
                        """
                        () => {
                            if (window.__PRELOADED_STATE__) return true;
                            const errorText = document.body.innerText;
                            if (errorText.includes('Произошла ошибка') || errorText.includes('отключен JavaScript')) {
                                // Если ошибка — останавливаем ожидание
                                window.__MAILRU_ERROR__ = true;
                                return true;
                            }
                            return false;
                        }
                        """,
                        timeout=15000
                    )

                    # Получаем состояние
                    preloaded_state = await page.evaluate("() => window.__PRELOADED_STATE__")
                    has_error = await page.evaluate("() => window.__MAILRU_ERROR__")

                    content = await page.content()
                    await browser.close()

                    if has_error or not preloaded_state:
                        # Анализируем тело на наличие ошибки
                        if "Произошла ошибка" in content or "отключен JavaScript" in content:
                            raise Exception(
                                "Mail.ru Cloud вернул ошибку: файл недоступен, удалён или требует авторизации. "
                                "Ссылка может быть рабочей в браузере, но недоступна для автоматического парсинга."
                            )
                        else:
                            raise Exception("Данные не загрузились: __PRELOADED_STATE__ отсутствует")

                    # Извлекаем pageId
                    page_id = preloaded_state.get("pageInfo", {}).get("pageId")
                    if not page_id:
                        raise Exception("pageId не найден в __PRELOADED_STATE__")

                except Exception as e:
                    await browser.close()
                    raise e

            # === Далее — стандартный API-запрос через aiohttp ===
            dispatcher_url = f"https://cloud.mail.ru/api/v2/dispatcher?x-page-id={page_id}"
            async with aiohttp.ClientSession() as session:
                async with session.get(dispatcher_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        raise Exception(f"API dispatcher вернул статус {resp.status}")
                    dispatcher_data = await resp.json()

            weblink_get_list = dispatcher_data.get("body", {}).get("weblink_get", [])
            if not weblink_get_list:
                raise Exception("weblink_get отсутствует в ответе API")
            base_url = weblink_get_list[0].get("url")
            if not base_url:
                raise Exception("base_url не найден в weblink_get")

            direct_download_url = f"{base_url}/{weblink}"
            filename = weblink.split('/')[-1] or "mail_cloud_file"

            return await self._download_file_only(
                direct_download_url,
                "mail_cloud",
                filename,
                procurement_id
            )

        except Exception as e:
            logger.error(f"Ошибка парсинга Mail.ru Cloud: {str(e)}")
            return {
                "status": "error",
                "error": f"Ошибка парсинга Mail.ru Cloud: {str(e)}"
            }


    async def _download_file_only(self, url: str, source: str, resource_id: str, 
                                procurement_id: str = None, file_extension: str = None,
                                original_filename: str = None) -> Dict:
        """
        Скачивает файл по прямой ссылке и сохраняет его во временный файл.

        Args:
            url (str): Прямая ссылка для скачивания.
            source (str): Источник (например, "google_drive").
            resource_id (str): Идентификатор ресурса.
            procurement_id (str, optional): Идентификатор закупки. По умолчанию None.
            file_extension (str, optional): Расширение файла. По умолчанию None.
            original_filename (str, optional): Оригинальное имя файла. По умолчанию None.

        Returns:
            Dict: Результат с путём к временному файлу и метаданными или ошибкой.
        """
        try:
            # Используем aiohttp для асинхронного скачивания
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as response:
                    if response.status != 200:
                        # Пробуем альтернативные форматы для Google таблиц
                        if "spreadsheets" in url and "export" in url:
                            return await self._try_alternative_google_sheets_formats(resource_id, procurement_id)
                        else:
                            raise Exception(f"HTTP {response.status}: {response.reason}")
                    
                    # Определяем расширение файла из заголовков или URL
                    if not file_extension:
                        content_type = response.headers.get('content-type', '')
                        file_extension = self._get_extension_from_content_type(content_type)
                        
                        if not file_extension:
                            file_extension = self._get_extension_from_url(url)
                    
                    # Создаем временный файл
                    with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as tmp_file:
                        # Читаем данные чанками и пишем в файл
                        async for chunk in response.content.iter_chunked(8192):
                            tmp_file.write(chunk)
                        tmp_path = tmp_file.name
                    
                    # Используем original_filename, если есть
                    if original_filename:
                        # Очищаем от недопустимых символов
                        safe_name = re.sub(r'[<>:"/\\|?*]', '_', original_filename)
                        filename = safe_name
                    else:
                        filename = f"{source}_{resource_id}{file_extension}"
                    
                    # Возвращаем информацию о скачанном файле, а не анализируем его
                    return {
                        "status": "success",
                        "filename": filename,
                        "file_path": tmp_path,  # Путь к скачанному файлу
                        "source": "cloud_storage",
                        "original_url": url,
                        "procurement_id": procurement_id,
                        "file_extension": file_extension
                    }
                    
        except Exception as e:
            logger.error(f"Ошибка скачивания файла {url}: {str(e)}")
            
            # Для Google таблиц пробуем CSV как запасной вариант
            if "spreadsheets" in url:
                return await self._try_google_sheets_csv(resource_id, procurement_id)
            
            return {
                "status": "error",
                "error": f"Ошибка скачивания файла: {str(e)}"
            }


    async def _try_alternative_google_sheets_formats(self, sheet_id: str, procurement_id: str = None) -> Dict:
        """
        Пробует альтернативные форматы экспорта Google Таблиц при неудаче основного.

        Args:
            sheet_id (str): Идентификатор Google Таблицы.
            procurement_id (str, optional): Идентификатор закупки. По умолчанию None.

        Returns:
            Dict: Результат с первым успешным форматом или ошибкой.
        """
        formats_to_try = [
            ("https://docs.google.com/spreadsheets/d/{}/export?format=xlsx", ".xlsx"),
            ("https://docs.google.com/spreadsheets/d/{}/export?format=pdf", ".pdf"),
            ("https://docs.google.com/spreadsheets/d/{}/export?format=ods", ".ods"),
        ]

        async with aiohttp.ClientSession() as session:
            for url_template, extension in formats_to_try:
                try:
                    url = url_template.format(sheet_id)
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                        if response.status == 200:
                            with tempfile.NamedTemporaryFile(delete=False, suffix=extension) as tmp_file:
                                async for chunk in response.content.iter_chunked(8192):
                                    tmp_file.write(chunk)
                                tmp_path = tmp_file.name

                            filename = f"google_sheets_{sheet_id}{extension}"
                            
                            return {
                                "status": "success",
                                "filename": filename,
                                "file_path": tmp_path,
                                "source": "cloud_storage", 
                                "original_url": url,
                                "procurement_id": procurement_id,
                                "file_extension": extension
                            }

                except Exception as e:
                    logger.warning(f"Формат {extension} не сработал: {str(e)}")
                    continue
            
        # Если все форматы не сработали, пробуем CSV
        return await self._try_google_sheets_csv(sheet_id, procurement_id)


    async def _try_google_sheets_csv(self, sheet_id: str, procurement_id: str = None) -> Dict:
        """
        Экспортирует Google Таблицу в формате CSV как последний резервный вариант.

        Args:
            sheet_id (str): Идентификатор Google Таблицы.
            procurement_id (str, optional): Идентификатор закупки. По умолчанию None.

        Returns:
            Dict: Результат с CSV-файлом или ошибкой.
        """
        try:
            csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(csv_url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                    if response.status == 200:
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp_file:
                            content = await response.read()
                            tmp_file.write(content)
                            tmp_path = tmp_file.name

                        filename = f"google_sheets_{sheet_id}.csv"
                        
                        return {
                            "status": "success",
                            "filename": filename,
                            "file_path": tmp_path,
                            "source": "cloud_storage",
                            "original_url": csv_url,
                            "procurement_id": procurement_id,
                            "file_extension": ".csv"
                        }
                    else:
                        raise Exception(f"CSV export failed with status {response.status}")

        except Exception as e:
            logger.error(f"CSV export также не сработал: {str(e)}")
            return {
                "status": "error",
                "error": f"Не удалось экспортировать Google таблицу: {str(e)}"
            }


    def _extract_google_drive_file_id(self, url: str) -> Optional[str]:
        """
        Извлекает идентификатор файла из различных форматов ссылок Google Drive.

        Args:
            url (str): Ссылка на файл в Google Drive.

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


    def _extract_yandex_disk_resource(self, url: str) -> str:
        """
        Извлекает идентификатор ресурса из ссылки Яндекс.Диска.

        Args:
            url (str): Ссылка на файл или папку в Яндекс.Диске.

        Returns:
            str: Идентификатор ресурса или "yandex_resource" по умолчанию.
        """
        match = re.search(r'/[id]/([a-zA-Z0-9_-]+)', url)
        if match:
            return match.group(1)
        parsed = urlparse(url)
        return parsed.path.split('/')[-1] or "yandex_resource"


    def _extract_mail_cloud_resource(self, url: str) -> str:
        """
        Извлекает идентификатор ресурса из ссылки облака Mail.ru.

        Args:
            url (str): Ссылка на файл в Mail.ru Cloud.

        Returns:
            str: Идентификатор ресурса или "mail_cloud_resource" по умолчанию.
        """
        parsed = urlparse(url)
        path_parts = parsed.path.split('/')
        return path_parts[-1] if path_parts else "mail_cloud_resource"


    def _convert_yandex_disk_to_download(self, url: str) -> str:
        """
        Формирует URL для скачивания из публичной ссылки Яндекс.Диска.

        Args:
            url (str): Публичная ссылка на ресурс.

        Returns:
            str: URL с параметром format=download.
        """
        resource_id = self._extract_yandex_disk_resource(url)

        if '/i/' in url or '/d/' in url:
            return f"https://disk.yandex.ru/d/{resource_id}?format=download"

        if 'download' in url or 'format=download' in url:
            return url

        if '?' in url:
            return f"{url}&format=download"
        else:
            return f"{url}?format=download"


    def _get_extension_from_content_type(self, content_type: str) -> str:
        """
        Определяет расширение файла по MIME-типу.

        Args:
            content_type (str): MIME-тип контента.

        Returns:
            str: Расширение файла (например, ".pdf") или ".bin" по умолчанию.
        """
        extension_map = {
            'application/pdf': '.pdf',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
            'application/msword': '.doc',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
            'application/vnd.ms-excel': '.xls',
            'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
            'application/vnd.ms-powerpoint': '.ppt',
            'text/plain': '.txt',
            'text/csv': '.csv',
            'text/html': '.html',
            'application/json': '.json',
            'application/xml': '.xml',
            'image/jpeg': '.jpg',
            'image/jpg': '.jpg',
            'image/png': '.png',
            'image/gif': '.gif',
            'image/webp': '.webp',
            'image/svg+xml': '.svg',
            'application/zip': '.zip',
            'application/x-rar-compressed': '.rar',
            'application/x-7z-compressed': '.7z',
            'application/gzip': '.gz',
            'application/x-tar': '.tar',
        }

        main_content_type = content_type.split(';')[0].strip().lower()
        return extension_map.get(main_content_type, '.bin')


    def _get_extension_from_url(self, url: str) -> str:
        """
        Определяет расширение файла по URL.

        Args:
            url (str): URL файла.

        Returns:
            str: Расширение файла или ".bin" по умолчанию.
        """
        parsed = urlparse(url)
        path = parsed.path.lower()

        # Сначала пробуем по пути
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
        for ext in extension_map:
            if path.endswith(ext):
                return extension_map[ext]

        # Затем пробуем из параметра filename в query string
        query_params = parse_qs(parsed.query)
        filenames = query_params.get('filename', [])
        if filenames:
            filename = filenames[0]
            _, ext = os.path.splitext(filename)
            ext = ext.lower()
            if ext in extension_map:
                return extension_map[ext]

        return '.bin'


    def _extract_filename_from_url(self, url: str) -> str:
        """
        Извлекает имя файла из параметров URL.

        Args:
            url (str): URL с параметром filename.

        Returns:
            str: Имя файла или пустая строка, если не найдено.
        """
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)
        filenames = query_params.get('filename', [])
        if filenames:
            return filenames[0]
        return ""


    async def _parse_eis(self, url: str, procurement_id: str = None) -> Dict:
        """
        Парсит страницу документов на портале ЕИС (zakupki.gov.ru) и скачивает прикреплённые файлы.

        Поддерживает как закупки по 44-ФЗ, так и по 223-ФЗ. Извлекает regNumber или noticeInfoId
        из URL, формирует корректную ссылку на вкладку «Документы», загружает страницу через
        Playwright, обходит модальные окна и скачивает все доступные файлы из блока
        .blockFilesTabDocs. Для каждого файла определяется MIME-тип и корректное расширение.

        Args:
            url (str): URL страницы закупки в ЕИС (может содержать regNumber или noticeInfoId).
            procurement_id (str, optional): Идентификатор закупки для логирования и привязки результата.
                По умолчанию None.

        Returns:
            Dict: Словарь с результатом операции. При успехе содержит список скачанных файлов
                с путями, именами и метаданными; при ошибке — статус "error" и сообщение об ошибке.
        """
        try:
            logger.info(f"Начало парсинга ЕИС: {url}")
            clean_url = url.strip()
    
            # --- Шаг 1: извлекаем regNumber или noticeInfoId ---
            parsed = urlparse(clean_url)
            query_params = parse_qs(parsed.query)
            reg_number = query_params.get("regNumber", [None])[0]
            notice_info_id = query_params.get("noticeInfoId", [None])[0]
    
            if not reg_number and not notice_info_id:
                raise Exception("Не найден regNumber или noticeInfoId в URL")
    
            # --- Шаг 2: формируем URL страницы документов ---
            if "notice223" in clean_url:
                doc_url = f"https://zakupki.gov.ru/epz/order/notice/notice223/documents.html?noticeInfoId={notice_info_id}"
            else:
                path_parts = parsed.path.split('/')
                notice_type = None
                for part in path_parts:
                    if part in ("zk20", "ea20", "ezt20", "okd20", "okdp20", "oks20"):
                        notice_type = part
                        break
                if not notice_type:
                    notice_type = "zk20"
                doc_url = f"https://zakupki.gov.ru/epz/order/notice/{notice_type}/view/documents.html?regNumber={reg_number}"
    
            logger.info(f"URL документов ЕИС: {doc_url}")
    
            # --- Шаг 3: загружаем страницу через Playwright ---
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-extensions",
                        "--mute-audio",
                    ]
                )
                context = await browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                page = await context.new_page()
    
                try:
                    await page.goto(doc_url, wait_until="domcontentloaded", timeout=20000)
                    await page.wait_for_selector(".blockFilesTabDocs", timeout=15000)
    
                    # === ЗАКРЫВАЕМ МОДАЛЬНЫЕ ОКНА ===
                    close_selectors = [
                        'button[aria-label="Закрыть"]',
                        'button:has-text("Принимаю")',
                        'button:has-text("Согласен")',
                        'button:has-text("Закрыть")',
                        '#modal-customer button'
                    ]
                    for selector in close_selectors:
                        try:
                            btn = await page.query_selector(selector)
                            if btn:
                                await btn.click(timeout=5000)
                                await page.wait_for_timeout(1000)
                                break
                        except:
                            continue
                        
                    # === ИЗВЛЕКАЕМ ССЫЛКИ ИЗ blockFilesTabDocs ===
                    file_links = await page.eval_on_selector_all(
                        ".blockFilesTabDocs a[href*='filestore']",
                        """els => els.map(el => ({
                            href: el.href.trim(),
                            text: el.innerText.trim() || 'eis_document'
                        }))"""
                    )
    
                    if not file_links:
                        raise Exception("Не найдено ссылок на документы в блоке 'blockFilesTabDocs'")
    
                    logger.info(f"Найдено {len(file_links)} документов в ЕИС")
    
                    results = []
                    for link_info in file_links:
                        href = link_info["href"]
                        orig_filename = link_info["text"]
    
                        try:
                            # === СКАЧИВАЕМ ФАЙЛ ЧЕРЕЗ PLAYWRIGHT ===
                            async with page.expect_download(timeout=30000) as download_info:
                                await page.click(f"a[href='{href}']", force=True)
                            download = await download_info.value
    
                            # Сохраняем во временный файл
                            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                                await download.save_as(tmp.name)
                                tmp_path = tmp.name
    
                            # === ОПРЕДЕЛЯЕМ РЕАЛЬНОЕ РАСШИРЕНИЕ ===
                            real_mime = magic.from_file(tmp_path, mime=True)
                            mime_to_ext = {
                                'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
                                'application/msword': '.doc',
                                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
                                'application/vnd.ms-excel': '.xls',
                                'application/pdf': '.pdf',
                                'text/plain': '.txt',
                                'text/html': '.html',
                                'application/zip': '.zip',
                            }
                            real_ext = mime_to_ext.get(real_mime, '.bin')
    
                            # Безопасное имя файла
                            safe_name = re.sub(r'[<>:"/\\|?*]', '_', orig_filename)
                            if not safe_name.endswith(real_ext):
                                safe_name += real_ext
    
                            results.append({
                                "status": "success",
                                "filename": safe_name,
                                "file_path": tmp_path,
                                "source": "eis",
                                "original_url": href,
                                "procurement_id": procurement_id,
                                "file_extension": real_ext
                            })
    
                        except Exception as e:
                            logger.warning(f"Не удалось скачать файл {href}: {e}")
                            continue
                        
                    if not results:
                        raise Exception("Не удалось скачать ни одного документа из ЕИС")
    
                    return {
                        "status": "success",
                        "files": results,
                        "source": "eis",
                        "original_url": url,
                        "procurement_id": procurement_id
                    }
    
                finally:
                    await browser.close()
    
        except Exception as e:
            logger.error(f"Ошибка парсинга ЕИС: {str(e)}")
            return {
                "status": "error",
                "error": f"Ошибка парсинга ЕИС: {str(e)}"
            }


# Глобальный экземпляр парсера
cloud_parser = CloudStorageParser()


async def parse_cloud_storage_link(url: str, procurement_id: str = None) -> Dict:
    """
    Унифицирует результат парсинга ссылки на документ из облачного хранилища или ЕИС.

    Вызывает парсер, определяющий тип ссылки (Mail.ru, Google Drive, ЕИС и др.),
    скачивает файл(ы) и возвращает структурированный результат. Для ЕИС возвращает
    список файлов напрямую, для остальных облачных сервисов — оборачивает единичный
    результат в список для единообразия. Обеспечивает согласованный формат ответа
    независимо от источника.

    Args:
        url (str): URL на документ или страницу закупки в облаке или на портале ЕИС.
        procurement_id (str, optional): Идентификатор закупки для логирования и привязки результата.
            По умолчанию None.

    Returns:
        Dict: Словарь с единым форматом: при успехе содержит ключ "files" со списком
            обработанных файлов, источник, исходный URL и procurement_id; при ошибке —
            статус "error" и описание проблемы.
    """
    result = await cloud_parser.parse_cloud_link(url, procurement_id)
    if result["status"] == "success":
        if "files" in result:
            return result  # ЕИС уже возвращает список
        else:
            # Облако: оборачиваем один файл в список
            return {
                "status": "success",
                "files": [result],
                "source": result.get("source", "cloud"),
                "original_url": url,
                "procurement_id": procurement_id
            }
    else:
        return result


def is_cloud_storage_link(url: str) -> bool:
    """
    Проверяет, является ли URL ссылкой на документ в поддерживаемом облачном хранилище.

    Использует внутреннюю логику парсера для распознавания ссылок на популярные
    облачные сервисы, такие как Яндекс.Диск, Google Drive и другие.

    Args:
        url (str): Проверяемый URL.

    Returns:
        bool: True, если ссылка ведёт на поддерживаемое облачное хранилище, иначе False.
    """
    return cloud_parser.is_cloud_link(url)