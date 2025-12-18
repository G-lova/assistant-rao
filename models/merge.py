from fastapi import APIRouter, Depends, HTTPException, status, Header
from pydantic import BaseModel
from typing import Dict, Any, Optional
import logging

from src.external_api_service import ExternalAPIService
from src.expertise_service import ExpertiseService
from models.dependencies import get_expertise_service, get_external_api_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/merge", tags=["merge"])

class MergeRequest(BaseModel):
    expertise_id: str
    send_to_external: bool = False


class MergeResponse(BaseModel):
    merged_data: dict  
    expertise_id: str
    environment: str
    external_api_response: Optional[dict] = None
    status: str = "success"

@router.post("/jsons", response_model=MergeResponse)
async def merge_jsons_endpoint(
    request: MergeRequest,
    service: ExpertiseService = Depends(get_expertise_service),
    external_service: ExternalAPIService = Depends(get_external_api_service),  # Используем зависимость
    x_api_database: str = Header(default="dev", alias="X-API-Database")
):
    """
    Сливает два JSON-документа по указанному expertise_id
    
    Args:
        request: {
            "expertise_id": "",
            "send_to_external": true  
        }
        x_api_database: Заголовок для выбора среды (dev/stage/prod)
    """
    logger.info(f" Запрос на слияние JSON для expertise_id={request.expertise_id} в среде {x_api_database}")
    
    try:
        # Выполняем слияние
        merged_data = service.merge_expertise_jsons(request.expertise_id)
        logger.info(f"✅ Успешное слияние для expertise_id={request.expertise_id}")
        
        external_response = None
        
       
        if request.send_to_external:
            try:
               
                expertise_id_int = int(request.expertise_id)
                
                
                external_response = external_service.send_expertise_data(
                    expertise_id=expertise_id_int,
                    merged_data=merged_data
                )
                logger.info(f"✅ Успешная отправка данных во внешнее API для expertise_id={request.expertise_id}")
                
            except Exception as e:
                logger.error(f"❌ Ошибка при отправке во внешнее API: {str(e)}")
                external_response = {
                    "error": str(e),
                    "status": "failed"
                }
        
        return MergeResponse(
            merged_data=merged_data,
            expertise_id=request.expertise_id,
            environment=x_api_database.lower(),
            external_api_response=external_response
        )
    
    except Exception as e:
        error_msg = f"Ошибка слияния для expertise_id={request.expertise_id} в среде {x_api_database}: {str(e)}"
        logger.error(f"❌ {error_msg}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_msg
        )
