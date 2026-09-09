import json
from configs.http_client_manager import HTTPClientManager
import aiohttp
import logging
from typing import Dict, Any

from configs.config import Config


logger = logging.getLogger(__name__)



class SendSubjectService:
    """Сервис для отправки данных во внешнее API (исходная версия)"""
    
    def __init__(self, http_manager: HTTPClientManager, environment: str = None):
        """
        Инициализация сервиса с указанием среды
        
        Args:
            environment: Окружение ('dev', 'stage', 'prod')
        """
        self.config = Config.get_update_db_config(environment)
        self.url = self.config.url
        self.headers = self.config.headers
        self.http_manager = http_manager

        key = self.headers.get("X-API-Key", "")
        logger.info(f"X-API-Key: '{key[:4]}...' (len={len(key)})")
        
        if not self.url or "сайт.ru" in self.url:
            logger.warning(f"UPDATE_API_URL не настроен для среды {environment} в .env файле!")
        else:
            logger.info(f"Инициализирован UpdateAPIService для среды {environment}")
            logger.debug(f"URL: {self.url}")
            logger.debug(f"Headers: {self.headers}")


    
    async def send_expertise_subject(self, expertise_id: int, text: str) -> Dict[str, Any]:
        """
        Отправляет данные экспертизы в исходном формате

        Headers:
        - X-API-Key: основной ключ
        
        Body:
        {
            "expertises": [{
                "id": 5290,
                "subjectContract": "..."
            }]
        }
        """
        if not text:
            logger.warning("Данные не отправлены. Предмет контракта отсутствует")
            return
        
        payload = {
            "expertises": [{
                "id": expertise_id,
                "subjectContract": text
            }]
        }
        
        logger.info(f" Отправка данных в исходном формате для expertise_id={expertise_id}")
        logger.debug(f" URL: {self.url}")
        logger.debug(f" Заголовки: {self.headers}")
        logger.debug(f" Payload: {json.dumps(payload, ensure_ascii=False, indent=2)}")
        
        try:
            session = self.http_manager.get_session()
            async with session.post(
                    self.url,
                    headers=self.headers,
                    json=payload
                ) as response:
                response_data = await response.json()

            logger.info(f"Ответ получен: статус {response.status}")
            logger.debug(f"Заголовки ответа: {dict(response.headers)}")

            # Обработка HTTP-ошибок
            if response.status not in (200, 201):
                error_detail = (
                    response_data.get('error') or
                    response_data.get('message') or
                    response_data.get('errors', {}).get('data', ['Неизвестная ошибка'])[0] or
                    f"HTTP {response.status}"
                )
                error_msg = f"Ошибка API ({response.status}): {error_detail}"
                logger.error(f"❌ {error_msg}")
                raise Exception(error_msg)

            logger.info("Данные успешно отправлены!")
            return response_data

        except aiohttp.ClientError as e:
            logger.error(f"Ошибка сети: {str(e)}")
            raise Exception(f"Сетевая ошибка: {str(e)}")

        except Exception as e:
            logger.error(f"Критическая ошибка при отправке данных: {str(e)}", exc_info=True)
            raise 


    async def send_subject(self, expertise_id, subject, send_to_external = False):
        """
        Отправляет предмет контракта для указанной экспертизы

        Args:
            expertise_id (int): ID экспертизы
            subject (str): Предмет контракта
            send_to_external (bool): Флаг отправки во внешний API

        Returns:
            Dict[str, Any]: Результат отправки
        """
        if send_to_external:
            await self.send_expertise_subject(expertise_id, subject)