import asyncio
import json
from json_repair import repair_json
from typing import Any, Dict
from configs.logger import get_logger
from configs.utils import extract_json_objects



logger = get_logger(__name__)


class ConclusionConsoladator:
    """
    Проверяет полноту и внутреннюю согласованность структурированных данных документа с помощью LLM.

    Класс анализирует JSON-представление извлечённых метаданных (например, номер контракта,
    даты, суммы, организации) и выявляет логические противоречия, пропуски или признаки
    недостоверности (например, дата подписания позже даты выполнения работ).

    Использует guided JSON generation для обеспечения предсказуемого формата ответа.
    """
    def __init__(self, llm_client, model):
        """
        
        """
        self.client = llm_client
        self.model = model


    async def get_rao_conclusion(self, content: str, expertise_id: int, expertise_object: int) -> Dict[str, Any]:
        """
        
        """
        logger.info(f"Формирование сводного заключения эксперта РАО для экспертизы: {expertise_id}")

        try:
            with open("prompts/rao_conclusion_prompt.txt") as f:
                rao_conclusion_prompt = f.read().replace('"object"', f'"{expertise_object}"')

            schema = "schemas/rao_conclusion6_schema.json" if expertise_object in (5,6) else "schemas/rao_conclusion7_schema.json"
            with open(schema) as f:
                RAO_CONCLUSION_SCHEMA = f.read()

            logger.info(f"Content: {content}")

            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": rao_conclusion_prompt
                    },
                    {
                        "role": "user",
                        "content": f"""Сформируй сводное заключение, основываясь на данных, извлеченных из документов, и заключениях экспертов:\n\n{content}"""
                    }
                ],
                max_tokens=2000,
                temperature=0.1,
                extra_body={"guided_json": json.loads(RAO_CONCLUSION_SCHEMA)}
                )
        except asyncio.TimeoutError:
            logger.error(f"Таймаут при формировании сводного заключения для экспертизы: {expertise_id}")
            fallback = {
                "status": "error",
                "conclusion": {},
                "error": "Ошибка при формировании сводного заключения"
            }
            return fallback

        raw_response = response.choices[0].message.content.strip()
        logger.info(f"Сырой ответ модели: {raw_response}")
        
        # Парсим JSON
        try:
            result = json.loads(raw_response)
            logger.info("Удалось распарсить JSON.")
            return result
        
        except json.JSONDecodeError as e:
            logger.warning(f"Первая попытка парсинга JSON не удалась: {e}")

            # Поиск JSON структур вручную
            try:
                result = json.loads(repair_json(raw_response))

                if not result:
                    logger.error("JSON структуры не найдены.")
                    fallback = {
                        "status": "error",
                        "conclusion": {},
                        "error": "Ошибка при формировании сводного заключения"
                    }
                    return fallback
                
                logger.info("Удалось распарсить JSON из извлечённого фрагмента вручную.")
                return result
            
            except json.JSONDecodeError:
                logger.error("Не удалось распарсить ни одну JSON структуру.")
                fallback = {
                    "status": "error",
                    "conclusion": {},
                    "error": "Ошибка при формировании сводного заключения"
                }
                return fallback