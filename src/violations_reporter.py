import asyncio
import json
from configs.rate_limiter import TokenBucket
from configs.retry_utils import LLM_RETRY_CONFIG, async_retry
from json_repair import repair_json

from configs.logger import get_logger
from typing import Any, Dict, List

from configs.utils import split_large_text


logger = get_logger(__name__)


class ViolationsReporter:
    """
    Оркестратор генерации аналитических отчетов с использованием LLM.
    
    Класс реализует паттерн Map-Reduce для обработки больших объемов данных:
    1. Разбивает входные данные на чанки (если они превышают лимит токенов).
    2. Параллельно анализирует каждый чанк с учетом ограничений параллелизма (Semaphore) 
       и частоты запросов (Rate Limiter).
    3. Синтезирует итоговый отчет на основе промежуточных результатов.
    
    Attributes:
        client: Клиент для взаимодействия с LLM API.
        model (str): Название используемой LLM модели.
        semaphore (asyncio.Semaphore): Ограничитель одновременных асинхронных задач.
        rate_limiter (TokenBucket): Алгоритм Token Bucket для ограничения частоты запросов к API.
    """
    def __init__(self, llm_client, model):
        """
        Инициализация оркестратора отчетов.
        
        Args:
            llm_client: Инициализированный клиент LLM (например, AsyncOpenAI или vLLM).
            model (str): Идентификатор модели (например, 'meta-llama/Llama-3-70b').
        """
        self.client = llm_client
        self.model = model


        self.semaphore = asyncio.Semaphore(5)
        self.rate_limiter = TokenBucket(rate=1.5)  # 1.5 запроса в секунду
            

    async def analize_data_from_content(self, metric: str, filters: Dict[str, Any], data: Dict[str, Any], charts: List) -> Dict[str, Any]:
        """
        Главный метод анализа данных. Реализует логику чанкинга и агрегации результатов.
        
        Если объем данных мал (один чанк), отчет генерируется сразу.
        Если данные большие, они разбиваются на части, анализируются параллельно, 
        а затем результаты объединяются в финальный синтезирующий запрос к LLM.
        
        Args:
            metric (str): Название метрики для контекста промпта.
            filters (Dict[str, Any]): Фильтры, примененные к данным.
            data (Any): Данные для анализа. Будут сериализованы в JSON, если это не строка.
            charts (List[str]): Список валидных типов графиков для передачи в JSON-схему LLM.
            
        Returns:
            Dict[str, Any]: Финальный словарь с аналитическим отчетом.
                Формат: {"status": str, "raw_text": str, "chart_type": str, "chart_title": str}
                
        Notes:
            - Используется `asyncio.gather` для параллельной обработки чанков.
            - При ошибках в отдельных чанках, метод не прерывается, а логирует ошибку 
              и передает сырые данные этого чанка на финальный этап.
        """
        data_in = {
            "metric": metric,
            "filters": filters,
            "data": data,
            "charts": charts
        }
        logger.info(f"Запуск формирования аналитического отчета для статистики: '{data_in}'")

        content_chunks = split_large_text(json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data, max_chunk_size=11000)
        logger.info(f"Количество чанков: {len(content_chunks)}")

        if (not data) or (not content_chunks):
            return {
                "status": "error",
                "raw_text": "Нет данных для обработки",
                "chart_type": "",
                "chart_title": ""
            }

        if len(content_chunks) == 1:
            content = {
                "metric": metric,
                "filters": filters,
                "data": data
            }
            final_result = await self.generate_report(metric, content, charts)
            logger.info(f"Аналитический отчет для '{metric}' сформирован успешно")

            return final_result

        content_parts = [{
            "metric": metric,
            "filters": filters,
            "data": {k: v} if isinstance(v, dict) else data
        } for k, v in data.items()]


        # Параллельный анализ всех чанков
        async with self.semaphore:
            results = await asyncio.gather(*[self.generate_report(metric, p, charts) for p in content_parts], return_exceptions=True)

        # Обрабатываем результаты
        successful_results = {
                "metric": metric,
                "filters": filters,
                "data": []
            }
        for result, (k, v) in zip(results, data.items()):
            if isinstance(result, Exception):
                logger.error(f"Ошибка при анализе блока '{metric}'.'{k}': {result}")
                successful_results["data"].append({k: v})
                continue
            if result != None:
                successful_results["data"].append({k: result.get("raw_text", "")})

        if not successful_results["data"]:
            return {
                "status": "error",
                "raw_text": "Ошибка обработки аналитических данных",
                "chart_type": "",
                "chart_title": ""
            }

        # Объединяем результаты
        try:
            logger.info(f"Результирование аналитического отчета по чанкам для '{metric}'")
            final_result = await self.generate_report(metric, successful_results, charts)
            logger.info(f"Результирование аналитического отчета по чанкам для '{metric}' прошло успешно")

            return final_result
        
        except Exception as e:
            logger.error(f"Ошибка результирования аналитического отчета по чанкам: {e}")
            return {
                "status": "error",
                "raw_text": "Ошибка обработки аналитических данных",
                "chart_type": "",
                "chart_title": ""
            }
    

    @async_retry(LLM_RETRY_CONFIG)
    async def generate_report(self, metric: str, content_part: Dict[str, Any], charts: List) -> Dict[str, Any]:
        """
        Формирует промпт и отправляет запрос к LLM для генерации структурированного аналитического текста.
        
        Метод гарантирует возврат валидного JSON, используя встроенные механизмы LLM (guided_json) 
        и постобработку (json_repair) в качестве fallback.
        
        Args:
            metric (str): Название метрики (используется для логирования и контекста).
            content_part (Dict[str, Any]): Часть данных (или агрегированные результаты) для анализа.
            charts (List[str]): Список доступных типов графиков (используется в enum JSON-схемы).
            
        Returns:
            Dict[str, Any]: Распарсенный ответ LLM.
                Формат: {"status": str, "raw_text": str, "chart_type": str, "chart_title": str}
            None: Если произошла неисправимая ошибка (таймаут, невозможность распарсить JSON).
            
        Raises:
            asyncio.TimeoutError: Перехватывается внутри метода, логируется и возвращает None.
            
        Notes:
            - Перед запросом обязательно вызывается `self.rate_limiter.acquire()`.
            - Используется декоратор `@async_retry` для автоматических повторных попыток при сбоях сети.
            - Если LLM возвращает невалидный JSON, применяется библиотека `json_repair`.
        """
        system_prompt = """
            Выяви ключевые проблемы и тенденции, опираясь на представленные данные.
            Сформируй аналитический текст для раздела, описывая **только** статистически значимые закономерности.
            Формулируй человекопонятным языком.
            Используй цифры из статистики.
            Если agg_group указаны, ссылайся в контексте на 'Рисунок' графика (без нумерации и описания причины выбора типа графика). 
            Например: 'Анализ динамики общего числа экспертиз за 2025 год показывает значительный рост активности в последние месяцы года (Рисунок).'
            или: 'На Рисунке видно, что наиболее распространёнными формами закупок являются аукцион и котировки'.
            В "chart_type" укажи наиболее подходящий тип графика (один на весь раздел), учитывай число группировок agg_group.
            Если подходит тип графика 'bar', а количество групп большое или названия групп длинные, то выбирай 'horizontal_bar'.
            Стиль: официальный, как в методических материалах Минобрнауки.
            Отвечай строго по структуре JSON с полями `status`, `raw_text`, 'chart_type', "chart_title", не добавляй ничего лишнего.
        """

        schema = {
            "type": "object",
            "required": [
                    "status",
                    "raw_text",
                    "chart_type",
                    "chart_title"
            ],
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["success", "error"],
                    "description": "Статус выполнения функции"
                },
                "raw_text": {
                    "type": "string",
                    "description": "Описание статистики в свободной форме"
                },
                "chart_type": {
                    "type": "string",
                    "enum": charts,
                    "description": "Тип графика, наиболее подходящего для иллюстрации текста"
                },
                "chart_title": {
                    "type": "string",
                    "description": "Название графика, иллюстрирующего текст"
                }
            }
        }

        content_chunks = split_large_text(json.dumps(content_part, ensure_ascii=False) if not isinstance(content_part, str) else content_part, max_chunk_size=12000)
        logger.info(f"Количество чанков: {len(content_chunks)}")

        # Ждём «разрешения» от глобального лимитера ПЕРЕД запросом
        await self.rate_limiter.acquire()
        
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system",
                        "content": system_prompt},
                    {"role": "user", "content": f"Статистика:\n\n{content_chunks[0]}"}
                ],
                extra_body={"guided_json": schema},
                max_tokens=3000,
                temperature=0.1
            )
        
        except asyncio.TimeoutError:
            logger.error(f"Таймаут при анализе показателя '{metric}'")
            return None

        raw_response = response.choices[0].message.content.strip()
        logger.info(f"Получен ответ длиной {len(raw_response)} символов для показателя '{metric}'")
        logger.info(raw_response)

        # Парсим JSON
        try:
            result = json.loads(raw_response)
            logger.info("Удалось распарсить JSON.")
            if isinstance(result, list):
                result = self.merge_json_objects(result)
            return result
        
        except json.JSONDecodeError as e:
            logger.warning(f"Первая попытка парсинга JSON не удалась: {e}")

        # Поиск JSON структур вручную
        try:
            result = json.loads(repair_json(raw_response))

            if not result:
                logger.error("JSON структуры не найдены.")
                return None
            
            logger.info("Удалось распарсить JSON из извлечённого фрагмента вручную.")
            if isinstance(result, list):
                result = self.merge_json_objects(result)
            return result
        
        except json.JSONDecodeError:
            logger.error("Не удалось распарсить ни одну JSON структуру.")
            return None