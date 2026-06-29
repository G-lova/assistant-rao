import base64

import aiofiles
import os
import re
import tempfile
import asyncio
import json
import uuid
import datetime

import aiohttp
import magic
import xmltodict
from configs.config import RetryConfig
from configs.file_reader import FileReader
from configs.http_client_manager import HTTPClientManager
from configs.logger import get_logger
from configs.rate_limiter import TokenBucket
from configs.retry_utils import EIS_RETRY_CONFIG, EISArchiveNotReadyError, EISRateLimitError, async_retry
from configs.utils import get_file_extension
import zipfile
import io
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple
from playwright.async_api import async_playwright, Browser
from urllib.parse import unquote, urljoin, urlparse, parse_qs
from bs4 import BeautifulSoup
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from socket import AF_INET

logger = get_logger(__name__)


class EISParser:
    """
    Парсер для извлечения документов из популярных облачных хранилищ.

    Поддерживает Google Drive, Google Docs (включая таблицы и презентации),
    Яндекс.Диск и облако Mail.ru. Предоставляет единый интерфейс для проверки
    и асинхронной обработки ссылок, включая скачивание файлов и подготовку
    их для дальнейшего анализа.
    """
    
    def __init__(self, http_manager: HTTPClientManager):
        """Инициализирует парсер с маппингом поддерживаемых доменов и соответствующих методов обработки."""
        # Получаем токен ЕИС из переменных окружения
        self.eis_token = os.getenv('EIS_INDIVIDUAL_PERSON_TOKEN')
        self.eis_soap_url = os.getenv('EIS_SOAP_URL')

        self.http_manager = http_manager
        # self.file_reader = FileReader()
        self.rate_limiter = TokenBucket(rate=1.5)  # 1.5 запроса в секунду — безопасно для ЕИС

    
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
                            
                    

            logger.error("Не удалось найти URL архива в ответе ЕИС")
            return None

        except Exception as e:
            logger.error(f"Ошибка извлечения URL архива: {str(e)}")
            return None


    @async_retry(EIS_RETRY_CONFIG)
    async def _download_eis_archive(self, archive_url: str, identifier: str, procurement_id: str = None) -> Dict:
        """Скачивает и обрабатывает архив из ЕИС"""
        try:
            session = self.http_manager.get_session() # ✅ Используем общий пул
            headers = {
                'individualPerson_token': self.eis_token,
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
        
            logger.info(f"→ Запрос архива: {archive_url[:100]}...")
            
            # async with aiohttp.ClientSession() as session:
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
                        "error": f"Не удалось скачать архив ЕИС: статус {response.status}"
                    }
                
                # Успех
                archive_content = await response.read()
                logger.info(f"Архив скачан: {len(archive_content)} байт")
                
                return await self._process_eis_archive(archive_content, identifier, archive_url, procurement_id)
                        
        except EISArchiveNotReadyError:
            # Переподнимаем, чтобы декоратор retry мог перехватить
            raise

        except Exception as e:
            # Все остальные ошибки — логируем и возвращаем как error-ответ
            logger.error(f"❌ Исключение при скачивании архива ЕИС: {type(e).__name__}: {str(e)}", exc_info=True)
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
                            file_extension = get_file_extension(content=extracted_data)
                        
                        # Создаем временный файл
                        with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as tmp_file:
                            tmp_file.write(extracted_data)
                            tmp_path = tmp_file.name
                    
                        # Кодируем содержимое в base64 вместо сохранения на диск
                        file_content_b64 = base64.b64encode(extracted_data).decode('utf-8')
                        
                        # Очищаем имя файла
                        safe_name = re.sub(r'[<>:"/\\|?*]', '_', file_info.filename)
                        
                        files.append({
                            "status": "success",
                            "filename": safe_name,
                            "file_path": tmp_path,
                            "source": "eis_soap",
                            "original_url": original_url,
                            "content_base64": file_content_b64,
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
        

    async def _parse_eis_soap_ip(self, 
                                 request_method: str, 
                                 subsystem_type: str = "PRIZ", 
                                 reg_number: str = "", 
                                 org_region: str = "", 
                                 fz: int = 44,
                                 document_type: str = "contract", 
                                 nsi_code: str = "nsiAllList",
                                 nsi_kind: str = "all",
                                 exact_date: str = "",
                                 procurement_id: str = None) -> Dict:
        """
        Парсинг закупки через официальный SOAP API ЕИС для физических лиц.
        Использует individualPerson_token и метод getDocsByReestrNumber.
        Сервисы принимают следующие запросы:
        • getDocsByReestrNumberRequest – запрос формирования в ХД архивов с
        документами по реестровому номеру;
        • getDocsByOrgRegionRequest – запрос формирования в ХД архивов с
        документами по региону заказчика и типу документа (только для
        юридических и физических лиц);
        • getNsiRequest – запрос в ХД данных справочника.
        """
        try:
            logger.info(f"Запрос документов ЕИС через SOAP API: {request_method}")

            # === 2. Формируем SOAP-запрос ===
            async with aiofiles.open(f'xml/{request_method}.xml', 'r', encoding='utf-8') as file:
                xml_content = await file.read()
                generated_uuid = str(uuid.uuid4())
                generated_datetime = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                document_type_fz = f"<documentType{fz}>{document_type}</documentType{fz}>"
                nsi_code_fz = f"<nsiCode{fz}>{nsi_code}</nsiCode{fz}>"
                soap_body = (xml_content
                    .replace("{{ UUID }}", generated_uuid)
                    .replace("{{ datetime }}", generated_datetime)
                    .replace("{{ token }}", self.eis_token)
                    .replace("{{ subsystem_type }}", subsystem_type)
                    .replace("{{ reg_number }}", reg_number)
                    .replace("{{ document_type_fz }}", document_type_fz)
                    .replace("{{ org_region }}", org_region)
                    .replace("{{ exact_date }}", exact_date)
                    .replace("{{ nsi_code_fz }}", nsi_code_fz)
                    .replace("{{ nsi_kind }}", nsi_kind)
                )

            session = self.http_manager.get_session() # ✅ Используем общий пул
            headers = {
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": "\"\""
            }

            # === 3. Отправляем запрос ===
            # async with aiohttp.ClientSession() as session:
            async with session.post(
                self.eis_soap_url,
                data=soap_body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"SOAP ошибка: {response.status}, тело: {error_text}")
                    return {"status": "error", "error": f"SOAP ошибка: {response.status}", "eis_response": {}}

                soap_response = await response.text()
                logger.info(f"Ответ ЕИС: {soap_response}")
                response_content = await asyncio.to_thread(xmltodict.parse, soap_response)
                print(response_content)

            # === 4. Парсим archiveUrl ===
            archive_url = self._extract_eis_archive_url(response_content)
            if not archive_url:
                error_info = self._extract_eis_error_info(response_content)
                if error_info:
                    return {
                        "status": "error", 
                        "error": f"Ошибка парсинга ЕИС: {error_info}",
                        "eis_response": response_content
                        }
                return {
                    "status": "error", 
                    "error": "Не найден archiveUrl в ответе ЕИС",
                    "eis_response": response_content
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
                        "eis_response": response_content,
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
                        "error": f"Не удалось скачать ни один архив. Ошибки: {errors}",
                        "eis_response": response_content
                    }
            else:
                # Один URL — обрабатываем как раньше
                result = await self._download_eis_archive(archive_url, reg_number, procurement_id)
                result["eis_response"] = response_content

                # logger.info(f"result before: {result}")

                # скачиваем закупочную документацию
                if request_method == 'getDocsByReestrNumberRequest' and result["status"] == "success":
                    logger.info(f"request_method = 'getDocsByReestrNumberRequest'")

                    # Фильтруем только успешные XML-файлы
                    xml_files = [
                        f for f in result["files"] 
                        if f["status"] == "success" and f["file_extension"] == ".xml"
                    ]
                    
                    if not xml_files:
                        return result

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
                                
                                logger.info(f"Найдено ссылок в {file_info['file_path']}: {len(attachment_urls)}")

                                return attachment_urls
                                
                            except Exception as e:
                                logger.error(f"Ошибка обработки {file_info.get('file_path')}: {e}", exc_info=True)
                                return []
                            
                    
                    # Запускаем обработку всех XML параллельно
                    attachment_urls = await asyncio.gather(
                        *(process_xml_file(f) for f in xml_files),
                        return_exceptions=True
                    )
                    # оставляем уникальные ссылки
                    unique_urls = set([
                        item for sublist in attachment_urls if isinstance(sublist, list)
                        for item in sublist
                    ])
                            
                    # Параллельное скачивание ссылок 
                    async with semaphore:
                        if unique_urls:
                            download_tasks = [
                                self._download_eis_file(file_url=url, procurement_id=None)
                                for url in unique_urls
                            ]
                            all_results = await asyncio.gather(*download_tasks, return_exceptions=True)
                            
                    # Фильтруем успешные результаты и исключения
                    successful_downloads = [
                        res for res in all_results 
                        if isinstance(res, dict) and res.get("status") == "success"
                    ]
                    
                    if successful_downloads:
                        result["files"].extend(successful_downloads)
                        result["archive_size"] += sum(res["file_size"] for res in successful_downloads)
                        result["file_count"] += len(successful_downloads)
                
                return result

        except Exception as e:
            logger.error(f"Ошибка SOAP-парсинга ЕИС: {str(e)}", exc_info=True)
            return {"status": "error", "error": f"Ошибка SOAP-парсинга: {str(e)}", "eis_response": {}}


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

    def _extract_filename_from_url(self, url: str) -> str:
        """Извлекает имя файла из параметра uid или пути URL"""
        try:
            # Пробуем извлечь uid как идентификатор
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            uid = params.get('uid', [None])[0]
            if uid:
                return f"file_{uid}"
            
            # Fallback: последняя часть пути
            path = parsed.path.rstrip('/')
            return path.split('/')[-1] or "eis_file"
        except:
            return "eis_file"
    

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


    def _sanitize_filename(self, filename: str) -> str:
        """Очищает имя файла от недопустимых символов"""
        # Удаляем проблемные символы для разных ОС
        sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '_', filename)
        # Ограничиваем длину
        if len(sanitized) > 200:
            name, ext = os.path.splitext(sanitized)
            sanitized = name[:190] + ext
        return sanitized.strip() or "eis_file"
        


    @async_retry(EIS_RETRY_CONFIG)
    async def _download_eis_file(self, file_url: str, procurement_id: str = None) -> Dict:
        """Скачивает и обрабатывает архив из ЕИС"""
        try:
            # Ждём «разрешения» от глобального лимитера ПЕРЕД запросом
            await self.rate_limiter.acquire()

            session = self.http_manager.get_session() # ✅ Используем общий пул
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
        
            logger.info(f"Запрос файла: {file_url[:100]}...")
            
            # async with aiohttp.ClientSession() as session:
            async with session.get(
                file_url, 
                headers=headers, 
                timeout=aiohttp.ClientTimeout(total=120)
            ) as response:
            
                # Обработка 429 (слишком много запросов) — Retry-After или экспоненциальная задержка
                if response.status == 429:
                    retry_after = response.headers.get('Retry-After')
                    retry_after_sec = int(retry_after) if retry_after else None
                    raise EISRateLimitError(
                        f"Rate limit exceeded: {file_url}", 
                        retry_after=retry_after_sec
                    )                    
                
                # Другие ошибки HTTP
                if response.status != 200:
                    error_text = await response.text()
                    logger.info(f"Ошибка скачивания: {response.status}, тело: {error_text[:300]}")
                    return {
                        "status": "error",
                        "error": f"Не удалось скачать файл ЕИС: статус {response.status}"
                    }
                
                # Читаем контент
                content = await response.read()
                
                # Извлекаем имя файла из заголовков
                filename = self._extract_filename_from_headers(response.headers, file_url)
                
                # Определяем расширение
                file_extension = get_file_extension(filename=filename, headers=response.headers)
                
                # Очищаем имя файла
                safe_name = self._sanitize_filename(filename)
                if not safe_name.endswith(file_extension):
                    safe_name += file_extension
                
                # Кодируем контент в base64
                content_base64 = base64.b64encode(content).decode('utf-8')
                
                # Создаём временный файл (опционально)
                with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as tmp_file:
                    tmp_file.write(content)
                    tmp_path = tmp_file.name
                
                # Формируем результат в нужном формате
                return {
                    "status": "success",
                    "filename": safe_name,
                    "file_path": tmp_path,
                    "source": "eis_soap",
                    "original_url": file_url,
                    "content_base64": content_base64,
                    "procurement_id": procurement_id,
                    "file_extension": file_extension,
                    "file_size": len(content),
                    "content_type": response.headers.get('Content-Type', 'application/octet-stream')
                }
        
        except EISRateLimitError:
            # Это должно было быть обработано декоратором; если дошли сюда — исчерпаны попытки
            logger.error(f"Исчерпаны попытки для {file_url}")
            return {"status": "error", "error": "Rate limit exceeded after all retries"}
        except (ConnectionError, TimeoutError):
            logger.error(f"Сетевая ошибка для {file_url}")
            return {"status": "error", "error": "Network error after retries"}
        except Exception as e:
            # Все остальные — логируем и возвращаем как ошибку
            logger.warning(f"Некритичная ошибка: {type(e).__name__}: {str(e)}")
            return {"status": "error", "error": f"Ошибка: {str(e)}"}
        
        
    def _make_unique_filename(self, filename: str, existing: set) -> str:
        """Делает имя файла уникальным в рамках одного архива"""
        if filename not in existing:
            existing.add(filename)
            return filename
        
        name, ext = os.path.splitext(filename)
        counter = 1
        while True:
            new_name = f"{name}_{counter}{ext}"
            if new_name not in existing:
                existing.add(new_name)
                return new_name
            counter += 1