import sys
import os
from datetime import date

from configs.config import Config
from search_experts.pipeline import ScoringPipeline
from configs.logger import setup_logging, get_logger


# Добавляем путь к проекту для корректного импорта
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.insert(0, project_root)


def scoring(expertise_id, rows):
    """
    Запускает полный пайплайн оценки и ранжирования экспертов для указанной экспертизы.

    Инициализирует систему логирования, создаёт экземпляр ScoringPipeline, выполняет загрузку данных,
    предобработку, вычисление схожести, фильтрацию конфликтов интересов и расчёт итогового рейтинга.
    Выводит топ-N результатов в консоль и сохраняет их в CSV-файл.

    Args:
        expertise_id (Any): Уникальный идентификатор экспертизы, на основе которой происходит подбор экспертов.
        rows (int): Количество лучших экспертов, которые необходимо вернуть и сохранить.

    Returns:
        pd.DataFrame: Таблица с топ-N экспертами, содержащая их рейтинги и ключевые метрики:
            - similarity_embeddings: Семантическое сходство профиля с задачей экспертизы.
            - conflict_fuzzy: Уровень риска конфликта интересов (0–100).
            - rating: Итоговый нормализованный рейтинг (0–1), рассчитанный по весовой формуле.
            - distance_rate, workload_rate, avg_rating: Дополнительные факторы ранжирования.

    Raises:
        Exception: Если возникла ошибка на любом этапе обработки — она записывается в лог и пробрасывается дальше.
    """
    # Настройка логирования
    setup_logging()
    logger = get_logger(__name__)
    
    try:
        logger.info("Starting scoring pipeline...")
        
        # Создание пайплайна
        pipeline = ScoringPipeline()
        
        # Параметры запуска
        sql_file_name = "experts_for_expertise.sql"
        
        logger.info(f"Processing expertise_id: {expertise_id}")
        
        # Запуск пайплайна
        df = pipeline.run_pipeline(sql_file_name, expertise_id)
        
        # Получение результатов
        top_results = pipeline.get_top_results(df, rows)
        
        logger.info("Scoring pipeline completed successfully")
        
        # Вывод результатов
        print("=" * 80)
        print(f"Топ-{rows} экспертов по рейтингу:")
        print("=" * 80)
        print(top_results.to_string(index=False))
        
        # Сохранение результатов
        output_dir = os.path.join(project_root, "data", "outputs")
        os.makedirs(output_dir, exist_ok=True)
        
        output_file = os.path.join(output_dir, f"expertise_{expertise_id}_results_{date.today()}.csv")
        top_results.to_csv(output_file, index=False, encoding='utf-8')
        
        logger.info(f"Results saved to: {output_file}")
        
        return top_results
        
    except Exception as e:
        logger.error(f"Error in scoring pipeline: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    expertise_id = int(input('ID экспертизы для подбора эксперта:'))
    scoring(expertise_id, rows=10)