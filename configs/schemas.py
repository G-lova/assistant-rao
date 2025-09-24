from pydantic import BaseModel
from typing import Dict, Optional, List, Union, Any
from fastapi import UploadFile


class DocumentContentResponse(BaseModel):
    """
    Модель ответа с информацией о содержимом документа.

    Представляет краткие метаданные и фрагмент содержимого загруженного документа,
    используемого в рамках проверки закупки. Позволяет быстро оценить тип, размер
    и корректность документа без передачи полного содержимого.

    Attributes:
        procurement_id (str): Уникальный идентификатор закупки.
        document_type (str): Тип документа (например, 'ТЗ', 'Смета', 'Протокол').
        filename (str): Имя исходного файла, предоставленного пользователем.
        content_type (str): MIME-тип файла (например, 'application/pdf', 'text/plain').
        content (str): Первые 20 символов содержимого файла или строка "Неверный документ", если проверка не пройдена.
        size (int): Размер файла в байтах.
        is_valid (bool): Флаг, указывающий, соответствует ли содержимое документа ожидаемому типу.
        error (Optional[str]): Описание ошибки, если возникла проблема при обработке (например, повреждённый файл).
    """
    procurement_id: str  # ID закупки
    document_type: str   # Тип документа
    filename: str        # Имя файла
    content_type: str    # MIME-тип
    content: str         # Первые 20 символов или "Неверный документ"
    size: int            # Размер файла в байтах
    is_valid: bool       # Соответствует ли документ своему типу
    error: Optional[str] = None  # Сообщение об ошибке


class BatchDocumentResponse(BaseModel):
    """
    Модель ответа для массовой обработки документов.

    Используется как схема ответа при загрузке нескольких файлов одновременно.
    Содержит общее состояние операции, список результатов по каждому документу
    и возможные ошибки, возникшие в процессе обработки.

    Attributes:
        status (str): Общий статус выполнения операции (например, 'success', 'partial_success', 'error').
        results (List[DocumentContentResponse]): Список детальных результатов анализа каждого документа.
        errors (List[str]): Список сообщений об ошибках, произошедших при обработке отдельных файлов. 
            По умолчанию — пустой список.
    """
    status: str
    results: List[DocumentContentResponse]
    errors: List[str] = []