import requests
import json
import logging
from typing import Dict, Any


from configs.config import Config

logger = logging.getLogger(__name__)



class ExternalAPIService:
    """Сервис для отправки данных во внешнее API (исходная версия)"""
    
    def __init__(self, environment: str = None)::
        self.config = Config.get_external_api_config(environment)
        self.url = self.config["url"]
        self.headers = self.config["headers"]
        
        if not self.url or "ваш-реальный-сайт" in self.url:
            logger.warning(" EXTERNAL_API_URL не настроен в .env файле!")
    
    def send_expertise_data(self, expertise_id: int, merged_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Отправляет данные экспертизы в исходном формате
        
        Headers:
        - X-API-Key: основной ключ
        
        Body:
        {
            "id": 4773,
            "data": { ... }
        }
        """
        payload = {
            "id": expertise_id,
            "data": merged_data
        }
        
        logger.info(f" Отправка данных в исходном формате для expertise_id={expertise_id}")
        logger.debug(f" URL: {self.url}")
        logger.debug(f" Заголовки: {self.headers}")
        logger.debug(f" Payload: {json.dumps(payload, ensure_ascii=False, indent=2)}")
        
        try:
            response = requests.post(
                self.url,
                headers=self.headers,  # ← Только X-API-Key
                data=json.dumps(payload, ensure_ascii=False),
                timeout=30
            )
            
            logger.info(f"✅ Ответ получен: статус {response.status_code}")
            logger.debug(f" Заголовки ответа: {dict(response.headers)}")
            
            response_text = response.text
            logger.debug(f" Тело ответа: {response_text}")
            
            content_type = response.headers.get('Content-Type', '').lower()
            if 'application/json' not in content_type:
                if response.status_code == 200:
                    logger.warning("⚠️ API вернуло успешный статус, но не в формате JSON")
                    return {"status": "success", "message": "Данные успешно обновлены"}
                error_msg = f"API вернуло не-JSON ответ. Content-Type={content_type}, Статус={response.status_code}"
                logger.error(f" {error_msg}")
                raise Exception(error_msg)
            
            try:
                response_data = response.json()
            except json.JSONDecodeError as e:
                if response.status_code == 200:
                    logger.warning(f" Не удалось распарсить JSON, но статус 200: {e}")
                    return {"status": "success", "message": "Данные успешно обновлены"}
                raise Exception(f"Ошибка парсинга JSON: {e}. Ответ: {response_text}")
            
            if response.status_code not in [200, 201]:
                error_detail = (
                    response_data.get('error') or
                    response_data.get('message') or
                    response_data.get('errors', {}).get('data', ['Неизвестная ошибка'])[0] or
                    f"HTTP {response.status_code}"
                )
                error_msg = f"Ошибка API ({response.status_code}): {error_detail}"
                logger.error(f" {error_msg}")
                raise Exception(error_msg)
            
            logger.info(f" Данные успешно отправлены!")
            return response_data
            
        except requests.exceptions.RequestException as e:
            logger.error(f" Ошибка сети: {str(e)}")
            raise Exception(f"Сетевая ошибка: {str(e)}")
        except Exception as e:
            logger.error(f" Критическая ошибка при отправке данных: {str(e)}", exc_info=True)
            raise
