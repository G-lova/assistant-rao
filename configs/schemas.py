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


class DateExtraction(BaseModel):
    """
    Извлечение даты из документа.

    Представляет собой структуру для хранения информации о найденной дате в документе,
    включая имя поля, значение даты и номер страницы, на которой она указана.

    Args:
        field (str): Название поля, содержащего дату (например, 'дата подписания').
        value (str): Значение извлечённой даты в виде строки.
        page (int): Номер страницы документа, на которой найдена дата.
    """
    field: str
    value: str
    page: int


class AmountExtraction(BaseModel):
    """
    Извлечение суммы из документа.

    Структура для хранения информации о денежной сумме, найденной в документе,
    включая название поля, значение суммы, валюту и номер страницы.

    Args:
        field (str): Название поля, содержащего сумму (например, 'сумма контракта').
        value (str): Значение суммы в виде строки (может включать форматирование).
        currency (str): Валюта суммы (например, 'RUB', 'USD').
        page (int): Номер страницы документа, на которой найдена сумма.
    """
    field: str
    value: str
    currency: str
    page: int


class LegalEntity(BaseModel):
    """
    Юридическое лицо, участвующее в документе.

    Описывает информацию о компании или организации, указанной в документе,
    включая её роль, наименование, реквизиты и адрес.

    Args:
        role (str): Роль юридического лица в документе (например, 'заказчик', 'исполнитель').
        name (str): Полное наименование юридического лица.
        inn (Optional[str]): ИНН организации. По умолчанию — None.
        kpp (Optional[str]): КПП организации. По умолчанию — None.
        ogrn (Optional[str]): ОГРН организации. По умолчанию — None.
        address (Optional[str]): Юридический или фактический адрес организации. По умолчанию — None.
        page (int): Номер страницы, на которой указано данное юридическое лицо.
    """
    role: str
    name: str
    inn: Optional[str] = None
    kpp: Optional[str] = None
    ogrn: Optional[str] = None
    address: Optional[str] = None
    page: int


class RawData(BaseModel):
    """
    Сырые извлечённые данные из документа.

    Содержит списки извлечённой информации: дат, сумм, юридических лиц,
    а также дополнительные поля, такие как номер договора и ссылки на законодательство.

    Args:
        dates (List[DateExtraction]): Список объектов с извлечёнными датами. По умолчанию — пустой список.
        amounts (List[AmountExtraction]): Список объектов с извлечёнными суммами. По умолчанию — пустой список.
        legal_entities (List[LegalEntity]): Список объектов с информацией о юридических лицах. По умолчанию — пустой список.
        contract_number (Optional[str]): Номер договора, если указан. По умолчанию — None.
        law_references (List[str]): Список ссылок на нормативные правовые акты, упомянутые в документе. По умолчанию — пустой список.
    """
    dates: List[DateExtraction] = []
    amounts: List[AmountExtraction] = []
    legal_entities: List[LegalEntity] = []
    contract_number: Optional[str] = None
    law_references: List[str] = []


class DocumentConclusion(BaseModel):
    """
    Заключение по анализу документа.

    Содержит результаты анализа документа, включая проверку соответствия типу,
    оценку читаемости, извлечённые сырые данные и общий вывод.

    Args:
        type_compliance (Dict[str, Any]): Оценка соответствия документа ожидаемому типу (например, по шаблону). Ключи могут включать 'score', 'details'.
        readability (Dict[str, Any]): Оценка читаемости документа (например, качество сканирования, наличие повреждений). Может содержать метрики и комментарии.
        raw_data (RawData): Объект с извлечёнными данными из документа.
        conclusion (str): Текстовый вывод по результатам анализа документа.
    """
    type_compliance: Dict[str, Any]
    readability: Dict[str, Any]
    raw_data: RawData
    conclusion: str


class DocumentAnalysisResult(BaseModel):
    """
    Модель результата анализа одного документа в рамках закупки.

    Содержит метаданные о документе, результаты автоматической проверки,
    извлечённые данные и финальное заключение. Используется для передачи детальной
    информации о каждом обработанном файле в составе группового ответа.
    """
    procurement_id: str
    filename: str
    document_type: str
    size: int
    content_preview: str  # первые 300 символов
    is_valid: bool
    analysis: Dict[str, Any]  # как в raw_data
    conclusion: str
    error: Optional[str] = None


class EvaluateDocumentsResponse(BaseModel):
    """
    Модель ответа на запрос массовой оценки документов.

    Представляет собой комплексный отчёт по анализу нескольких документов одной закупки,
    включая результаты проверки каждого файла, результаты междокументной согласованности
    и сводную информацию. Используется как response_model в эндпоинтах пакетной обработки.
    """
    documents: List[DocumentAnalysisResult]
    consistency_check: Dict[str, Any]  # результат check_consistency
    summary: Dict[str, Any]  # краткая сводка