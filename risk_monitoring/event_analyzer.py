import asyncio
import json
from string import Template

from configs.rate_limiter import TokenBucket
from json_repair import repair_json

from configs.logger import get_logger
from typing import Any, Dict

from configs.utils import split_large_text


logger = get_logger(__name__)


EVENT_RISKS_KEYS = {
    # Документные риски
    "DOC-001": "Неполнота документа",
    "DOC-002": "Ограничение конкуренции",
    "DOC-003": "Избыточные или необоснованные требования",
    "DOC-004": "Неоднозначные и субъективные требования",
    "DOC-005": "Внутренние противоречия",
    "DOC-006": "Междокументные несоответствия",
    "DOC-007": "Несоответствие документа структурным данным",
    "DOC-008": "Ошибки классификации и нормативных ссылок",
    "DOC-009": "Изменение содержания документа",
    "DOC-010": "Подозрительное изменение документа",
    "DOC-011": "Аномалии структуры документа",
    "DOC-012": "Повторяемость документных шаблонов",
    "DOC-013": "Неполный комплект документов",

    # Финансовые риски
    "FIN-001": "Аномальная цена",
    "FIN-002": "Демпинг",
    "FIN-003": "Обеспечение заявки",
    "FIN-004": "Обеспечение исполнения и гарантий",
    "FIN-005": "Аванс",
    "FIN-006": "Условия оплаты",
    "FIN-007": "Аномальное изменение финансовых условий",
    "FIN-008": "Несоразмерность цены и исполнения", # ???

    # Процедурные риски
    "PROC-001": "Низкая конкуренция",
    "PROC-002": "Аномальное изменение процедуры",
    "PROC-003": "Нарушение или аномалия сроков процедуры",
    "PROC-004": "Проблемы с заявками",
    "PROC-005": "Единственный поставщик",
    "PROC-006": "Жалобы и процедура",
    "PROC-007": "Уклонение / смена победителя",

    # Контрактные риски
    "CTR-001": "Изменение цены контракта",
    "CTR-002": "Изменение объёма и состава ТРУ",
    "CTR-003": "Изменение сроков контракта",
    "CTR-004": "Изменение финансовых условий контракта",
    "CTR-005": "Необоснованное или несоответствующее основание изменения",
    "CTR-006": "Нарушение сроков исполнения",
    "CTR-007": "Неполное или ненадлежащее исполнение",
    "CTR-008": "Приёмка и подтверждение исполнения",
    "CTR-009": "Расторжение / отказ",
    "CTR-010": "Неприменение ответственности",

   # Поведенческие риски
    # "BEH-001": "Аномальное поведение заказчика",
    # "BEH-002": "Аномальное поведение поставщика",
    # "BEH-003": "Повторяющийся сценарий поведения",
    # "BEH-004": "Аномальная активность",
    "BEH-005": "Аномальное распределение результатов",

    # Исторические риски
    # "HIST-001": "Повторение риска",
    # "HIST-002": "Повторение сценария",
    # "HIST-003": "Повторение рискованного требования",
    # "HIST-004": "Рост / снижение риска",
    # "HIST-005": "Риск после предыдущего события",

    # AI-риски
    "AI-001": "Семантическое ограничение конкуренции",
    "AI-002": "Семантическое противоречие",
    "AI-003": "Неоднозначность / субъективность",
    "AI-004": "Смысловое изменение версии",
    "AI-005": "Необоснованное требование",
    "AI-006": "Новый риск-паттерн",

    # Сетевые риски
    # "NET-001": "Аномальная связь заказчик–поставщик",
    # "NET-002": "Совместное участие участников",
    # "NET-003": "Повторяющееся распределение ролей",
    # "NET-004": "Аномальная группа участников",
    # "NET-005": "Центральный участник сети",
    # "NET-006": "Изменение структуры сети"
}


class EventAnalyzer:
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

        with open("prompts/event_analyzer_prompt.txt") as f:
            self.event_analyzer_prompt = f.read()

        with open("schemas/event_analyzer_schema.json") as f:
            self.event_analyzer_schema = f.read()



    async def check_event_risks(self, content: str, xml_name: str) -> Dict[str, Any]:
        """
        """
        logger.info(f"Анализ события '{xml_name}' на риски")

        # Ждём «разрешения» от глобального лимитера ПЕРЕД запросом
        await self.rate_limiter.acquire()

        # Загружаем промпт
        prompt = self.event_analyzer_prompt.replace("EVENT_RISKS_KEYS", json.dumps(EVENT_RISKS_KEYS))

        schema = self.event_analyzer_schema.replace(
            '"DOC_RISKS_KEYS"', json.dumps(list(EVENT_RISKS_KEYS.keys())))

        chunks = split_large_text(json.dumps(content, ensure_ascii=False, indent=2) if not isinstance(content, str) else content)

        try:
            response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": prompt
                },
                {
                    "role": "user",
                    "content": f"""Проанализируй событие:\n\n{chunks[0]}"""
                }
            ],
            max_tokens=3000,
            temperature=0.1,
            extra_body={"guided_json": schema}
        )
        except asyncio.TimeoutError:
            logger.error(f"Таймаут при анализе {xml_name}")
            fallback = {
                "resume": "Ошибка анализа события",
                "risks": []
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
                        "event_summary": "Ошибка анализа события",
                        "risks": []
                    }
                    return fallback
                
                logger.info("Удалось распарсить JSON из извлечённого фрагмента вручную.")
                return result
            
            except json.JSONDecodeError:
                logger.error("Не удалось распарсить ни одну JSON структуру.")
                fallback = {
                    "event_summary": "Ошибка анализа события",
                    "risks": []
                }
                return fallback