import os
import re
import logging
import tempfile
import asyncio
import json

import aiohttp
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs


logger = logging.getLogger(__name__)


async def execute_with_retry(async_func, *args, max_retries=3, base_delay=5, **kwargs):
    """
    Выполняет асинхронную функцию с автоматическими повторными попытками при возникновении ошибок.

    Поддерживает экспоненциальную задержку между попытками и дополнительно проверяет,
    не вернула ли функция словарь со статусом "error" — в этом случае также считается,
    что операция завершилась неудачей. Используется для повышения надёжности вызовов
    внешних сервисов или нестабильных операций.

    Args:
        async_func (_type_): Асинхронная функция для выполнения.
        max_retries (int, optional): Максимальное количество повторных попыток.
            По умолчанию 3.
        base_delay (int, optional): Начальная задержка в секундах перед первой повторной попыткой.
            По умолчанию 5.

    Raises:
        last_exception: Исключение, возникшее при последней (неудачной) попытке.

    Returns:
        _type_: Результат успешного выполнения async_func.
    """
    last_exception = None
    
    for attempt in range(max_retries + 1):
        try:
            result = await async_func(*args, **kwargs)
            
            # Проверяем, не вернула ли функция ошибку в результате
            if isinstance(result, dict) and result.get("status") == "error":
                raise Exception(result.get("error", "Unknown error in result"))
                
            return result
            
        except Exception as e:
            last_exception = e
            logger.warning(f"Попытка {attempt + 1}/{max_retries + 1} не удалась: {str(e)}")
            
            if attempt < max_retries:
                # Экспоненциальная задержка
                delay = base_delay * (2 ** attempt)
                logger.info(f"Повтор через {delay} секунд...")
                await asyncio.sleep(delay)
            else:
                logger.error(f"Все {max_retries + 1} попыток не удались")
                raise last_exception
    
    raise last_exception


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
            'files.mail.ru': self._parse_mail_cloud
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

        Выполняет маршрутизацию к соответствующему парсеру по домену и применяет
        повторные попытки при ошибках.

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
            return await execute_with_retry(
                parser_func,
                url,
                procurement_id,
                max_retries=3,
                base_delay=5
            )
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
        Парсит публичную ссылку на документ в облаке Mail.ru и возвращает прямую ссылку для скачивания.

        Анализирует HTML-страницу публичной папки Mail.ru, извлекает данные из скрипта
        window.cloudSettings, формирует прямой URL на файл и инициирует его скачивание.
        Поддерживает обработку редиректов и экранированных символов в JSON.

        Args:
            url (str): Публичная ссылка на файл в облаке Mail.ru (должна содержать '/public/').
            procurement_id (str, optional): Идентификатор закупки для логирования и привязки результата.
                По умолчанию None.

        Raises:
            Exception: При неверном формате ссылки, отсутствии необходимых данных в HTML,
                ошибках загрузки страницы или парсинга JSON.

        Returns:
            Dict: Словарь с результатом операции. При успехе — содержит данные скачанного файла;
                при ошибке — статус "error" и сообщение об ошибке.
        """
        try:
            clean_url = url.strip()
            if '/public/' not in clean_url:
                raise Exception("Некорректный формат ссылки Mail.ru")

            # === 1. Скачиваем HTML-страницу с User-Agent и БЕЗ редиректов ===
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(clean_url, headers=headers, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        html_content = await response.text()
                    elif 300 <= response.status < 400:
                        # Mail.ru иногда редиректит на другую страницу с тем же контентом
                        redirect_url = response.headers.get('Location')
                        if redirect_url:
                            async with session.get(redirect_url, headers=headers, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10)) as resp2:
                                if resp2.status == 200:
                                    html_content = await resp2.text()
                                else:
                                    raise Exception(f"Не удалось загрузить страницу после редиректа: {resp2.status}")
                        else:
                            raise Exception("Получен редирект без Location")
                    else:
                        raise Exception(f"Не удалось загрузить страницу Mail.ru: {response.status}")

            # === 2. Извлекаем window.cloudSettings ===
            match = re.search(r'<script>window\.cloudSettings\s*=\s*({.*?})\s*;</script>', html_content, re.DOTALL)
            if not match:
                raise Exception("Блок window.cloudSettings не найден в HTML")

            json_str = match.group(1)

            # Очищаем от x3c/x3e (экранированные символы)
            json_str = re.sub(r'"[^"]*x3[cd][^"]*"', '""', json_str)

            # Парсим JSON
            try:
                cloud_settings = json.loads(json_str)
            except json.JSONDecodeError:
                json_str = json_str.replace(r'\/', '/')
                cloud_settings = json.loads(json_str)

            # === 3. Собираем прямую ссылку ===
            weblink_get_list = cloud_settings.get("dispatcher", {}).get("weblink_get", [])
            if not weblink_get_list:
                raise Exception("weblink_get отсутствует в cloudSettings")

            base_url = weblink_get_list[0].get("url")
            weblink = cloud_settings.get("request", {}).get("weblink")
            if not base_url or not weblink:
                raise Exception("Не удалось извлечь base_url или weblink")

            direct_download_url = f"{base_url}/{weblink}"

            # === 4. Извлекаем имя файла ===
            filename = weblink.split('/')[-1] if '/' in weblink else weblink
            if not filename:
                filename = "mail_cloud_file"

            # === 5. Скачиваем файл ПО ПРЯМОЙ ССЫЛКЕ (без редиректов!) ===
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


# Глобальный экземпляр парсера
cloud_parser = CloudStorageParser()


async def parse_cloud_storage_link(url: str, procurement_id: str = None) -> Dict:
    """
    Асинхронно парсит документ по ссылке из облачного хранилища и возвращает его анализ.

    Поддерживает популярные облачные сервисы (например, Яндекс.Диск, Google Drive).
    Скачивает файл, извлекает текст, определяет тип документа и возвращает
    структурированный результат анализа. Используется как основной интерфейс
    для обработки внешних ссылок.

    Args:
        url (str): URL на документ в облачном хранилище.
        procurement_id (str, optional): Идентификатор закупки для логирования
            и привязки результата. По умолчанию None.

    Returns:
        Dict: Словарь с результатом парсинга, содержащий статус, путь к временному файлу,
            имя документа, результат ИИ-анализа и другую метаинформацию.
    """
    return await cloud_parser.parse_cloud_link(url, procurement_id)


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