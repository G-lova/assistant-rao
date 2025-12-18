from fastapi import Depends, HTTPException, status, Header
from typing import Generator, Optional
import logging


from configs.config import Config
from models.data_fetcher_para import DataFetcher
from src.expertise_service import ExpertiseService
from src.external_api_service import ExternalAPIService

logger = logging.getLogger(__name__)

def get_data_fetcher(
    x_api_database: str = Header(default="dev", alias="X-API-Database")
) -> Generator[DataFetcher, None, None]:
    """
    Зависимость для получения экземпляра DataFetcher
    """
    try:
        logger.info(f" Получение конфигурации для среды: {x_api_database}")
        
        # Получаем конфигурацию для указанной среды
        db_config = Config.get_database_config(environment=x_api_database)
        
        # Логируем детали конфигурации (маскируем ключ)
        masked_key = db_config.api_key[:4] + "*" * (len(db_config.api_key) - 4) if db_config.api_key else "NULL"
        logger.info(f" Конфигурация для {x_api_database}:")
        logger.info(f"   URL: {db_config.url}")
        
        # Проверяем наличие обязательных параметров
        if not db_config.url or not db_config.api_key:
            error_msg = (
                f" Неполная конфигурация для среды {x_api_database}. "
                f"URL: {db_config.url}, API Key: {db_config.api_key}"
            )
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        # Создаем DataFetcher
        fetcher = DataFetcher(
            url=db_config.url,
            headers=db_config.headers
        )
        
        logger.info(f" DataFetcher успешно создан для среды {x_api_database}")
        yield fetcher
        
    except Exception as e:
        logger.error(f" КРИТИЧЕСКАЯ ОШИБКА при создании DataFetcher: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ошибка инициализации DataFetcher для среды {x_api_database}: {str(e)}"
        )

def get_expertise_service(
    fetcher: DataFetcher = Depends(get_data_fetcher)
) -> ExpertiseService:
    """
    Зависимость для получения сервиса работы с экспертизами
    
    Автоматически внедряет DataFetcher, настроенный для среды из заголовка X-API-Database
    """
    try:
        logger.info(" Создание ExpertiseService")
        service = ExpertiseService(data_fetcher=fetcher)
        logger.info(" ExpertiseService успешно создан")
        return service
    except Exception as e:
        logger.error(f" Ошибка создания ExpertiseService: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ошибка создания сервиса экспертиз: {str(e)}"
        )

def get_external_api_service(
    x_api_database: str = Header(default="dev", alias="X-API-Database")
) -> ExternalAPIService:
    """
    Зависимость для получения сервиса внешнего API
    
    Использует тот же заголовок X-API-Database, что и для базы данных
    """
    try:
        logger.info(f"🔧 Создание ExternalAPIService для среды: {x_api_database}")
        return ExternalAPIService(environment=x_api_database)
    except Exception as e:
        logger.error(f"❌ Ошибка создания ExternalAPIService: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ошибка создания сервиса внешнего API: {str(e)}"
        )
