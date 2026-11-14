import aiofiles
import asyncio
import datetime
import logging
import os
import re
import tempfile
import uuid
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp
import xmltodict

from configs.eis_common import EISSoapMixin
from configs.host_limiter import media_limiter, parse_retry_after
from configs.http_client_manager import HTTPClientManager
from configs.rate_limiter import TokenBucket
from configs.retry_utils import EIS_RETRY_CONFIG, MEDIA_RETRY_CONFIG, EISRateLimitError, async_retry
from configs.resources import resource_path
from configs.utils import get_file_extension

logger = logging.getLogger(__name__)


class CloudStorageParser(EISSoapMixin):
    """
    Парсер для извлечения документов из популярных облачных хранилищ.

    Поддерживает Google Drive, Google Docs (включая таблицы и презентации),
    Яндекс.Диск и облако Mail.ru. Предоставляет единый интерфейс для проверки
    и асинхронной обработки ссылок, включая скачивание файлов и подготовку
    их для дальнейшего анализа.
    """
    
    def __init__(self, http_manager: HTTPClientManager, expertise_object):
        """Инициализирует парсер с маппингом поддерживаемых доменов и соответствующих методов обработки."""
        self.supported_domains = {
            'drive.google.com': self._parse_google_drive,
            'docs.google.com': self._parse_google_docs,
            'yadi.sk': self._parse_yandex_disk,
            'disk.yandex.ru': self._parse_yandex_disk,
            'cloud.mail.ru': self._parse_mail_cloud,
            'files.mail.ru': self._parse_mail_cloud,
            'zakupki.gov.ru': self._parse_eis_soap_from_web,
            'develop.rao0123.1t.ws': self.download_media,
            'stage.rao0123.1t.ws': self.download_media,
            'expert.rusacademedu.ru': self.download_media,
            'otchet.almira.cc': self.download_media,
        }
        
        # Получаем токен ЕИС из переменных окружения
        self.eis_token = os.getenv('EIS_INDIVIDUAL_PERSON_TOKEN')
        self.eis_soap_url = os.getenv('EIS_SOAP_URL')

        self.http_manager = http_manager
        self.semaphore = asyncio.Semaphore(3)
        self.rate_limiter = TokenBucket(rate=1.5)  # 1.5 запроса в секунду — безопасно для ЕИС

        self.expertise_object = expertise_object


    async def _parse_eis_soap(self, url: str, procurement_id: str = None) -> Dict:
        """
        Обрабатывает SOAP запросы к ЕИС API.
        
        Формат URL: soap://method?param1=value1&param2=value2
        Поддерживаемые методы: getDocsByReestrNumber, getDocsByOrgRegion
        
        Args:
            url (str): SOAP URL в формате soap://method?params
            procurement_id (str, optional): Идентификатор закупки
            
        Returns:
            Dict: Результат с файлами из архива ЕИС
        """
        if not self.eis_token:
            return {
                "status": "error",
                "error": "Токен ЕИС не найден. Укажите ESV_key в переменных окружения."
            }
        
        try:
            # Парсим SOAP URL
            parsed = urlparse(url)
            method = parsed.hostname  # Используем hostname как название метода
            params = parse_qs(parsed.query)
            
            # Преобразуем параметры из списков в одиночные значения
            clean_params = {k: v[0] if len(v) == 1 else v for k, v in params.items()}
            
            if method == 'getDocsByReestrNumber':
                return await self._eis_soap_get_docs_by_reestr_number(clean_params, procurement_id)
            elif method == 'getDocsByOrgRegion':
                return await self._eis_soap_get_docs_by_org_region(clean_params, procurement_id)
            else:
                return {
                    "status": "error",
                    "error": f"Неподдерживаемый SOAP метод: {method}"
                }
                
        except Exception as e:
            logger.error(f"Ошибка обработки SOAP запроса: {str(e)}")
            return {
                "status": "error",
                "error": f"Ошибка обработки SOAP запроса: {str(e)}"
            }


    async def _eis_soap_get_docs_by_reestr_number(self, params: Dict, procurement_id: str = None) -> Dict:
        """Получает документы по реестровому номеру через SOAP API ЕИС"""
        reestr_number = params.get('reestrNumber')
        subsystem_type = params.get('subsystemType', 'PRIZ')
        
        if not reestr_number:
            return {
                "status": "error",
                "error": "Отсутствует обязательный параметр reestrNumber"
            }
        
        # Формируем XML запрос
        request_id = str(uuid.uuid4())
        create_date = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        
        xml_template = f'''<?xml version="1.0" encoding="UTF-8"?>
            <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ws="http://zakupki.gov.ru/fz44/get-docs-ip/ws">
            <soapenv:Header>
                <individualPerson_token>{self.eis_token}</individualPerson_token>
            </soapenv:Header>
            <soapenv:Body>
                <ws:getDocsByReestrNumberRequest>
                    <index>
                        <id>{request_id}</id>
                        <createDateTime>{create_date}</createDateTime>
                        <mode>PROD</mode>
                    </index>
                    <selectionParams>
                        <subsystemType>{subsystem_type}</subsystemType>
                        <reestrNumber>{reestr_number}</reestrNumber>
                    </selectionParams>
                </ws:getDocsByReestrNumberRequest>
            </soapenv:Body>
            </soapenv:Envelope>'''
        
        return await self._send_eis_soap_request(xml_template, f"reestr_{reestr_number}", procurement_id)


    async def _eis_soap_get_docs_by_org_region(self, params: Dict, procurement_id: str = None) -> Dict:
        """Получает документы по региону заказчика через SOAP API ЕИС"""
        org_region = params.get('orgRegion')
        document_type = params.get('documentType')
        exact_date = params.get('exactDate')
        subsystem_type = params.get('subsystemType', 'PRIZ')
        
        if not org_region or not document_type:
            return {
                "status": "error", 
                "error": "Отсутствуют обязательные параметры orgRegion или documentType"
            }
        
        # Формируем период
        period_info = ""
        if exact_date:
            period_info = f'''
            <periodInfo>
                <exactDate>{exact_date}</exactDate>
            </periodInfo>'''
        
        request_id = str(uuid.uuid4())
        create_date = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        
        xml_template = f'''<?xml version="1.0" encoding="UTF-8"?>
            <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ws="http://zakupki.gov.ru/fz44/get-docs-ip/ws">
            <soapenv:Header>
                <individualPerson_token>{self.eis_token}</individualPerson_token>
            </soapenv:Header>
            <soapenv:Body>
                <ws:getDocsByOrgRegionRequest>
                    <index>
                        <id>{request_id}</id>
                        <createDateTime>{create_date}</createDateTime>
                        <mode>PROD</mode>
                    </index>
                    <selectionParams>
                        <orgRegion>{org_region}</orgRegion>
                        <subsystemType>{subsystem_type}</subsystemType>
                        <documentType44>{document_type}</documentType44>
                        {period_info}
                    </selectionParams>
                </ws:getDocsByOrgRegionRequest>
            </soapenv:Body>
            </soapenv:Envelope>'''
        
        return await self._send_eis_soap_request(xml_template, f"region_{org_region}", procurement_id)


    async def _send_eis_soap_request(self, xml_data: str, identifier: str, procurement_id: str = None) -> Dict:
        """Отправляет SOAP запрос к ЕИС и обрабатывает ответ"""
        session = self.http_manager.get_session() # ✅ Используем общий пул
        soap_url = "https://int44.zakupki.gov.ru/eis-integration/services/getDocsIP"
        headers = {
            'Content-Type': 'text/xml; charset=utf-8',
        }
        
        try:
            # Отправляем SOAP запрос
            async with session.post(
                soap_url, 
                data=xml_data, 
                headers=headers, 
                timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                response_content = await response.text()
                logger.info(f"SOAP ответ: статус {response.status}")
                
                if response.status != 200:
                    return {
                        "status": "error",
                        "error": f"SOAP API вернул статус {response.status}: {response_content}"
                    }
                
                # Парсим ответ
                try:
                    response_dict = await asyncio.to_thread(xmltodict.parse, response_content)
                except Exception as parse_error:
                    logger.error(f"Ошибка парсинга XML ответа: {parse_error}")
                    return {
                        "status": "error",
                        "error": f"Ошибка парсинга XML ответа: {parse_error}"
                    }
                
                # Проверяем наличие ошибки в ответе
                error_info = self._extract_eis_error_info(response_dict)
                if error_info:
                    logger.warning(f"SOAP API вернул ошибку: {error_info}")
                    return {
                        "status": "error",
                        "error": f"SOAP API ошибка: {error_info}"
                    }
                
                # Извлекаем URL архива
                archive_url = self._extract_eis_archive_url(response_dict)
                if not archive_url:
                    return {
                        "status": "error",
                        "error": f"Не удалось извлечь URL архива из ответа ЕИС"
                    }
                
                # Скачиваем и обрабатываем архив
                return await self._download_eis_archive(archive_url, identifier, procurement_id)
                    
        except Exception as e:
            logger.error(f"Ошибка SOAP запроса к ЕИС: {str(e)}")
            return {
                "status": "error",
                "error": f"Ошибка SOAP запроса к ЕИС: {str(e)}"
            }
    
    async def _parse_eis_soap_from_web(self, url: str, procurement_id: str = None) -> Dict:
        """
        Проверяет доступность публичной страницы закупки на портале ЕИС и извлекает её реестровый номер.

        Не выполняет полного парсинга документов, а лишь подтверждает, что URL ведёт на существующую
        страницу закупки в ЕИС, и корректно извлекает идентификатор закупки (regNumber).
        Возвращает минимальный результат с подтверждением доступности страницы.

        Args:
            url (str): URL страницы закупки на zakupki.gov.ru.
            procurement_id (str, optional): Идентификатор закупки в локальной системе для логирования.
                По умолчанию None.

        Returns:
            Dict: Словарь с результатом проверки. При успехе содержит статус "success",
                номер закупки, источник и исходный URL; при ошибке — статус "error"
                и диагностическое сообщение. Поле "files" всегда пустое.
        """
        try:
            clean_url = url.strip()
            reestr_number = self._extract_reestr_number_from_web_url(clean_url)
            if not reestr_number:
                return await self._download_http_file(url=url, procurement_id=procurement_id, source="eis_web", handle_rate_limit=True)

            return await self._parse_eis_soap_ip(reestr_number, procurement_id)

        except Exception as e:
            logger.error(f"Ошибка при проверке ссылки ЕИС: {str(e)}")
            reestr_number = self._extract_reestr_number_from_web_url(url)
            return {
                "status": "error",
                "error": f"Ошибка проверки ссылки ЕИС: {str(e)}",
                "procurement_number": reestr_number,
                "source": "eis_web",
                "original_url": url
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
            return await self._download_http_file(
                url=download_url, 
                source="google_drive", 
                resource_id=file_id,
                procurement_id=procurement_id
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
            
            return await self._download_http_file(
                url=export_url,
                source="google_docs",
                resource_id=doc_id,
                procurement_id=procurement_id,
                file_extension=file_extension,
                fallback_formats=[
                    ("https://docs.google.com/spreadsheets/d/{}/export?format=xlsx", ".xlsx"),
                    ("https://docs.google.com/spreadsheets/d/{}/export?format=pdf", ".pdf"),
                    ("https://docs.google.com/spreadsheets/d/{}/gviz/tq?tqx=out:csv", ".csv"),
                ]
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
            
            return await self._download_http_file(
                url=download_url,
                source="yandex_disk",
                resource_id=resource_id,
                procurement_id=procurement_id,
                file_extension=file_extension,
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
            session = self.http_manager.get_session()
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

                file_extension = get_file_extension(url=download_url)
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

        session = self.http_manager.get_session()
        for alt_url in alternative_urls:
            try:
                async with session.head(alt_url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=5)) as response:
                    if response.status == 200:
                        content_type = response.headers.get('content-type', '')
                        file_extension = get_file_extension(content_type=response.headers.get('Content-Type', ''))

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
        Скачивает файлы из Mail.ru Cloud по публичной ссылке.
        Поддерживает папки и вложенные структуры.
        """
        try:
            clean_url = url.strip().rstrip('/')
            if '/public/' not in clean_url:
                raise ValueError("Некорректный формат ссылки Mail.ru")

            # === ИЗВЛЕКАЕМ weblink КОРРЕКТНО ===
            weblink = clean_url.split('/public/', 1)[1]
            if not weblink:
                raise ValueError("Не удалось извлечь weblink")
            logger.info(f"Извлечён weblink: {weblink}")

            # === ШАГ 1: Получаем pageId из HTML ===
            page_id = await self._get_page_id_from_html(clean_url)
            if not page_id:
                raise ValueError("pageId не найден в HTML")
            logger.info(f"Получен pageId: {page_id}")

            # === ШАГ 2: Получаем base_url из dispatcher ===
            base_url = await self._get_base_url(page_id)
            if not base_url:
                raise ValueError("base_url не получен из dispatcher")
            logger.info(f"Получен base_url: {base_url}")

            # === ШАГ 3: Получаем список файлов рекурсивно ===
            files = await self._get_all_files(weblink, page_id, base_url)
            if not files:
                raise ValueError("Файлы не найдены в облаке")
            logger.info(f"Найдено файлов: {len(files)}")

            results = []
            for file_info in files:
                direct_url = file_info["url"]
                fallback_url = "/".join(direct_url.split("/")[:-1])
                safe_name = file_info["filename"]
                ext = os.path.splitext(safe_name)[1] or ".bin"
                
                # === СКАЧИВАЕМ ФАЙЛ ===
                result = await self._download_http_file(
                    url=direct_url,
                    procurement_id=procurement_id,
                    source="mail_cloud",
                    resource_id=safe_name,
                    file_extension=ext,
                    original_filename=safe_name,
                    fallback_formats=[(fallback_url, ext)]
                )
                results.append(result)

            log = {
                "status": "success",
                "files": results,
                "source": "mail_cloud",
                "original_url": url,
                "procurement_id": procurement_id
            }
            logger.info(f"Результат парсинга Mail.ru: {log}")

            return {
                "status": "success",
                "files": results,
                "source": "mail_cloud",
                "original_url": url,
                "procurement_id": procurement_id
            }

        except Exception as e:
            logger.error(f"Ошибка парсинга Mail.ru Cloud: {str(e)}")
            return {
                "status": "error",
                "error": f"Ошибка парсинга Mail.ru Cloud: {str(e)}"
            }

    # === ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ (уже есть в коде, но улучшим) ===

    async def _get_page_id_from_html(self, url: str) -> Optional[str]:
        session = self.http_manager.get_session()
        async with session.get(url) as resp:
            html = await resp.text()
            # Ищем pageId в любом формате
            match = re.search(r'pageId["\']?\s*:\s*["\']?([a-zA-Z0-9_-]+)', html)
            return match.group(1) if match else None

    async def _get_base_url(self, page_id: str) -> Optional[str]:
        dispatcher_url = f"https://cloud.mail.ru/api/v2/dispatcher?x-page-id={page_id}"
        session = self.http_manager.get_session()
        async with session.get(dispatcher_url) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        
            # Попытка получить base_url из weblink_get (старый формат)
            weblink_get_list = data.get("body", {}).get("weblink_get", [])
            if weblink_get_list and isinstance(weblink_get_list, list):
                base_url = weblink_get_list[0].get("url")
                if base_url:
                    logger.debug(f"Получен base_url из weblink_get: {base_url}")
                    return base_url

            # Новый формат: weblink_get отсутствует → используем фиксированный URL
            # Официальный базовый URL для скачивания
            fixed_base_url = "https://cloclo1.datacloudmail.ru"
            logger.debug(f"weblink_get не найден. Используем фиксированный base_url: {fixed_base_url}")
            return fixed_base_url

    async def _get_all_files(self, weblink: str, page_id: str, base_url: str, current_path: str = "") -> List[Dict]:
        """Рекурсивно получает все файлы из папки"""
        folder_url = f"https://cloud.mail.ru/api/v2/folder?weblink={weblink}&x-page-id={page_id}"
        logger.debug(f"Запрос содержимого папки: {folder_url} (текущий путь: '{current_path}')")
        session = self.http_manager.get_session()
        async with session.get(folder_url) as resp:
            if resp.status != 200:
                logger.error(f"API folder вернул статус {resp.status}")
                return []
            data = await resp.json()
        
        files = []
        items = data.get("body", {}).get("list", [])
        logger.debug(f"Получено элементов в папке: {len(items)}")

        for item in data.get("body", {}).get("list", []):    
            if item["type"] == "folder":
                logger.debug(f"Обнаружена подпапка: {item['name']}")
                sub_files = await self._get_all_files(
                    weblink=f"{weblink}/{item['name']}",
                    page_id=page_id,
                    base_url=base_url,
                    current_path=f"{current_path}/{item['name']}" if current_path else item['name']
                )
                files.extend(sub_files)
            else:
                filename = item["name"]
                full_path = f"{current_path}/{filename}" if current_path else filename
                safe_name = re.sub(r'[<>:"/\\|?*]', '_', full_path)
                direct_url = f"{base_url}/{weblink}{f'/{current_path}' if current_path else ''}/{filename}"
                files.append({"url": direct_url, "filename": safe_name})
                logger.debug(f"Добавлен файл: {safe_name}")
        return files


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

        # Сначала filename из query-параметра
        query_params = parse_qs(parsed.query)
        filename = query_params.get("filename", [None])[0]

        if filename:
            return unquote(filename)

        # Затем последняя часть path
        path = unquote(parsed.path.rstrip("/"))
        filename = os.path.basename(path)

        # === ОБРАБОТКА view.html ===
        # Если это динамическая страница ЕИС (view.html), пытаемся сформировать уникальное имя на основе любого ID-параметра в query string.
        if filename == "view.html" and query_params:

            # Ищем ЛЮБОЙ параметр, оканчивающийся на "Id"
            for key, values in query_params.items():
                if key.endswith("Id") and values:
                    value = values[0]
                    # Убираем "Id" из ключа для красивого имени:
                    # contractInfoId -> contractInfo
                    clean_key = key[:-2] if len(key) > 2 else key
                    return f"printForm_{clean_key}_{value}.html"
            
            # Если ничего не нашли — берём первый непустой параметр с числовым значением
            for key, values in query_params.items():
                if values and (values[0].isdigit() or values[0].replace('.', '', 1).isdigit()):
                    return f"printForm_{key}_{values[0]}.html"   # было print(...) — имя терялось
            
            # Если совсем ничего не нашли — берём первый непустой параметр
            for key, values in query_params.items():
                if values and values[0]:
                    return f"printForm_{key}_{values[0]}.html"

        return filename or ""


    async def parse_cloud_link(self, url: str, procurement_id: str = None) -> Dict:
        """
        Определяет тип ссылки и делегирует её обработку соответствующему парсеру.

        Поддерживает специализированные парсеры для известных доменов (например, Mail.ru, Google Drive),
        обработку SOAP-ссылок на ЕИС и универсальный fallback-механизм для скачивания
        произвольных файлов по прямым HTTP/HTTPS-ссылкам. Гарантирует, что любая ссылка
        будет обработана без исключения: в худшем случае возвращается структура с ошибкой.

        Args:
            url (str): URL на документ или ресурс (поддерживается любой формат, включая soap://).
            procurement_id (str, optional): Идентификатор закупки для логирования и привязки результата.
                По умолчанию None.

        Returns:
            Dict: Словарь с единым форматом результата: при успехе — содержит файл(ы) и метаданные,
                при ошибке — статус "error" и диагностическое сообщение.
        """
        if url.startswith('soap://'):
            return await self._parse_eis_soap(url, procurement_id)

        parsed = urlparse(url)
        domain = parsed.netloc

        # 1. Поддерживаемые домены → специализированные парсеры
        if domain in self.supported_domains:
            parser_func = self.supported_domains[domain]
            return await parser_func(url, procurement_id)

        # 2. Любые другие ссылки → попытка скачать напрямую
        logger.info(f"Домен {domain} не поддерживается. Пробую generic-скачивание для: {url}")
        result = await self._download_http_file(url=url, procurement_id=procurement_id, source="generic", handle_rate_limit=True)

        # 3. Если generic тоже не сработал — не падаем, возвращаем ошибку
        if result["status"] != "success":
            logger.warning(f"Не удалось обработать ссылку (ни специализированный, ни generic парсер): {url}")
            return result

        return result
    

    def _extract_reestr_number_from_web_url(self, url: str) -> Optional[str]:
        """
        Извлекает реестровый номер из различных форматов URL ЕИС.

        Args:
            url (str): URL страницы закупки

        Returns:
            Optional[str]: Реестровый номер или None
        """
        try:
            parsed = urlparse(url)
            query_params = parse_qs(parsed.query)

            # Пробуем разные параметры, которые могут содержать реестровый номер
            reestr_number = (
                query_params.get("regNumber", [None])[0] or
                query_params.get("reestrNumber", [None])[0] 
                # or query_params.get("noticeInfoId", [None])[0]
            )

            if reestr_number:
                # Проверяем, что это действительно реестровый номер (обычно 20+ цифр)
                if re.match(r'^\d{10,}$', reestr_number):
                    return reestr_number

            # Если в query параметрах нет, пробуем извлечь из пути
            path_parts = parsed.path.split('/')
            for part in path_parts:
                # Реестровые номера обычно имеют формат: 0373200003624000001 (20+ цифр)
                if re.match(r'^\d{10,}$', part):
                    return part

            # Для URL типа common-info.html пробуем найти номер в предыдущих частях пути
            if 'common-info' in parsed.path or 'documents' in parsed.path:
                # Ищем номер в предыдущих сегментах пути
                path_segments = parsed.path.split('/')
                for i, segment in enumerate(path_segments):
                    if segment in ['common-info.html', 'documents.html', 'view'] and i > 0:
                        # Берем предыдущий сегмент
                        prev_segment = path_segments[i-1]
                        if re.match(r'^\d{10,}$', prev_segment):
                            return prev_segment

            logger.warning(f"Не удалось извлечь реестровый номер из URL: {url}")
            return None

        except Exception as e:
            logger.error(f"Ошибка извлечения реестрового номера из URL {url}: {str(e)}")
            return None


    async def _parse_eis_soap_ip(self, reg_number: str, procurement_id: str = None) -> Dict:
        """
        Парсинг закупки через официальный SOAP API ЕИС для физических лиц.
        Использует individualPerson_token и метод getDocsByReestrNumber.
        """
        try:
            logger.info(f"Запрос документов ЕИС по regNumber={reg_number} через SOAP API")

            # Формируем SOAP-запрос
            async with aiofiles.open(resource_path('xml/getDocsByReestrNumberRequest.xml'), 'r', encoding='utf-8') as file:
                xml_content = await file.read()
                generated_uuid = str(uuid.uuid4())
                generated_datetime = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                subsystem_type = "PRIZ" if self.expertise_object in (3,5,6) else "RGK"
                soap_body = (xml_content
                    .replace("{{ UUID }}", generated_uuid)
                    .replace("{{ datetime }}", generated_datetime)
                    .replace("{{ token }}", self.eis_token)
                    .replace("{{ subsystem_type }}", subsystem_type)
                    .replace("{{ reg_number }}", reg_number)
                )

            headers = {
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": "\"\""
            }

            # === 3. Отправляем запрос ===
            session = self.http_manager.get_session()
            async with session.post(
                self.eis_soap_url,
                data=soap_body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"SOAP ошибка: {response.status}, тело: {error_text}")
                    return {"status": "error", "error": f"SOAP ошибка: {response.status}"}

                soap_response = await response.text()
                logger.info(f"Ответ ЕИС: {soap_response}")
                response_content = await asyncio.to_thread(xmltodict.parse, soap_response)

            # === 4. Парсим archiveUrl ===
            archive_url = self._extract_eis_archive_url(response_content)
            if not archive_url:
                error_info = self._extract_eis_error_info(response_content)
                if error_info:
                    return {
                        "status": "error", 
                        "error": f"Ошибка парсинга ЕИС: {error_info}"
                        }
                return {
                    "status": "error", 
                    "error": "Не найден archiveUrl в ответе ЕИС"
                    }

            logger.info(f"Получена ссылка на архив: {archive_url}")

            # === 5. Скачиваем архив ===
            logger.info(f"type(archive_url): {type(archive_url)}")

            # Обработка: один URL или список
            if isinstance(archive_url, list):
                # Обрабатываем каждый архив параллельно
                tasks = [
                    self._download_eis_archive(url, f"{reg_number}_{i}", procurement_id)
                    for i, url in enumerate(archive_url)
                ]
                async with self.semaphore:
                    results = await asyncio.gather(*tasks)
                
                # Объединяем успешные результаты
                all_files = []
                errors = []
                for res in results:
                    if res.get("status") == "success" and "files" in res:
                        all_files.extend(res["files"])
                    elif res.get("status") == "error":
                        errors.append(res.get("error"))
                
                if all_files:
                    return {
                        "status": "success",
                        "procurement_number": reg_number,
                        "files": all_files,
                        "source": "eis_soap",
                        "original_url": archive_url,
                        "procurement_id": procurement_id,
                        "archive_count": len(archive_url),
                        "file_count": len(all_files)
                    }
                else:
                    return {
                        "status": "error",
                        "procurement_number": reg_number,
                        "error": f"Не удалось скачать ни один архив. Ошибки: {errors}",
                        "eis_response": response_content
                    }
            else:
                # Один URL — обрабатываем как раньше
                result = await self._download_eis_archive(archive_url, reg_number, procurement_id)
                result["eis_response"] = response_content


                # скачиваем закупочную документацию
                if result["status"] == "success":
                    logger.info(f"request_method = 'getDocsByReestrNumberRequest'")

                    # Фильтруем только успешные XML-файлы
                    xml_files = [
                        f for f in result["files"] 
                        if f["status"] == "success" and f["file_extension"] == ".xml"
                    ]
                    
                    if not xml_files:
                        return result


                    def select_latest_xml_versions(files):
                        """
                        Для документов из VERSIONED_DOCUMENT_TYPES:
                            оставляет только XML с максимальной версией.

                        Для остальных типов:
                            оставляет все XML без изменений.

                        Например:
                            epNotificationEOK2020_..._1_....xml
                            epNotificationEOK2020_..._2_....xml

                        -> останется только версия 2.

                        А:
                            epClarificationDoc_..._null_GUID1.xml
                            epClarificationDoc_..._null_GUID2.xml

                        -> останутся оба файла.
                        """
                        VERSIONED_DOCUMENT_TYPES = {
                            "epNotificationEZK2020",
                            "epNotificationEF2020",
                            "epNotificationEZT2020",
                            "epNotificationEOK2020",
                            "fcsNotificationEP",
                            "fcsNotification111",
                            "pprf615NotificationPO",
                            "pprf615NotificationEF",
                            "purchaseNotice",
                            "purchaseNoticeOK",
                            "purchaseNoticeOA",
                            "purchaseNoticeAE",
                            "purchaseNoticeAE94FZ",
                            "purchaseNoticeAESMBO",
                            "purchaseNoticeZK",
                            "purchaseNoticeZKESMBO",
                            "purchaseNoticeZPESMBO",
                            "purchaseNoticeEP",
                            "contract",
                            "pprf615Contract",
                            "contractCutted",
                        }

                        latest = {}

                        for fileinfo in files:
                            logger.info(f'filename: {fileinfo["filename"]}')

                            m = fileinfo["filename"].split('_')

                            key = fileinfo["filename"] if ((m[2] == 'null') or (m[0] not in VERSIONED_DOCUMENT_TYPES)) else f'{m[0]}_{m[1]}'

                            version = 0 if m[2] == 'null' else int(m[2])

                            if key not in latest or version > latest[key][0]:
                                latest[key] = (version, fileinfo)

                        return [item[1] for item in latest.values()]

                    # Отбор актуальных версий xml-файлов
                    actual_xml_files = select_latest_xml_versions(xml_files)

                    # Ограничитель параллелизма (настраивается)
                    semaphore = asyncio.Semaphore(10)
                    
                    async def process_xml_file(file_info):
                        async with semaphore:
                            try:
                                # Чтение файла
                                async with aiofiles.open(file_info["file_path"], "r", encoding="utf-8") as f:
                                    extracted_text = await f.read()
                                
                                if not extracted_text:
                                    return []
                                
                                # Выносим CPU-bound операции в отдельный поток
                                content = await asyncio.to_thread(xmltodict.parse, extracted_text)
                                attachment_urls = await asyncio.to_thread(self.extract_urls_from_dict, content)
                                
                                logger.info(f"Найдено ссылок в {file_info['filename']}: {len(attachment_urls)}")

                                return attachment_urls
                                
                            except Exception as e:
                                logger.error(f"Ошибка обработки {file_info.get('filename')}: {e}", exc_info=True)
                                return []
                            
                    
                    # Запускаем обработку всех XML параллельно
                    async with self.semaphore:
                        attachment_urls = await asyncio.gather(
                            *(process_xml_file(f) for f in actual_xml_files),
                            return_exceptions=True
                        )
                    # оставляем уникальные ссылки
                    unique_urls = set([
                        item for sublist in attachment_urls if isinstance(sublist, list)
                        for item in sublist if "zakupki.gov.ru" in item
                    ])
                            
                    # Параллельное скачивание ссылок 
                    async with semaphore:
                        if unique_urls:
                            download_tasks = [
                                self._download_http_file(url=url, procurement_id=procurement_id, source="eis_web", handle_rate_limit=True)
                                for url in unique_urls
                            ]
                            async with self.semaphore:
                                all_results = await asyncio.gather(*download_tasks, return_exceptions=True)
                            
                    # Фильтруем успешные результаты и исключения
                    successful_downloads = [
                        res for res in all_results 
                        if isinstance(res, dict) and res.get("status") == "success"
                    ]
                    
                    if successful_downloads:
                        result["files"] = successful_downloads

                
                return result

        except Exception as e:
            logger.error(f"Ошибка SOAP-парсинга ЕИС: {str(e)}", exc_info=True)
            return {"status": "error", "procurement_number": reg_number, "error": f"Ошибка SOAP-парсинга: {str(e)}"}


    async def parse_cloud_storage_link(self, url: str, procurement_id: str = None) -> Dict:
        """
        Унифицирует обработку ссылок на документы из облаков и портала ЕИС с поддержкой валидации закупок.

        Для ссылок на zakupki.gov.ru возвращает только метаданные закупки (включая номер)
        без скачивания файлов — это позволяет проверить существование закупки, не запуская
        полный анализ. Для всех остальных облачных сервисов (Mail.ru, Google Drive и пр.)
        возвращает список скачанных файлов в единообразном формате. Гарантирует наличие
        ключевых полей (procurement_number, files, source) независимо от источника.

        Args:
            url (str): URL на документ или страницу закупки (поддерживается ЕИС и публичные облака).
            procurement_id (str, optional): Идентификатор закупки в локальной системе для привязки результата.
                По умолчанию None.

        Returns:
            Dict: Словарь с единым интерфейсом ответа:
                - для ЕИС: содержит procurement_number и пустой список files,
                - для облаков: содержит список файлов в поле "files",
                - при ошибке: статус "error" и диагностическое сообщение.
        """
        try:
            # Парсим домен для логики ветвления
            parsed_url = urlparse(url)
            domain = parsed_url.netloc.lower()

            # === 2. Обработка всех остальных ссылок через парсер ===
            result = await self.parse_cloud_link(url, procurement_id)

            # === 3. Унификация ответа ===
            if result["status"] == "success":
                # Уже содержит "files" — как из ЕИС SOAP, так и из облаков
                if "files" in result:
                    return result
                # Одиночный файл (например, из Google Drive) → оборачиваем в список
                else:
                    return {
                        "status": "success",
                        "files": [result],
                        "source": result.get("source", "cloud"),
                        "original_url": url,
                        "procurement_id": procurement_id
                    }
            else:
                # Ошибка парсинга — возвращаем её как есть
                return {
                    "status": "error",
                    "error": result.get("error", "Неизвестная ошибка при обработке ссылки"),
                    "source": result.get("source", "unknown"),
                    "original_url": url,
                    "procurement_id": procurement_id,
                    "files": []
                }

        except Exception as e:
            logger.exception(f"Необработанное исключение в parse_cloud_storage_link для URL: {url}")
            return {
                "status": "error",
                "error": f"Внутренняя ошибка обработки ссылки: {str(e)}",
                "source": "unknown",
                "original_url": url,
                "procurement_id": procurement_id,
                "files": []
            }


    @async_retry(EIS_RETRY_CONFIG)
    async def _download_http_file(
        self,
        url: str,
        procurement_id: str = None,
        source: str = "generic",
        resource_id: str = None,
        file_extension: str = None,
        original_filename: str = None,
        handle_rate_limit: bool = True,
        fallback_formats: List[Tuple[str, str]] = None  # Для Google Sheets и подобных
    ) -> Dict:
        """
        Универсальная функция для скачивания файлов по прямым HTTP/HTTPS-ссылкам.
        
        Args:
            url: Прямая ссылка на файл
            procurement_id: Идентификатор закупки
            source: Источник файла (для логирования)
            resource_id: Идентификатор ресурса (опционально)
            file_extension: Предопределённое расширение (опционально)
            original_filename: Оригинальное имя файла (опционально)
            handle_rate_limit: Обрабатывать ли 429 ошибки (по умолчанию True)
            fallback_formats: Список альтернативных форматов для повторных попыток
                            [(url_template, extension), ...]
        
        Returns:
            Dict с результатом скачивания в унифицированном формате
        """
        try:
            # Ждём «разрешения» от глобального лимитера ПЕРЕД запросом
            await self.rate_limiter.acquire()
            
            headers = {"User-Agent": "Mozilla/5.0 (compatible; procurement-checker/1.0)"}
            session = self.http_manager.get_session()
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                # Обработка 429 (слишком много запросов)
                if handle_rate_limit and resp.status == 429:
                    retry_after = resp.headers.get('Retry-After')
                    retry_after_sec = int(retry_after) if retry_after else None
                    raise EISRateLimitError(
                        f"Rate limit exceeded: {url}",
                        retry_after=retry_after_sec
                    )
                
                # Обработка ошибок с попыткой альтернативных форматов
                if resp.status != 200:
                    if fallback_formats and len(fallback_formats) > 0:
                        # Пробуем следующий формат рекурсивно
                        next_url, next_ext = fallback_formats.pop(0)
                        return await self._download_http_file(
                            url=next_url.format(resource_id),
                            procurement_id=procurement_id,
                            source=source,
                            resource_id=resource_id,
                            file_extension=next_ext,
                            original_filename=original_filename,
                            handle_rate_limit=handle_rate_limit,
                            fallback_formats=fallback_formats
                        )
                    else:
                        error_text = await resp.text()
                        logger.info(f"Ошибка скачивания: {resp.status}, тело: {error_text[:300]}")
                        return {
                            "status": "error",
                            "error": f"HTTP {resp.status}: не удалось скачать файл"
                        }
                
                # Читаем контент
                content = await resp.read()

                # Определяем имя файла
                if original_filename:
                    filename = self._sanitize_filename(original_filename)
                else:
                    filename = self._extract_filename_from_headers(resp.headers, url)
                
                # Определяем расширение
                if not file_extension:
                    file_extension = get_file_extension(filename=filename, content_type=resp.headers.get('Content-Type', ''))
                    if not file_extension or file_extension == '.bin':
                        file_extension = get_file_extension(url=url)
                
                # Финальная очистка имени
                safe_name = self._sanitize_filename(filename)
                if not safe_name.endswith(file_extension):
                    safe_name += file_extension
                
                # Создаём временный файл
                with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as tmp_file:
                    tmp_file.write(content)
                    tmp_path = tmp_file.name
                
                return {
                    "status": "success",
                    "filename": safe_name,
                    "file_path": tmp_path,
                    "source": source,
                    "original_url": url,
                    "procurement_id": procurement_id,
                    "file_extension": file_extension
                }
                    
        except EISRateLimitError:
            logger.error(f"Исчерпаны попытки скачивания {url}")
            return {"status": "error", "error": "Исчерпаны попытки скачивания"}
        except (ConnectionError, TimeoutError):
            logger.error(f"Сетевая ошибка для {url}")
            return {"status": "error", "error": "Network error after retries"}
        except Exception as e:
            logger.warning(f"Ошибка скачивания файла {url}: {str(e)}")
            return {"status": "error", "error": f"Ошибка скачивания: {str(e)}"}


    async def download_inner(self, url: str, procurement_id: str):
        if "api/media/" in url:
            return await self.download_media(url, procurement_id)
        else:
            return await self._download_http_file(url=url, procurement_id=procurement_id, source="inner", handle_rate_limit=True)
        

    @async_retry(MEDIA_RETRY_CONFIG)
    async def download_media(
        self,
        url: str,
        procurement_id: str,
    ):
        """Скачивает файл через ``/api/media`` портала экспертиз (ключ ``SYSTEM_API_KEY``).

        Соединение — из общей сессии ``HTTPClientManager`` (не новая ``aiohttp.ClientSession`` на каждый файл).
        Запросы к одному хосту идут через общий ограничитель процесса (:func:`configs.host_limiter.media_limiter`):
        не больше ``settings.MEDIA_DOWNLOAD_CONCURRENCY`` одновременно, а после ответа 429 пауза ставится на весь
        хост — по ``Retry-After`` или ``settings.PORTAL_RATE_LIMIT_DELAY``. Повторы — ``MEDIA_RETRY_CONFIG``.

        Args:
            url: Ссылка на файл (``.../storage/...`` или ``.../api/media/<id>/<имя>``).
            procurement_id: ID экспертизы — для результата.

        Returns:
            dict: ``status="success"``, ``file_path`` (временный файл), ``filename``, ``file_extension`` и др.

        Raises:
            EISRateLimitError: 429 (повторяется декоратором, после исчерпания попыток — наружу).
            RuntimeError: Другой ответ, кроме 200.
        """
        if "storage" in url:
            url = url.replace("storage", "api/media")
        api_url = "/".join((url.split("/"))[:-1])
        filename = self._extract_filename_from_url(url)

        headers = {
            "X-API-Key": os.getenv("SYSTEM_API_KEY"),
        }

        limiter = media_limiter()
        async with limiter.slot(api_url) as host:
            # общая сессия HTTPClientManager: раньше на каждый файл создавалась своя aiohttp.ClientSession —
            # новое TCP/TLS-соединение и свой пул на каждое скачивание
            session = self.http_manager.get_session()
            async with session.get(
                api_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as response:

                if response.status == 429:
                    retry_after_sec = parse_retry_after(response.headers.get('Retry-After'))
                    # пауза для всех запросов процесса к этому хосту, чтобы они не тратили попытки впустую
                    from configs import settings
                    limiter.penalize(host, retry_after_sec if retry_after_sec is not None else settings.PORTAL_RATE_LIMIT_DELAY)
                    raise EISRateLimitError(
                        f"Rate limit exceeded: {api_url}",
                        retry_after=retry_after_sec
                    )

                if response.status != 200:
                    try:
                        data = await response.json()
                        error = data.get("error", "Неизвестная ошибка")
                    except Exception:
                        error = await response.text()

                    raise RuntimeError(
                        f"Ошибка скачивания media {api_url}: "
                        f"{response.status}: {error}"
                    )
                
                with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
                    async for chunk in response.content.iter_chunked(8192):
                        tmp_file.write(chunk)
                    tmp_path = tmp_file.name

                                
                    # Определяем расширение по Content-Type
                    file_ext = get_file_extension(content_type=response.headers.get('Content-Type', ''))
                    if not file_ext or file_ext == '.bin':
                        file_ext = get_file_extension(url)

                    # Безопасное имя файла
                    safe_name = re.sub(r'[<>:"/\\|?*]', '_', filename)
                    if not safe_name.endswith(file_ext):
                        safe_name += file_ext
                
                return {
                    "status": "success",
                    "filename": filename,
                    "file_path": tmp_path,
                    "source": "inner_media", 
                    "original_url": api_url.replace("api/", ""),
                    "procurement_id": procurement_id,
                    "file_extension": file_ext
                }