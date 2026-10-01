import json
from configs.http_client_manager import HTTPClientManager
import aiohttp
import logging
from typing import Dict, Any

from configs.config import Config


logger = logging.getLogger(__name__)



class SendAIAnalysisService:
    """Сервис для отправки данных во внешнее API (исходная версия)"""
    
    def __init__(self, http_manager: HTTPClientManager, environment: str = None):
        """
        Инициализация сервиса с указанием среды
        
        Args:
            environment: Окружение ('dev', 'stage', 'prod')
        """
        self.config = Config.get_update_ai_db_config(environment)
        self.http_manager = http_manager


    
    async def send_ai_analysis(self, table_name: str, id: int, ai_analysis: str) -> Dict[str, Any]:
        """
        """
        if not ai_analysis:
            logger.warning("Данные не отправлены. Предмет контракта отсутствует")
            return
        
        payload = {
            "table": table_name,
            "id": id,
            "ai_analysis": ai_analysis
        }
        
        logger.info(f" Отправка данных в исходном формате для {table_name}, id={id}")
        
        try:
            session = self.http_manager.get_session()
            async with session.patch(
                    self.config.url,
                    headers=self.config.headers,
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


    async def send_to_db(self, table_name: str, id: int, ai_analysis: str, send_to_external: bool = False):
        """
        """
        if send_to_external:
            await self.send_ai_analysis(table_name, id, ai_analysis)