import asyncio
import json
from typing import Any, Dict
from configs.llm_client import get_llm
from configs.logger import get_logger
from configs.working_with_db import get_async_summary_report_from_db
from json_repair import repair_json



logger = get_logger(__name__)


class LawDetector:
    """
    Проверяет полноту и внутреннюю согласованность структурированных данных документа с помощью LLM.

    Класс анализирует JSON-представление извлечённых метаданных (например, номер контракта,
    даты, суммы, организации) и выявляет логические противоречия, пропуски или признаки
    недостоверности (например, дата подписания позже даты выполнения работ).

    Использует guided JSON generation для обеспечения предсказуемого формата ответа.
    """
    def __init__(self):
        """
        
        """
        self.client, self.model = get_llm()


    async def get_law(self, expertise_id: int) -> Dict[str, Any]:
        """
        
        """
        logger.info(f"Определение основного закона для экспертизы: {expertise_id}")
            
        # Получение данных о загруженных документах
        summary_report = await get_async_summary_report_from_db(expertise_id)
        if isinstance(summary_report, str):
            try:
                summary_report = json.loads(summary_report)
            except Exception:
                pass

        if not summary_report:
            summary_report = {}
        
        content =  {
            "summary_report": summary_report
        }

        try:
            logger.info(f"Content: {content}")

            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "Ищи раздел 'law_references'. Верни строго код закона: 4 - для ФЗ-44, 5 - для ФЗ-223, 0 - если не удалось определить закон."
                    },
                    {
                        "role": "user",
                        "content": f"""Определи основной закон проведения экспертизы:\n\n{content}"""
                    }
                ],
                max_tokens=4096,
                temperature=0.1,
                extra_body={"guided_json": {
                    "type": "object",
                    "required": ["expertise_id", "law"],
                    "properties": {
                        "expertise_id": {
                            "type": "integer",
                            "description": "Номер экспертизы",
                            "enum": [expertise_id]
                        },
                        "law": {
                            "type": "integer",
                            "description": "Код закона (4: ФЗ-44, 5: ФЗ-223, 0: не удалось определить)",
                            "enum": [4,5,0]
                        }
                    }
                            
                }}
                )
        except asyncio.TimeoutError:
            logger.error(f"Таймаут при определении закона для экспертизы: {expertise_id}")
            return {"expertise_id": expertise_id, "law": 0}

        raw_response = response.choices[0].message.content.strip()
        logger.info(f"Сырой ответ модели: {raw_response}")
        
        # Парсим JSON
        try:
            result = json.loads(raw_response)
            logger.info(f"Удалось распарсить JSON: {result}")
            if isinstance(result, int):
                result = {"expertise_id": expertise_id, "law": result}
            return result
        
        except json.JSONDecodeError as e:
            logger.warning(f"Первая попытка парсинга JSON не удалась: {e}")

        # Поиск JSON структур вручную
        try:
            result = json.loads(repair_json(raw_response))

            if not result:
                return {"expertise_id": expertise_id, "law": 0}
            
            logger.info(f"Удалось распарсить JSON из извлечённого фрагмента вручную: {result}")
            if isinstance(result, int):
                result = {"expertise_id": expertise_id, "law": result}
            return result
        
        except json.JSONDecodeError:
            logger.error("Не удалось распарсить ни одну JSON структуру.")
            return {"expertise_id": expertise_id, "law": 0}



if __name__ == "__main__":

    expertise_id = int(input('ID экспертизы:'))
    law_detector = LawDetector()
    asyncio.run(law_detector.get_law(expertise_id))