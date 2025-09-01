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
from typing import Dict, List
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx import Presentation
from pdf2image import convert_from_path
from dotenv import load_dotenv
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from configs.config import Config
from configs.procurement_requirements import PROCUREMENT_REQUIREMENTS
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


def get_required_documents(legislation: str, procurement_method: str, expertise_details: str) -> List[str]:
    """
    Возвращает список обязательных документов для закупки на основе законодательства,
    способа проведения закупки и дополнительных требований экспертизы.

    Функция выполняет поиск по вложенному словарю `PROCUREMENT_REQUIREMENTS`,
    где структура организована по уровням: законодательство → способ закупки → детали экспертизы.
    Если какой-либо уровень отсутствует, возвращается пустой список.

    Args:
        legislation (str): Нормативный акт, по которому проводится закупка (например, "44-ФЗ", "223-ФЗ").
        procurement_method (str): Способ определения поставщика (например, "Электронный аукцион", "Запрос котировок").
        expertise_details (str): Дополнительные критерии, влияющие на состав документов (например, "С ОС", "Без ОС", "Для НИОКР").

    Returns:
        List[str]: Список названий обязательных документов (например, ["Техническое задание", "Проект договора"]).
                   Возвращает пустой список, если подходящие требования не найдены.
    """
    return (PROCUREMENT_REQUIREMENTS.get(legislation, {})
                                  .get(procurement_method, {})
                                  .get(expertise_details, []))


def extract_filename(path):
    """
    Извлекает имя файла без пути и расширения из заданного пути.

    Функция пытается извлечь название CSV-файла, используя регулярное выражение,
    чтобы найти имя файла перед расширением `.csv` и после последнего разделителя путей (`/` или `\`).
    Если совпадение не найдено, возвращается имя файла (без расширения) с помощью стандартных средств `os.path`.

    Поддерживает как Unix- (`/`), так и Windows-стиль (`\\`) разделителей путей.

    Args:
        path (str): Полный или относительный путь к файлу (например, 'data/procurements.csv' или 'C:\\files\\report.csv').

    Returns:
        str: Имя файла без пути и расширения. Например, из 'data/procurements.csv' вернёт 'procurements'.
             Если имя не удаётся определить — возвращает базовое имя файла без расширения.
    """
    match = re.search(r'[/\\]([^/\\]+)\.csv$', path)
    return match.group(1) if match else os.path.splitext(os.path.basename(path))[0]


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


def check_procurement_completeness(law_type: str, procurement_type: str, check_type: str, files: Dict[str, str]) -> Dict:
    """
    Проверяет полноту комплекта документов для закупки по заданным требованиям.

    Функция сравнивает список загруженных документов с ожидаемыми по законодательству,
    способу закупки и типу проверки. Оценивает наличие всех обязательных файлов и их содержимое.

    Args:
        law_type (str): Тип законодательства (например, "44-ФЗ", "223-ФЗ").
        procurement_type (str): Способ проведения закупки (например, "Конкурс", "Аукцион").
        check_type (str): Тип экспертизы или проверки (например, "Полный комплект документов").
        files (Dict[str, str]): Словарь, где ключ — имя файла, значение — его текстовое содержимое.

    Returns:
        Dict: Результат проверки с полями:
            - is_complete (bool): True, если все обязательные документы присутствуют и не пустые.
            - missing_files (List[str]): Список отсутствующих обязательных документов.
            - invalid_files (Dict[str, str]): Словарь с именами файлов и причинами некорректности (например, "Файл пустой").
            - feedback (str): Человекочитаемый отчёт о результатах проверки.
    """
    requirements = (PROCUREMENT_REQUIREMENTS.get(law_type, {})
                                     .get(procurement_type, {})
                                     .get(check_type, []))
    if not requirements:
        return {
            "is_complete": False,
            "missing_files": [],
            "invalid_files": {},
            "feedback": "Неизвестный тип проверки или закупки"
        }

    uploaded_files = {f.lower().strip(): f for f in files.keys()}
    required_files_lower = [r.lower().strip() for r in requirements]

    missing_files = []
    found_files = []
    for req_file, req_lower in zip(requirements, required_files_lower):
        found = False
        for uploaded_lower, original_name in uploaded_files.items():
            if req_lower in uploaded_lower or uploaded_lower in req_lower:
                found = True
                found_files.append(original_name)
                break
        if not found:
            missing_files.append(req_file)

    invalid_files = {}
    for file_name, content in files.items():
        if file_name in found_files and not content.strip():
            invalid_files[file_name] = "Файл пустой"

    feedback_parts = []
    if not missing_files:
        feedback_parts.append("Все обязательные документы представлены.")
    else:
        feedback_parts.append(f"Отсутствуют документы: {', '.join(missing_files)}")

    if invalid_files:
        feedback_parts.append(f"Проблемы в документах: {', '.join(invalid_files.keys())}")

    if not missing_files and not invalid_files:
        feedback_parts.append("Документы соответствуют требованиям комплектности.")

    return {
        "is_complete": len(missing_files) == 0 and len(invalid_files) == 0,
        "missing_files": missing_files,
        "invalid_files": invalid_files,
        "feedback": "\n".join(feedback_parts)
    }