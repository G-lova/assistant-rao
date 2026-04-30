import asyncio
import json

from configs.rate_limiter import TokenBucket
from json_repair import repair_json

from configs.logger import get_logger
from typing import Any, Dict

from configs.utils import extract_json_objects


logger = get_logger(__name__)


class CompletenessChecker:
    """
    Проверяет полноту и внутреннюю согласованность структурированных данных документа с помощью LLM.

    Класс анализирует JSON-представление извлечённых метаданных (например, номер контракта,
    даты, суммы, организации) и выявляет логические противоречия, пропуски или признаки
    недостоверности (например, дата подписания позже даты выполнения работ).

    Использует guided JSON generation для обеспечения предсказуемого формата ответа.
    """
    def __init__(self, llm_client, model):
        """
        Инициализирует проверяющий модуль с клиентом LLM и названием модели.

        Загружает:
            - промпт для анализа согласованности из файла `prompts/consistency_prompt.txt`,
            - JSON-схему из `schemas/consistency_schema.json` (в виде строки для `guided_json`).

        Args:
            llm_client: Экземпляр клиента LLM (совместимого с OpenAI API),
                        поддерживающего метод `chat.completions.create`.
            model (str): Название модели LLM.
        """
        self.client = llm_client
        self.model = model

        self.rate_limiter = TokenBucket(rate=1.5)  # 1.5 запроса в секунду

        with open("prompts/completeness_prompt.txt") as f:
            self.completeness_prompt = f.read()

        with open("schemas/completeness_schema.json") as f:
            self.COMPLETENESS_SCHEMA = f.read()


    async def check_doc_completeness(self, content: str, document_name: str) -> Dict[str, Any]:
        """
        Анализирует согласованность и полноту данных, извлечённых из документа.

        Метод передаёт структурированные данные (в виде строки JSON или текста) в LLM,
        которая оценивает их на предмет:
            - логических противоречий (например, даты, суммы),
            - отсутствующих обязательных полей,
            - признаков фальсификации или ошибок распознавания.

        Поддерживает устойчивую обработку ответа: если LLM возвращает неформатированный
        текст, метод пытается извлечь валидный JSON с помощью вспомогательной функции
        `extract_json_objects`.

        Args:
            content (str): Строка с данными документа для анализа (обычно JSON или
                           структурированный текст с извлечёнными полями).

        Returns:
            Dict[str, Any]: Словарь с результатом проверки, содержащий:
                - "status": "approve" (данные согласованы) или "deny" (обнаружены проблемы),
                - "description": строка описаний выявленных несоответствий.
                В случае неустранимых ошибок парсинга возвращается резервный словарь
                с "status": "deny" и обобщённым сообщением об ошибке.
        """
        logger.info(f"Анализ на полноту и согласованность данных в документе")
        logger.info(f"completeness_content: {content}")

        # Ждём «разрешения» от глобального лимитера ПЕРЕД запросом
        await self.rate_limiter.acquire()

        try:
            response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": self.completeness_prompt
                },
                {
                    "role": "user",
                    "content": f"""Проанализируй данные, извлеченные из документа:\n\n{content}"""
                }
            ],
            max_tokens=500,
            temperature=0.1,
            extra_body={"guided_json": self.COMPLETENESS_SCHEMA}
        )
        except asyncio.TimeoutError:
            logger.error(f"Таймаут при анализе {document_name}")
            fallback = {
                "status": "deny",
                "description": "Ошибка анализа данных в документе"
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
                        "status": "deny",
                        "description": "Ошибка анализа данных в документе"
                    }
                    return fallback
                
                logger.info("Удалось распарсить JSON из извлечённого фрагмента вручную.")
                logger.info(f"type: {type(result)}, completeness: {result}")
                return result
            
            except json.JSONDecodeError:
                logger.error("Не удалось распарсить ни одну JSON структуру.")
                fallback = {
                    "status": "deny",
                    "description": "Ошибка анализа данных в документе"
                }
                return fallback