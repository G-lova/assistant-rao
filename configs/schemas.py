from pydantic import BaseModel
from typing import Dict, Optional, List

class DocumentContentResponse(BaseModel):
    """
    Ответ с содержимым документа и метаинформацией
    """
    filename: str
    content_type: str
    content: str
    size: int  # в байтах
    is_valid: bool
    error: Optional[str] = None

class APIError(BaseModel):
    """
    Модель для ошибок API
    """
    detail: str