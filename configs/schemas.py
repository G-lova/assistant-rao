from pydantic import BaseModel
from typing import Dict, Optional, List, Union
from fastapi import UploadFile

class DocumentContentResponse(BaseModel):
    procurement_id: str  # ID закупки
    document_type: str   # Тип документа
    filename: str        # Имя файла
    content_type: str    # MIME-тип
    content: str         # Первые 20 символов
    size: int            # Размер файла в байтах
    is_valid: bool       # Успешно ли обработан
    error: Optional[str] = None  # Сообщение об ошибке


class APIError(BaseModel):
    detail: str


class MissingDocument(BaseModel):
    document_name: str
    explanation: str


class ProcurementCheckRequest(BaseModel):
    procurement_id: str
    expertise_object: str
    legislation: str
    procurement_method: str
    expertise_details: str
    eis_link: Optional[str] = None
    documents: List[Dict[str, Union[str, None]]]  # Список документов


class ProcurementCheckResponse(BaseModel):
    status: str  # "allow" or "deny"
    required_documents: List[str]  # Список требуемых документов
    provided_documents: List[str]  # Список предоставленных документов
    missing_documents: List[MissingDocument]  # Список отсутствующих документов
    documents_content: Dict[str, str]  # Первые 100 символов содержимого каждого документа
    error: Optional[str]


class DocumentInfo(BaseModel):
    document_name: str
    status: str
    explanation: Optional[str] = None
    file_path: Optional[str] = None


class DocumentForm(BaseModel):
    document_name: str
    status: str
    explanation: Optional[str] = None
    file: Optional[UploadFile] = None