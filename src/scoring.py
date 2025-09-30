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


def scoring(expertise_id):
    """
    Запускает пайплайн оценки экспертов для заданной экспертизы.

    Функция инициализирует пайплайн ScoringPipeline, выполняет SQL-запрос для получения данных
    об экспертах, рассчитывает рейтинг на основе критериев и возвращает отсортированный список
    экспертов по убыванию релевантности. Результаты сохраняются в CSV-файл.

    Args:
        expertise_id: Идентификатор экспертизы, для которой проводится оценка и подбор экспертов.

    Returns:
        pd.Series или pd.DataFrame: Отсортированный набор результатов (топ экспертов),
                                   готовый к использованию или выводу.
                                   В случае ошибки — исключение не подавляется.
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
        top_results = pipeline.get_top_results(df)
        
        logger.info("Scoring pipeline completed successfully")
        
        # Вывод результатов
        print(f'Эксперты в порядке убывания рейтинга:\n{top_results.tolist()}')
        
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
    scoring(expertise_id)