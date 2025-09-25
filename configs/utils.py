import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile

import pandas as pd
import numpy as np
import PyPDF2
import rarfile
import textract
import zipfile
from typing import Dict, List, Any, Set
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx import Presentation
from pdf2image import convert_from_path
from dotenv import load_dotenv
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from configs.config import Config
from configs.procurement_requirements import DOCUMENT_TYPE_MAPPING
from src.ocr import ocr_image_with_qwen_vl


config = Config.get_model_config()
load_dotenv()


logging.getLogger("PyPDF2").setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class APIKeyMiddleware(BaseHTTPMiddleware):
    """
    Промежуточное ПО (middleware) для проверки API-ключа в заголовках запроса.

    Этот класс реализует защиту всех эндпоинтов приложения с помощью проверки
    наличия и валидности API-ключа в заголовке `X-API-Key`. Исключения сделаны
    для стандартных эндпоинтов документации (Swagger и ReDoc), чтобы упростить
    тестирование и использование API.

    Использует `secrets.compare_digest` для защищённого от атак по времени
    сравнения ключей. В случае отсутствия или несоответствия ключа возвращается
    ошибка 403. При ошибках конфигурации (например, не задан API_KEY в .env) — 500.

    Attributes:
        None (middleware не требует дополнительных атрибутов)
    """

    async def dispatch(self, request: Request, call_next):
        """
        Обрабатывает входящий запрос, проверяя наличие и корректность API-ключа.

        Args:
            request (Request): Объект входящего HTTP-запроса.
            call_next (Callable): Следующая функция в цепочке обработки запроса.

        Returns:
            Response: Ответ, либо результат следующего обработчика, либо JSON с ошибкой.
        """
        if request.url.path in ["/docs", "/openapi.json", "/redoc"]:
            return await call_next(request)
        
        try:
            api_key = request.headers.get("X-API-Key")
            if not api_key:
                return JSONResponse(status_code=403, content={"detail": "API key missing"})
            expected_api_key = os.getenv("API_KEY")
            if not expected_api_key:
                return JSONResponse(status_code=500, content={"detail": "API_KEY not configured"})
            if not secrets.compare_digest(api_key, expected_api_key):
                return JSONResponse(status_code=403, content={"detail": "Invalid API key"})
            return await call_next(request)
        
        except Exception as e:
            return JSONResponse(status_code=500, content={"detail": f"Internal server error: {str(e)}"})


def read_txt_file(file_path: str) -> str:
    """
    Читает и возвращает содержимое текстового файла в кодировке UTF-8.

    Args:
        file_path (str): Путь к .txt файлу, который необходимо прочитать.

    Returns:
        str: Содержимое файла в виде строки. Возвращает пустую строку, если файл пустой.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()


def read_doc_file(file_path: str) -> str:
    """
    Читает файл формата .doc или .docx и извлекает из него текстовое содержимое.

    Для .docx дополнительно извлекается текст с изображений с помощью OCR.
    Проверяет существование файла, его размер и поддержку формата.

    Args:
        file_path (str): Путь к файлу .doc или .docx.

    Raises:
        ValueError: Если файл не существует.
        ValueError: Если файл пустой.
        ValueError: Если формат файла не поддерживается (не .doc и не .docx).
        ValueError: Если возникает ошибка при чтении файла .doc.
        ValueError: Если происходит ошибка на этапе обработки.

    Returns:
        str: Извлечённый текст из документа. В случае ошибки — исключение.
    """
    try:
        if not os.path.exists(file_path):
            raise ValueError("Файл не существует")
        if os.path.getsize(file_path) == 0:
            raise ValueError("Файл пустой")
        if file_path.lower().endswith('.docx'):
            return extract_text_and_images_from_docx(file_path, ocr_func=ocr_image_with_qwen_vl)
        elif file_path.lower().endswith('.doc'):
            try:
                return textract.process(file_path).decode('utf-8')
            except Exception as e:
                raise ValueError(f"Ошибка чтения DOC: {str(e)}")
        else:
            raise ValueError("Неподдерживаемый формат файла")
    except Exception as e:
        logger.error(f"Ошибка обработки документа: {str(e)}")
        raise ValueError(f"Ошибка обработки файла: {str(e)}")


def read_pptx_file(file_path: str) -> str:
    """
    Извлекает текстовое содержимое из презентации PowerPoint (.pptx).

    Проходит по всем слайдам и извлекает текст из всех доступных фигур (shape),
    включая заголовки, пункты списка и другие текстовые блоки.

    Args:
        file_path (str): Путь к файлу .pptx.

    Returns:
        str: Объединённый текст всех слайдов, разделённый переносами строк.
             Возвращает пустую строку, если текст не найден.
    """
    presentation = Presentation(file_path)
    text_content = []
    for slide in presentation.slides:
        for shape in slide.shapes:
            if hasattr(shape, "text"):
                text_content.append(shape.text)
    return "\n".join(text_content)


def read_pdf_file(file_path: str) -> str:
    """
    Извлекает текст из PDF-файла ТОЛЬКО с помощью OCR для каждой страницы.

    Все страницы PDF конвертируются в изображения и обрабатываются через OCR (Qwen-VL),
    независимо от наличия встроенного текста. Это гарантирует единообразную обработку:
    сканы, защищённые PDF, многослойные документы — всё проходит через распознавание образа.

    Args:
        file_path (str): Путь к PDF-файлу.

    Raises:
        ValueError: Если произошла ошибка при чтении или обработке файла.

    Returns:
        str: Объединённый текст всех страниц с пометкой "OCR".
             Если текст не распознан, возвращает "[Нет читаемого текста]".
    """
    ocr_texts = []

    try:
        # Конвертируем PDF в список изображений
        with tempfile.TemporaryDirectory() as temp_dir:
            images = convert_from_path(file_path, output_folder=temp_dir)
            for i, image in enumerate(images):
                img_path = os.path.join(temp_dir, f"page_{i}.jpg")
                image.save(img_path, "JPEG")

                # OCR через Qwen-VL
                ocr_text = ocr_image_with_qwen_vl(img_path)

                # Добавляем с пометкой страницы
                ocr_texts.append(f"Страница {i+1} (OCR): {ocr_text}")

        result = "\n".join(ocr_texts)
        return result.strip() if result.strip() else "[Нет читаемого текста]"

    except Exception as e:
        raise ValueError(f"Ошибка при обработке PDF через OCR: {str(e)}")


def read_excel_file(file_path: str) -> str:
    """
    Читает файл Excel (.xls или .xlsx) и извлекает текстовое содержимое всех листов.

    Для устаревшего формата .xls сначала выполняется конвертация в .xlsx с помощью LibreOffice.
    Затем данные считываются с помощью pandas и преобразуются в строковое представление.

    Args:
        file_path (str): Путь к файлу Excel.

    Raises:
        ValueError: Если файл не существует.
        ValueError: Если формат файла не поддерживается (не .xls и не .xlsx).
        RuntimeError: Если ошибка возникла при конвертации .xls в .xlsx.
        RuntimeError: Если сконвертированный файл не найден.
        RuntimeError: Если произошла ошибка при чтении или обработке файла.

    Returns:
        str: Текстовое содержимое всех листов в формате:
             === Лист: <название> ===
             [таблица в виде строки]
             Объединено через переносы строк. Возвращает пустую строку при ошибке.
    """
    if not os.path.exists(file_path):
        raise ValueError(f"Файл не найден: {file_path}")
    base, ext = os.path.splitext(file_path)
    ext = ext.lower()
    if ext not in ('.xls', '.xlsx'):
        raise ValueError("Поддерживаются только файлы .xls и .xlsx")

    temp_dir = None
    temp_xlsx_path = None
    try:
        if ext == '.xls':
            temp_dir = tempfile.mkdtemp()
            temp_xlsx_path = os.path.join(temp_dir, os.path.basename(base) + ".xlsx")
            cmd = [
                'libreoffice',
                '--headless',
                '--convert-to',
                'xlsx',
                '--outdir',
                temp_dir,
                file_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"Ошибка конвертации: {result.stderr}")
            converted_files = [f for f in os.listdir(temp_dir) if f.endswith('.xlsx')]
            if not converted_files:
                raise RuntimeError("Не удалось найти сконвертированный файл")
            temp_xlsx_path = os.path.join(temp_dir, converted_files[0])
            file_to_read = temp_xlsx_path
        else:
            file_to_read = file_path

        excel_data = pd.read_excel(file_to_read, sheet_name=None, engine='openpyxl')
        result = []
        for sheet_name, df in excel_data.items():
            result.append(f"=== Лист: {sheet_name} ===")
            result.append(df.to_string())
        return "\n".join(result)

    except Exception as e:
        raise RuntimeError(f"Ошибка при обработке Excel файла: {str(e)}")
    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)


def extract_text_and_images_from_docx(file_path: str, ocr_func=None) -> str:
    """
    Извлекает текст и распознаёт текст с изображений из файла DOCX.

    Функция извлекает текст из абзацев и таблиц документа, а также обрабатывает встроенные изображения,
    извлекая их из ZIP-структуры DOCX. Для каждого изображения можно выполнить OCR с помощью переданной
    функции `ocr_func`. Результаты объединяются в единый текстовый вывод.

    Args:
        file_path (str): Путь к файлу .docx.
        ocr_func (callable, optional): Функция для распознавания текста на изображении. 
                                      Должна принимать путь к изображению и возвращать строку с текстом.
                                      Если не указана, изображения помечаются без распознавания.
                                      Defaults to None.

    Raises:
        ValueError: Если произошла ошибка при обработке файла DOCX.

    Returns:
        str: Объединённый текст, содержащий:
             - обычный текст из документа;
             - результаты OCR с изображений (если `ocr_func` указана) или метки изображений.
             Все элементы разделены переносами строк. В случае ошибки — исключение.
    """
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            doc = Document(file_path)
            result = []
            for para in doc.paragraphs:
                if para.text.strip():
                    result.append(para.text)
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        if cell.text.strip():
                            result.append(cell.text)

            image_texts = []
            doc_zip = zipfile.ZipFile(file_path)
            image_files = [name for name in doc_zip.namelist() if name.startswith('word/media/')]
            for img_file in image_files:
                try:
                    img_data = doc_zip.read(img_file)
                    img_path = os.path.join(temp_dir, os.path.basename(img_file))
                    with open(img_path, 'wb') as f:
                        f.write(img_data)
                    if ocr_func:
                        ocr_text = ocr_func(img_path)
                        image_texts.append(f"[OCR из изображения {os.path.basename(img_file)}]: {ocr_text}")
                    else:
                        image_texts.append(f"[Изображение: {os.path.basename(img_file)}]")
                except Exception as e:
                    logger.error(f"Ошибка обработки изображения {img_file}: {str(e)}")
                    continue

            if image_texts:
                result.append("\n".join(image_texts))
            return "\n".join(result)

    except Exception as e:
        logger.error(f"Ошибка обработки DOCX: {str(e)}")
        raise ValueError(f"Ошибка обработки DOCX: {str(e)}")


def process_archive(file_path: str) -> str:
    """
    Извлекает и обрабатывает файлы из архива (.zip или .rar), читая их содержимое.

    Функция последовательно:
    - распаковывает каждый файл из архива во временную директорию;
    - определяет его тип и извлекает текст с помощью `read_file`;
    - добавляет содержимое с заголовком имени файла;
    - удаляет временный файл после обработки.

    Поддерживает вложенные папки в архиве. Каталоги пропускаются.

    Args:
        file_path (str): Путь к архивному файлу (.zip или .rar).

    Returns:
        str: Объединённый текст всех обработанных файлов в формате:
             === имя_файла ===
             [содержимое]
             В случае ошибки возвращает сообщение об ошибке с именем архива.
    """
    content = []

    try:
        if file_path.lower().endswith('.zip'):
            with zipfile.ZipFile(file_path, 'r') as archive:
                for zip_info in archive.infolist():
                    if zip_info.is_dir():
                        continue
                    with archive.open(zip_info) as f:
                        extracted_path = os.path.join(tempfile.gettempdir(), zip_info.filename)
                        os.makedirs(os.path.dirname(extracted_path), exist_ok=True)
                        with open(extracted_path, 'wb') as out_f:
                            out_f.write(f.read())
                        try:
                            text = read_file(extracted_path, original_filename=zip_info.filename)
                            content.append(f"=== {zip_info.filename} ===\n{text}")
                        except Exception as e:
                            content.append(f"=== {zip_info.filename} ===\n[Ошибка чтения: {e}]")
                        finally:
                            if os.path.exists(extracted_path):
                                os.unlink(extracted_path)

        elif file_path.lower().endswith('.rar'):
            with rarfile.RarFile(file_path) as archive:
                for rar_info in archive.infolist():
                    if rar_info.isdir():
                        continue
                    with archive.open(rar_info) as f:
                        extracted_path = os.path.join(tempfile.gettempdir(), rar_info.filename)
                        os.makedirs(os.path.dirname(extracted_path), exist_ok=True)
                        with open(extracted_path, 'wb') as out_f:
                            out_f.write(f.read())
                        try:
                            text = read_file(extracted_path, original_filename=rar_info.filename)
                            content.append(f"=== {rar_info.filename} ===\n{text}")
                        except Exception as e:
                            content.append(f"=== {rar_info.filename} ===\n[Ошибка чтения: {e}]")
                        finally:
                            if os.path.exists(extracted_path):
                                os.unlink(extracted_path)

        return "\n\n".join(content)

    except Exception as e:
        logger.error(f"Ошибка при обработке архива {file_path}: {str(e)}")
        return f"[Архив {os.path.basename(file_path)}: ошибка извлечения — {str(e)}]"


def read_file(file_path: str, original_filename: str = None) -> str:
    """
    Читает файл и извлекает текстовое содержимое в зависимости от его формата.

    Функция определяет тип файла по расширению и вызывает соответствующий обработчик:
    TXT, PDF, Excel, PowerPoint, DOC/DOCX, архивы и другие форматы. Поддерживает
    передачу оригинального имени файла для корректной обработки (например, при работе с временными путями).

    Args:
        file_path (str): Путь к файлу на диске.
        original_filename (str, optional): Оригинальное имя файла (может отличаться от имени во временном хранилище).
                                           Используется для определения расширения. Defaults to None.

    Raises:
        ValueError: Если произошла ошибка при чтении файла (например, повреждён, недоступен).

    Returns:
        str: Извлечённый текст или сообщение о типе файла, если формат не поддерживается.
             Для неподдерживаемых бинарных форматов возвращается строка вида "Бинарный файл ...".
    """
    filename_to_check = original_filename or os.path.basename(file_path)
    _, ext = os.path.splitext(filename_to_check)
    ext = ext.lower()
    logger.info(f"Чтение файла: {filename_to_check} (расширение: {ext})")

    try:
        if ext == ".txt":
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read()
                logger.info(f"Прочитан TXT-файл: {filename_to_check}, длина: {len(text)}")
                return text
        elif ext == ".pdf":
            text = read_pdf_file(file_path)
            logger.info(f"Извлечён текст из PDF: {filename_to_check}, длина: {len(text)}")
            return text
        elif ext in [".xlsx", ".xls"]:
            text = read_excel_file(file_path)
            logger.info(f"Извлечён текст из Excel: {filename_to_check}, длина: {len(text)}")
            return text
        elif ext == ".pptx":
            text = read_pptx_file(file_path)
            logger.info(f"Извлечён текст из PPTX: {filename_to_check}, длина: {len(text)}")
            return text
        elif ext in [".csv", ".json"]:
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read()
                logger.info(f"Прочитан {ext.upper()}-файл: {filename_to_check}, длина: {len(text)}")
                return text
        elif ext in [".doc", ".docx"]:
            text = read_doc_file(file_path)
            logger.info(f"Извлечён текст из DOC(X): {filename_to_check}, длина: {len(text)}")
            return text
        elif ext in [".zip", ".rar"]:
            text = process_archive(file_path)
            logger.info(f"Извлечён текст из архива: {filename_to_check}, длина: {len(text)}")
            return text
        else:
            msg = f"Бинарный файл {os.path.basename(file_path)} (формат {ext})"
            logger.warning(f"Неподдерживаемый формат файла: {filename_to_check}")
            return msg
    except Exception as e:
        logger.error(f"Ошибка чтения файла {filename_to_check}: {str(e)}", exc_info=True)
        raise ValueError(f"Ошибка чтения файла: {str(e)}")


def check_procurement_completeness(
    files: Dict[str, str],
    document_analysis_results: Dict[str, dict]
) -> Dict:
    """
    Проверяет полноту и качество комплекта загруженных документов.

    Анализирует:
    1. Наличие обязательных приложенных документов (из attached_documents_list)
    2. Читаемость каждого документа
    3. Корректность типов документов
    4. Наличие ключевых реквизитов (даты, суммы, стороны)
    5. Формирует общий статус и детализированный фидбек

    Args:
        files (Dict[str, str]): Словарь: имя_файла -> путь.
        document_analysis_results (Dict[str, dict]): Результаты анализа документов.

    Returns:
        Dict: Словарь с результатами проверки:
            - status: 'allow' или 'deny'
            - declared_attachments: список требуемых документов
            - missing_in_upload: какие из них отсутствуют
            - provided_documents: нормализованные типы загруженных
            - source_docs_for_attached: источники списка приложений
            - feedback: текстовое пояснение
            - detailed_issues: список выявленных проблем
            - final_feedback: итоговое заключение
    """
    declared_attached: Set[str] = set()
    source_docs_for_attached: List[str] = []
    provided_documents: List[str] = []
    missing_in_upload: List[str] = []
    issues: List[str] = []
    feedback_parts: List[str] = []

    # 1. Извлечение списка обязательных документов
    for file_name, result in document_analysis_results.items():
        attached_list = result.get("attached_documents_list", [])
        if attached_list:
            declared_attached.update(attached_list)
            source_docs_for_attached.append(file_name)

    if not declared_attached:
        feedback_parts.append("В документах не найден явный список «Документы к закупке». Полагаем, что комплектность не регламентирована.")
    else:
        feedback_parts.append(f"Обязательные документы определены на основе: {', '.join(source_docs_for_attached)}")
        feedback_parts.append(f"Требуется предоставить: {', '.join(sorted(declared_attached))}")

    # 2. Нормализация загруженных файлов
    normalized_provided = {
        normalize_document_type(name): name
        for name in files.keys()
    }
    provided_documents = sorted(normalized_provided.keys())

    # Проверка отсутствующих документов
    if declared_attached:
        missing_in_upload = [doc for doc in declared_attached if doc not in normalized_provided]
        if missing_in_upload:
            issues.append(f"Отсутствуют обязательные документы: {', '.join(missing_in_upload)}")

    # 3. Проверка читаемости
    for file_name, result in document_analysis_results.items():
        readability = result.get("readability", {})
        status_read = readability.get("status")

        if status_read == "неудовлетворительно":
            issues.append(f"{file_name}: документ нечитаем")
            feedback_parts.append(f"{file_name} — статус читаемости: 'неудовлетворительно'")
        elif status_read == "частично читаем":
            details = ", ".join(readability.get("issues", []))
            issues.append(f"{file_name}: частичная читаемость")
            feedback_parts.append(f"{file_name} — частично читаем: {details}")

    # 4. Проверка соответствия типа
    for file_name, result in document_analysis_results.items():
        type_comp = result.get("type_compliance", {})
        if type_comp.get("status") == "не соответствует":
            expected = type_comp.get("expected_type", "неизвестно")
            actual = type_comp.get("actual_type", "не определён")
            issues.append(f"{file_name}: тип '{actual}' не соответствует ожидаемому '{expected}'")
            feedback_parts.append(f"{file_name} — ожидался тип '{expected}', обнаружен '{actual}'")

    # 5. Проверка наличия ключевых данных
    for file_name, result in document_analysis_results.items():
        raw_data = result.get("raw_data", {})

        if not raw_data.get("dates"):
            issues.append(f"{file_name}: отсутствуют даты")
            feedback_parts.append(f"{file_name} — не обнаружены даты (например, дата контракта или извещения)")

        if not raw_data.get("amounts"):
            issues.append(f"{file_name}: отсутствуют суммы")
            feedback_parts.append(f"{file_name} — не указаны финансовые суммы (НМЦК, цена и т.п.)")

        if not raw_data.get("legal_entities"):
            issues.append(f"{file_name}: не указаны стороны")
            feedback_parts.append(f"{file_name} — отсутствуют данные о заказчике или поставщике")

    # 6. Определение статуса
    critical_issues = [
        issue for issue in issues
        if any(keyword in issue.lower() for keyword in ("нечитаем", "отсутствуют обязательные", "не соответствует"))
    ]
    status = "deny" if critical_issues else "allow"

    # 7. Финальное пояснение
    if status == "allow":
        if not declared_attached:
            final_feedback = "Комплектность документов не регламентирована — проверка пройдена."
        else:
            final_feedback = "Все обязательные документы предоставлены, типы корректны, данные полны."
    else:
        final_feedback = "Выявлены критические проблемы:\n" + \
                         "\n".join(f"- {issue}" for issue in critical_issues)

    return {
        "status": status,
        "declared_attachments": sorted(list(declared_attached)),
        "missing_in_upload": sorted(missing_in_upload),
        "provided_documents": provided_documents,
        "source_docs_for_attached": sorted(source_docs_for_attached),
        "feedback": "\n".join(feedback_parts),
        "detailed_issues": issues,
        "final_feedback": final_feedback
    }


def normalize_document_type(doc_type: str) -> str:
    """
    Нормализует тип документа, приводя его к единому стандартному наименованию.

    Функция принимает строку с названием типа документа (возможно, в неформатном виде),
    очищает и преобразует её, затем сопоставляет с эталонными типами из маппинга.
    Проверка выполняется в порядке: точное совпадение → частичные вхождения по приоритетным ключам.
    Если соответствие не найдено, возвращается тип по умолчанию.

    Args:
        doc_type (str): Исходное название типа документа (например, из метаданных или OCR).

    Returns:
        str: Нормализованное название типа документа, соответствующее одному из стандартных значений,
             или "Дополнительные материалы", если тип не распознан.
    """
    if not doc_type or not isinstance(doc_type, str):
        return "Дополнительные материалы"

    clean = doc_type.strip().lower()

    # 1. Точное совпадение
    for key in DOCUMENT_TYPE_MAPPING:
        if clean == key.lower():
            return key

    # 2. Частичные совпадения с приоритетом
    priority_matches = [
        ("требования к содержанию заявки на конкурс", "Требования к содержанию заявки на конкурс"),
        ("техническое задание", "Техническое задание"),
        ("извещение", "Извещение"),
        ("проект контракта", "Проект контракта"),
        ("обоснование нмцк", "Обоснование н(м)цк"),
        ("акт о приемке", "Акт о приемке товара"),
        ("счет-фактура", "Документ о приемке товара (УПД, Счет-фактура и др.)"),
        ("дополнительное соглашение", "Дополнительные соглашения к контракту"),
        ("расчёт нмцк", "Обоснование н(м)цк"),
        ("обоснование начальной", "Обоснование н(м)цк"),
        ("нмцк", "Обоснование н(м)цк"),
        ("тз", "Техническое задание"),
    ]

    for substr, full_type in priority_matches:
        if substr in clean:
            return full_type

    return "Дополнительные материалы"