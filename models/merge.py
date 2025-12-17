from fastapi import APIRouter, Depends, HTTPException, status, Header
from pydantic import BaseModel
from typing import Dict, Any, Optional
import logging

from src.external_api_service import ExternalAPIService
from src.expertise_service import ExpertiseService
from models.dependencies import get_expertise_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/merge", tags=["merge"])

class MergeRequest(BaseModel):
    expertise_id: str
    send_to_external: bool = False

# ИСПРАВЛЕНО: правильные имена полей
class MergeResponse(BaseModel):
    merged_data: dict  # ← Было "merged_" (опечатка), Стало "merged_data"
    expertise_id: str
    environment: str
    external_api_response: Optional[dict] = None
    status: str = "success"

@router.post("/jsons", response_model=MergeResponse)
async def merge_jsons_endpoint(
    request: MergeRequest,
    service: ExpertiseService = Depends(get_expertise_service),
    x_api_database: str = Header(default="dev", alias="X-API-Database")
):
    try:
        # Выполняем слияние
        merged_data = service.merge_expertise_jsons(request.expertise_id)
        # Убедимся, что данные не None
        if merged_data is None:
            raise ValueError("Слияние вернуло пустой результат")
        
        external_response = None
        
        if request.send_to_external:
            try:
                external_service = ExternalAPIService()
                expertise_id_int = int(request.expertise_id)
                external_response = external_service.send_expertise_data(
                    expertise_id=expertise_id_int,
                    merged_data=merged_data
                )
            except Exception as e:
                logger.error(f" Ошибка при отправке во внешнее API: {str(e)}")
                external_response = {
                    "error": str(e),
                    "status": "failed"
                }
        
        # ИСПРАВЛЕНО: правильное имя параметра merged_data
        return MergeResponse(
            merged_data=merged_data,  
            expertise_id=request.expertise_id,
            environment=x_api_database.lower(),
            external_api_response=external_response
        )
    
    except Exception as e:
        error_msg = f"Ошибка слияния для expertise_id={request.expertise_id}: {str(e)}"
        logger.error(f" {error_msg}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_msg
        )
