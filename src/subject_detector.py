import asyncio
import json
from typing import Any, Dict
from configs.config import Config
from configs.data_fetcher import DataFetcher
from configs.http_client_manager import HTTPClientManager
from configs.llm_client import get_llm
from configs.logger import get_logger
from json_repair import repair_json



logger = get_logger(__name__)


class SubjectDetector:
    """
    Класс для определения предмета контракта с использованием LLM и отправки данных во внешний API.
    """
    def __init__(self, http_manager: HTTPClientManager, environment: str):
        """
        Инициализация SubjectDetector с указанием среды.
        """
        self.client, self.model = get_llm()
        self.config = Config()
        db_config = self.config.get_database_config(environment)
        self.data_fetcher = DataFetcher(db_config.url, db_config.headers, http_manager)  


    async def get_subject(self, expertise_id: int, content: str) -> Dict[str, Any]:
        """
        Получает предмет контракта для указанной экспертизы.
        """
        logger.info(f"Определение предмета контракта для экспертизы: {expertise_id}")

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": """
                            Ищи раздел 'procurement_subject'. 
                            Укажи предмет контракта, описаный в 'description', None - если не удалось найти предмет контракта. 
                            Верни json с обязательными полями: 'status' ("success" / "error"), 'subject'.
                        """
                    },
                    {
                        "role": "user",
                        "content": f"""Определи предмет контракта:\n\n{content}"""
                    }
                ],
                max_tokens=4096,
                temperature=0.1,
                extra_body={"guided_json": {
                    "type": "object",
                    "required": ["status", "expertise_id", "subject"],
                    "properties": {
                        "status": {
                            "type": "string",
                            "description": "Статус исполнения запроса ('success' / 'error')",
                            "enum": ["success", "error"]
                        },
                        "expertise_id": {
                            "type": "integer",
                            "description": "Номер экспертизы",
                            "enum": [expertise_id]
                        },
                        "subject": {
                            "type": ["string", "null"],
                            "description": "Предмет контракта"
                        },
                        "error": {
                            "type": "string",
                            "description": "Ошибка исполнения запроса"
                        }
                    }
                            
                }}
                )
        except asyncio.TimeoutError:
            logger.error(f"Таймаут при определении предмета контракта для экспертизы: {expertise_id}")
            return {"status": "error", "expertise_id": expertise_id, "subject": None, "error": "Таймаут при определении предмета контракта"}

        raw_response = response.choices[0].message.content.strip()
        logger.info(f"Сырой ответ модели: {raw_response}")
        
        # Парсим JSON
        try:
            result = json.loads(raw_response)
            logger.info(f"Удалось распарсить JSON: {result}")
            if isinstance(result, str):
                result = {"status": "success", "expertise_id": expertise_id, "subject": result}

            return result
        
        except json.JSONDecodeError as e:
            logger.warning(f"Первая попытка парсинга JSON не удалась: {e}")

        # Поиск JSON структур вручную
        try:
            result = json.loads(repair_json(raw_response))

            if not result:
                return {"status": "error", "expertise_id": expertise_id, "subject": None, "error": "Ошибка парсига"}
            
            logger.info(f"Удалось распарсить JSON из извлечённого фрагмента вручную: {result}")
            if isinstance(result, int):
                result = {"status": "success", "expertise_id": expertise_id, "subject": result}
            return result
        
        except json.JSONDecodeError:
            logger.error("Не удалось распарсить ни одну JSON структуру.")
            return {"status": "error", "expertise_id": expertise_id, "subject": None, "error": "Ошибка парсинга"}



    async def get_subject_from_typeDopDetal(self, expertise_id: int):
        """
        Получает предмет контракта из дополнительных деталей экспертизы.
        """
        sql_query = """
            SELECT JSON_EXTRACT(typeDopDetal, '$.ai_doc_answer') AS ai_doc_answer
            FROM expertises
            WHERE id = ?
        """

        df_ai_answer = await self.data_fetcher.fetch_async_expertise_data(sql_query, bindings=[expertise_id])
        ai_doc_answer = df_ai_answer.iloc[0]["ai_doc_answer"]

        if not ai_doc_answer:
            return {"status": "error", "expertise_id": expertise_id, "subject": None, "error": "Отсутствуют извлеченные данные"}

        if not isinstance(ai_doc_answer, str):
            ai_doc_answer = json.loads(ai_doc_answer)

        result = await self.get_subject(expertise_id, ai_doc_answer)

        return result