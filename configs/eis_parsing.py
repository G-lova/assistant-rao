import base64
import aiofiles
import asyncio
import datetime
import os
import tempfile
import uuid
from typing import Dict
from urllib.parse import parse_qs, urlparse

import aiohttp
import xmltodict

from configs.eis_common import EISSoapMixin
from configs.http_client_manager import HTTPClientManager
from configs.logger import get_logger
from configs.rate_limiter import TokenBucket
from configs.retry_utils import EIS_RETRY_CONFIG, EISRateLimitError, async_retry
from configs.resources import resource_path
from configs.utils import get_file_extension

logger = get_logger(__name__)


class EISParser(EISSoapMixin):
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
        self.rate_limiter = TokenBucket(rate=1.5)  # 1.5 запроса в секунду — безопасно для ЕИС

    
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
            async with aiofiles.open(resource_path(f'xml/{request_method}.xml'), 'r', encoding='utf-8') as file:
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
                logger.debug(f"Ответ ЕИС: {soap_response}")
                response_content = await asyncio.to_thread(xmltodict.parse, soap_response)

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
                    "no_data": self._eis_no_data(response_content),
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