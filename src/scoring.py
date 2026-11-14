import asyncio
import sys
import os
from datetime import date

from configs.config import Config
from configs.http_client_manager import HTTPClientManager
from search_experts.pipeline import ScoringPipeline
from configs.logger import get_logger


# Добавляем путь к проекту для корректного импорта
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.insert(0, project_root)

logger = get_logger(__name__)

def top_preview(results, limit: int = 10) -> str:
    """Первые эксперты ответа для журнала: «425 (80.08), 523 (78.38), …» или «425, 523, …» без деталей.

    Args:
        results: Ответ ``ScoringPipeline.get_top_results`` — список ID или список ``{id: {..., 'total_rate'}}``.
        limit: Сколько показать.

    Returns:
        str: Строка; пустой ответ — «нет экспертов».
    """
    if not results:
        return "нет экспертов"
    items = []
    for r in list(results)[:limit]:
        if isinstance(r, dict):
            eid, d = next(iter(r.items()))
            items.append(f"{eid} ({d.get('total_rate')})" if isinstance(d, dict) else str(eid))
        else:
            items.append(str(r))
    more = f" … всего {len(results)}" if len(results) > limit else ""
    return ", ".join(items) + more


async def scoring(expertise_id: int, details: bool, x_api_database: str, http_manager: HTTPClientManager) -> list:
    """
    Запускает пайплайн скоринга экспертов для заданной экспертизы.

    Создает экземпляр ScoringPipeline, выполняет SQL-запрос из файла
    experts_for_expertise.sql для получения данных об экспертах,
    рассчитывает рейтинг на основе ML-модели и возвращает отсортированный
    список наиболее подходящих экспертов.

    Args:
        expertise_id: Уникальный идентификатор экспертизы
        details: Флаг, указывающий на необходимость получения подробной информации о каждом эксперте (по умолчанию False)
        x_api_database: Идентификатор среды базы данных ('dev', 'prod', 'stage')

    Returns:
        list: Отсортированный список ID экспертов по убыванию релевантности

    Raises:
        Exception: При ошибках в пайплайне скоринга экспертов
    """
    # Настройка логирования

    try:

        # Создание пайплайна
        pipeline = ScoringPipeline(http_manager, x_api_database)

        # Параметры запуска
        sql_file_name = "experts_for_expertise.sql"


        # Запуск пайплайна
        df = await pipeline.run_pipeline(sql_file_name, expertise_id)

        # Получение результатов
        top_results = await asyncio.to_thread(pipeline.get_top_results, df, details)

        # итог одной строкой: время шагов и отсев кандидатов; первые 10 в ответе; полный ответ — на DEBUG
        logger.info(f"{pipeline.log_ctx} готово: {pipeline.summary()}")
        logger.info(f"{pipeline.log_ctx} первые в ответе: {top_preview(top_results)}")
        logger.debug(f"{pipeline.log_ctx} ответ полностью: {top_results}")

        return top_results

    except Exception as e:
        logger.error(f"[подбор {expertise_id}/{x_api_database}] ошибка: {e}", exc_info=True)
        raise


if __name__ == "__main__":

    expertise_id = int(input('ID экспертизы для подбора эксперта:'))
    asyncio.run(scoring(expertise_id, False, "dev", http_manager))
