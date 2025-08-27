from pydantic import BaseModel
from typing import Dict, Optional, List, Union
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


class APIError(BaseModel):
    """
    Модель стандартного ответа об ошибке API.

    Используется для возврата понятного сообщения об ошибке клиенту
    в случае возникновения исключения или валидации.

    Attributes:
        detail (str): Подробное описание ошибки (например, "Файл слишком большой").
    """
    detail: str


class MissingDocument(BaseModel):
    """
    Модель описания отсутствующего документа.

    Содержит информацию о документе, который требуется для закупки, но не был предоставлен.

    Attributes:
        document_name (str): Название требуемого документа (например, "Проект договора").
        explanation (str): Обоснование необходимости документа (например, "Требуется по 44-ФЗ, часть 2, статья 56").
    """
    document_name: str
    explanation: str


class ProcurementCheckRequest(BaseModel):
    """
    Модель запроса на проверку закупки.

    Содержит все необходимые данные для анализа соответствия пакета документов
    требованиям законодательства и типу закупки.

    Attributes:
        procurement_id (str): Уникальный идентификатор закупки.
        expertise_object (str): Объект закупки (например, "Поставка лекарственных препаратов").
        legislation (str): Нормативная база (например, "44-ФЗ", "223-ФЗ").
        procurement_method (str): Способ закупки (например, "Запрос котировок", "Аукцион").
        expertise_details (str): Дополнительные сведения для экспертизы (например, специфические требования).
        eis_link (Optional[str]): Ссылка на закупку в ЕИС (например, на сайте zakupki.gov.ru). Может отсутствовать.
        documents (List[Dict[str, Union[str, None]]]): Список документов, каждый из которых представлен словарём
            с ключами, такими как 'name', 'url' или 'content'. Может содержать ссылки на файлы или их данные.
    """
    procurement_id: str
    expertise_object: str
    legislation: str
    procurement_method: str
    expertise_details: str
    eis_link: Optional[str] = None
    documents: List[Dict[str, Union[str, None]]]  # Список документов


class ProcurementCheckResponse(BaseModel):
    """
    Модель ответа на запрос проверки закупки.

    Содержит результат экспертизы: статус, список документов, отсутствующие файлы,
    фрагменты содержимого и возможные ошибки.

    Attributes:
        status (str): Итоговый статус проверки — 'allow' (разрешено) или 'deny' (требуется доработка).
        required_documents (List[str]): Список имён документов, обязательных для данной закупки.
        provided_documents (List[str]): Список имён документов, фактически предоставленных пользователем.
        missing_documents (List[MissingDocument]): Список объектов с информацией о каждом отсутствующем документе.
        documents_content (Dict[str, str]): Контент первых 100 символов каждого документа, ключ — имя документа.
        error (Optional[str]): Сообщение об ошибке, если проверка не была выполнена (например, ошибка парсинга).
    """
    status: str  # "allow" or "deny"
    required_documents: List[str]  # Список требуемых документов
    provided_documents: List[str]  # Список предоставленных документов
    missing_documents: List[MissingDocument]  # Список отсутствующих документов
    documents_content: Dict[str, str]  # Первые 100 символов содержимого каждого документа
    error: Optional[str]


class DocumentInfo(BaseModel):
    """
    Модель информации о документе в контексте проверки.

    Используется для передачи статуса и пояснений по конкретному документу.

    Attributes:
        document_name (str): Название документа.
        status (str): Статус документа (например, "valid", "invalid", "missing").
        explanation (Optional[str]): Дополнительное пояснение (например, "Не соответствует типу ТЗ").
        file_path (Optional[str]): Путь к файлу на сервере (если сохранён). Может быть None.
    """
    document_name: str
    status: str
    explanation: Optional[str] = None
    file_path: Optional[str] = None


class DocumentForm(BaseModel):
    """
    Модель формы загрузки документа.

    Используется в интерфейсах, где пользователь может загружать или обновлять документы.

    Attributes:
        document_name (str): Название документа, указываемое пользователем.
        status (str): Текущий статус документа (например, "draft", "uploaded").
        explanation (Optional[str]): Комментарий или пояснение от пользователя или системы.
        file (Optional[UploadFile]): Сам файл, загружаемый пользователем. Может отсутствовать, если документ уже загружен.
    """
    document_name: str
    status: str
    explanation: Optional[str] = None
    file: Optional[UploadFile] = None