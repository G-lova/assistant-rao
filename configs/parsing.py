import os
import re
import logging
import tempfile
import asyncio
import json
import uuid
import datetime

import aiohttp
import magic
import xmltodict
import httpx
import zipfile
import io
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
            'zakupki.gov.ru': self._parse_eis_soap_from_web,
        }
        
        # Получаем токен ЕИС из переменных окружения
        self.eis_token = os.getenv('ESV_key')



    def is_cloud_link(self, url: str) -> bool:
        """
        Проверяет, относится ли URL к поддерживаемому облачному хранилищу или ЕИС.

        Args:
            url (str): Проверяемый URL.

        Returns:
            bool: True, если домен URL поддерживается или это SOAP запрос, иначе False.
        """
        if url.startswith('soap://'):
            return True
            
        parsed = urlparse(url)
        return parsed.netloc in self.supported_domains


    async def parse_cloud_link(self, url: str, procurement_id: str = None) -> Dict:
        """
        Асинхронно обрабатывает ссылку на документ в облаке или ЕИС.

        Выполняет маршрутизацию к соответствующему парсеру по домену.
        Поддерживает SOAP запросы к ЕИС в формате soap://method?params.

        Args:
            url (str): Ссылка на документ в облачном хранилище или SOAP запрос.
            procurement_id (str, optional): Идентификатор закупки для логирования.
                По умолчанию None.

        Returns:
            Dict: Результат обработки: либо данные для скачивания файла, либо ошибка.
        """
        # Обработка SOAP запросов к ЕИС
        if url.startswith('soap://'):
            return await self._parse_eis_soap(url, procurement_id)
            
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
        soap_url = "https://int44.zakupki.gov.ru/eis-integration/services/getDocsIP"
        headers = {
            'Content-Type': 'text/xml; charset=utf-8',
        }
        
        try:
            async with aiohttp.ClientSession() as session:
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
                        response_dict = xmltodict.parse(response_content)
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


    def _extract_eis_archive_url(self, response_dict: Dict) -> Optional[str]:
        """Извлекает URL архива из ответа SOAP API ЕИС"""
        try:
            logger.info(f"Попытка извлечения URL архива из ответа: {json.dumps(response_dict, indent=2, ensure_ascii=False)}")

            # Основная структура SOAP ответа
            envelope = response_dict.get('soap:Envelope', {})
            body = envelope.get('soap:Body', {})

            # Ищем в различных возможных структурах ответа
            for key, value in body.items():
                if 'Response' in key:
                    data_info = value.get('dataInfo', {})

                    # Если dataInfo - строка (прямой URL)
                    if isinstance(data_info, str):
                        if data_info.startswith('http'):
                            return data_info

                    # Если dataInfo - словарь
                    elif isinstance(data_info, dict):
                        archive_url = data_info.get('archiveUrl')
                        if archive_url and archive_url.startswith('http'):
                            return archive_url

                    # Пробуем найти archiveUrl на верхнем уровне
                    archive_url = value.get('archiveUrl')
                    if archive_url and archive_url.startswith('http'):
                        return archive_url

                    # Пробуем найти href или downloadUrl
                    for field in ['href', 'downloadUrl', 'url']:
                        url_candidate = value.get(field)
                        if url_candidate and url_candidate.startswith('http'):
                            return url_candidate

            # Дополнительные попытки найти URL в других полях
            def find_url_in_dict(d, depth=0):
                if depth > 5:  # Ограничиваем глубину рекурсии
                    return None

                if isinstance(d, dict):
                    for k, v in d.items():
                        if isinstance(v, str) and v.startswith('http') and any(x in v.lower() for x in ['archive', 'download', 'file']):
                            return v
                        elif isinstance(v, (dict, list)):
                            result = find_url_in_dict(v, depth + 1)
                            if result:
                                return result
                elif isinstance(d, list):
                    for item in d:
                        result = find_url_in_dict(item, depth + 1)
                        if result:
                            return result
                return None

            archive_url = find_url_in_dict(response_dict)
            if archive_url:
                return archive_url

            logger.error("Не удалось найти URL архива в ответе ЕИС")
            return None

        except Exception as e:
            logger.error(f"Ошибка извлечения URL архива: {str(e)}")
            return None


    async def _download_eis_archive(self, archive_url: str, identifier: str, procurement_id: str = None) -> Dict:
        """Скачивает и обрабатывает архив из ЕИС"""
        try:
            headers = {
                'individualPerson_token': self.eis_token,
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    archive_url, 
                    headers=headers, 
                    timeout=aiohttp.ClientTimeout(total=120)
                ) as response:
                    if response.status != 200:
                        return {
                            "status": "error",
                            "error": f"Не удалось скачать архив ЕИС: статус {response.status}"
                        }
                    
                    # Читаем содержимое архива
                    archive_content = await response.read()
                    
                    # Обрабатываем архив и извлекаем файлы
                    return await self._process_eis_archive(archive_content, identifier, archive_url, procurement_id)
                    
        except Exception as e:
            logger.error(f"Ошибка скачивания архива ЕИС: {str(e)}")
            return {
                "status": "error", 
                "error": f"Ошибка скачивания архива ЕИС: {str(e)}"
            }


    async def _process_eis_archive(self, archive_content: bytes, identifier: str, 
                                 original_url: str, procurement_id: str = None) -> Dict:
        """Обрабатывает ZIP архив из ЕИС и извлекает файлы"""
        try:
            files = []
            
            with zipfile.ZipFile(io.BytesIO(archive_content)) as zip_file:
                for file_info in zip_file.infolist():
                    if not file_info.is_dir():
                        # Извлекаем файл
                        extracted_data = zip_file.read(file_info.filename)
                        
                        # Определяем расширение
                        _, file_extension = os.path.splitext(file_info.filename)
                        if not file_extension:
                            file_extension = self._get_extension_from_content(extracted_data)
                        
                        # Создаем временный файл
                        with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as tmp_file:
                            tmp_file.write(extracted_data)
                            tmp_path = tmp_file.name
                        
                        # Очищаем имя файла
                        safe_name = re.sub(r'[<>:"/\\|?*]', '_', file_info.filename)
                        
                        files.append({
                            "status": "success",
                            "filename": safe_name,
                            "file_path": tmp_path,
                            "source": "eis_soap",
                            "original_url": original_url,
                            "procurement_id": procurement_id,
                            "file_extension": file_extension
                        })
            
            if not files:
                return {
                    "status": "error",
                    "error": "В архиве ЕИС не найдено файлов"
                }
            
            return {
                "status": "success",
                "files": files,
                "source": "eis_soap",
                "original_url": original_url,
                "procurement_id": procurement_id,
                "archive_size": len(archive_content),
                "file_count": len(files)
            }
            
        except Exception as e:
            logger.error(f"Ошибка обработки архива ЕИС: {str(e)}")
            return {
                "status": "error",
                "error": f"Ошибка обработки архива ЕИС: {str(e)}"
            }


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


    async def _parse_eis_soap_from_web(self, url: str, procurement_id: str = None) -> Dict:
        """
        Автоматически преобразует веб-URL ЕИС в SOAP запрос и получает документы.
        Если SOAP API недоступен, использует веб-скрапинг как fallback.
        """
        try:
            # Извлекаем реестровый номер из URL
            reestr_number = self._extract_reestr_number_from_web_url(url)
            if not reestr_number:
                return {
                    "status": "error",
                    "error": f"Не удалось извлечь реестровый номер из URL ЕИС: {url}"
                }

            logger.info(f"Автоматическое преобразование веб-URL ЕИС в SOAP запрос для реестрового номера: {reestr_number}")

            # Сначала пробуем SOAP API
            soap_result = await self._eis_soap_get_docs_by_reestr_number(
                {'reestrNumber': reestr_number, 'subsystemType': 'PRIZ'}, 
                procurement_id
            )

            # Если SOAP API вернул ошибку авторизации, пробуем веб-скрапинг
            if soap_result.get("status") == "error" and any(error in soap_result.get("error", "") for error in ["токен", "Токен", "token", "Token"]):
                logger.warning("SOAP API недоступен из-за проблем с токеном. Пробуем веб-скрапинг...")
                return await self._parse_eis_web_fallback(url, procurement_id)

            return soap_result

        except Exception as e:
            logger.error(f"Ошибка преобразования веб-URL в SOAP: {str(e)}")
            # При любой ошибке пробуем веб-скрапинг
            logger.warning("Пробуем веб-скрапинг как fallback...")
            return await self._parse_eis_web_fallback(url, procurement_id)


    async def _parse_eis_web_fallback(self, url: str, procurement_id: str = None) -> Dict:
        """
        Fallback метод для парсинга ЕИС через веб-скрапинг когда SOAP API недоступен.
        """
        try:
            logger.info(f"Начало парсинга ЕИС через веб-скрапинг: {url}")
            clean_url = url.strip()
            parsed = urlparse(clean_url)
            query_params = parse_qs(parsed.query)
            reg_number = query_params.get("regNumber", [None])[0]
            notice_info_id = query_params.get("noticeInfoId", [None])[0]

            if not reg_number and not notice_info_id:
                raise Exception("Не найден regNumber или noticeInfoId в URL")

            if "notice223" in clean_url:
                doc_url = f"https://zakupki.gov.ru/epz/order/notice/notice223/documents.html?noticeInfoId={notice_info_id}"
            else:
                path_parts = parsed.path.split('/')
                notice_type = next((part for part in ("zk20", "ea20", "ezt20", "okd20", "okdp20", "oks20") if part in path_parts), "zk20")
                doc_url = f"https://zakupki.gov.ru/epz/order/notice/{notice_type}/view/documents.html?regNumber={reg_number}"

            logger.info(f"URL документов ЕИС: {doc_url}")

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

                    # Извлекаем ссылки из blockFilesTabDocs
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
                            # Извлекаем UID из ссылки
                            parsed_href = urlparse(href)
                            uid = parse_qs(parsed_href.query).get("uid", [None])[0]
                            if not uid:
                                logger.warning(f"Не удалось извлечь uid из ссылки: {href}")
                                continue
                            
                            # Формируем прямую ссылку на файл
                            direct_url = f"https://zakupki.gov.ru/44fz/filestore/public/download/file?uid={uid}"

                            # Скачиваем через aiohttp с заголовками
                            headers = {
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                                "Referer": doc_url,
                                "Accept": "*/*"
                            }

                            async with aiohttp.ClientSession() as session:
                                async with session.get(direct_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                                    if resp.status != 200:
                                        logger.warning(f"Не удалось скачать файл {direct_url}: статус {resp.status}")
                                        continue
                                    
                                    # Определяем расширение по Content-Type
                                    content_type = resp.headers.get('content-type', '')
                                    file_ext = self._get_extension_from_content_type(content_type)
                                    if not file_ext or file_ext == '.bin':
                                        file_ext = self._get_extension_from_url(direct_url)

                                    # Сохраняем во временный файл
                                    with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as tmp:
                                        async for chunk in resp.content.iter_chunked(8192):
                                            tmp.write(chunk)
                                        tmp_path = tmp.name

                                    # Безопасное имя файла
                                    safe_name = re.sub(r'[<>:"/\\|?*]', '_', orig_filename)
                                    if not safe_name.endswith(file_ext):
                                        safe_name += file_ext

                                    results.append({
                                        "status": "success",
                                        "filename": safe_name,
                                        "file_path": tmp_path,
                                        "source": "eis_web",
                                        "original_url": direct_url,
                                        "procurement_id": procurement_id,
                                        "file_extension": file_ext
                                    })

                        except Exception as e:
                            logger.warning(f"Не удалось обработать файл {href}: {e}")
                            continue
                        
                    if not results:
                        raise Exception("Не удалось скачать ни одного документа из ЕИС")

                    return {
                        "status": "success",
                        "files": results,
                        "source": "eis_web",
                        "original_url": url,
                        "procurement_id": procurement_id
                    }

                finally:
                    await browser.close()

        except Exception as e:
            logger.error(f"Ошибка парсинга ЕИС через веб-скрапинг: {str(e)}")
            return {
                "status": "error",
                "error": f"Ошибка парсинга ЕИС: {str(e)}"
            }


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
                query_params.get("reestrNumber", [None])[0] or
                query_params.get("noticeInfoId", [None])[0]
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


    def _get_extension_from_content(self, content: bytes) -> str:
        """
        Определяет расширение файла по MIME-типу.

        Args:
            content_type (str): MIME-тип контента.

        Returns:
            str: Расширение файла (например, ".pdf") или ".bin" по умолчанию.
        """
        try:
            mime = magic.Magic(mime=True)
            mime_type = mime.from_buffer(content)
            
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

            return extension_map.get(mime_type, '.bin')
        except:
            return '.bin'


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
        """
        try:
            logger.info(f"Начало парсинга ЕИС: {url}")
            clean_url = url.strip()
            parsed = urlparse(clean_url)
            query_params = parse_qs(parsed.query)
            reg_number = query_params.get("regNumber", [None])[0]
            notice_info_id = query_params.get("noticeInfoId", [None])[0]
            if not reg_number and not notice_info_id:
                raise Exception("Не найден regNumber или noticeInfoId в URL")
    
            if "notice223" in clean_url:
                doc_url = f"https://zakupki.gov.ru/epz/order/notice/notice223/documents.html?noticeInfoId={notice_info_id}"
            else:
                path_parts = parsed.path.split('/')
                notice_type = next((part for part in ("zk20", "ea20", "ezt20", "okd20", "okdp20", "oks20") if part in path_parts), "zk20")
                doc_url = f"https://zakupki.gov.ru/epz/order/notice/{notice_type}/view/documents.html?regNumber={reg_number}"
    
            logger.info(f"URL документов ЕИС: {doc_url}")
    
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
                            # === ИЗВЛЕКАЕМ UID ИЗ ССЫЛКИ ===
                            parsed_href = urlparse(href)
                            uid = parse_qs(parsed_href.query).get("uid", [None])[0]
                            if not uid:
                                logger.warning(f"Не удалось извлечь uid из ссылки: {href}")
                                continue
                            
                            # === ФОРМИРУЕМ ПРЯМУЮ ССЫЛКУ НА ФАЙЛ ===
                            direct_url = f"https://zakupki.gov.ru/44fz/filestore/public/download/file?uid={uid}"
    
                            # === СКАЧИВАЕМ ЧЕРЕЗ aiohttp С ЗАГОЛОВКАМИ ===
                            headers = {
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                                "Referer": doc_url,
                                "Accept": "*/*"
                            }
    
                            async with aiohttp.ClientSession() as session:
                                async with session.get(direct_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                                    if resp.status != 200:
                                        logger.warning(f"Не удалось скачать файл {direct_url}: статус {resp.status}")
                                        continue
                                    
                                    # Определяем расширение по Content-Type
                                    content_type = resp.headers.get('content-type', '')
                                    file_ext = self._get_extension_from_content_type(content_type)
                                    if not file_ext or file_ext == '.bin':
                                        file_ext = self._get_extension_from_url(direct_url)
    
                                    # Сохраняем во временный файл
                                    with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as tmp:
                                        async for chunk in resp.content.iter_chunked(8192):
                                            tmp.write(chunk)
                                        tmp_path = tmp.name
    
                                    # Безопасное имя файла
                                    safe_name = re.sub(r'[<>:"/\\|?*]', '_', orig_filename)
                                    if not safe_name.endswith(file_ext):
                                        safe_name += file_ext
    
                                    results.append({
                                        "status": "success",
                                        "filename": safe_name,
                                        "file_path": tmp_path,
                                        "source": "eis",
                                        "original_url": direct_url,
                                        "procurement_id": procurement_id,
                                        "file_extension": file_ext
                                    })
    
                        except Exception as e:
                            logger.warning(f"Не удалось обработать файл {href}: {e}")
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

    
    async def _extract_real_file_url_from_eis_page(self, page, file_html_url: str) -> Optional[str]:
        """
        Загружает страницу вида .../file.html?uid=... и извлекает настоящую ссылку на файл
        из JavaScript-переменной window.__PRELOADED_STATE__.
        """
        try:
            await page.goto(file_html_url, wait_until="domcontentloaded", timeout=10000)
            # Получаем весь HTML
            content = await page.content()
            # Ищем __PRELOADED_STATE__
            if "__PRELOADED_STATE__" in content:
                # Извлекаем JSON из скрипта
                start = content.find("window.__PRELOADED_STATE__ = ") + len("window.__PRELOADED_STATE__ = ")
                end = content.find("};", start) + 1
                if start > len("window.__PRELOADED_STATE__ = ") and end > start:
                    json_str = content[start:end]
                    data = json.loads(json_str)
                    # Извлекаем URL файла
                    file_url = data.get("file", {}).get("url")
                    if file_url:
                        return file_url
            return None
        except Exception as e:
            logger.error(f"Ошибка извлечения реальной ссылки из {file_html_url}: {e}")
            return None


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