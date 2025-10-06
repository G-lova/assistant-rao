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
from src.ocr import ocr_image_with_qwen_vl
from configs.working_with_db import (save_raw_data,
                                     save_clean_conclusion,
                                     save_summary_report)
from src.evaluator import (check_documents_consistency,
                           analyze_document_chunks,
                           split_large_text,
                           check_completeness_with_ai)
from configs.parsing import parse_cloud_storage_link


config = Config.get_model_config()
load_dotenv()


logging.getLogger("PyPDF2").setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

document_analysis_cache = {}

API_KEY_PATTERN = re.compile(r'^[A-Za-z0-9._\-]+$')

class APIKeyMiddleware(BaseHTTPMiddleware):
    """
    Промежуточное ПО для аутентификации входящих запросов по API-ключу.

    Проверяет наличие, формат и корректность API-ключа в заголовке X-API-Key.
    Использует безопасное сравнение строк и скрывает внутренние ошибки конфигурации
    от клиента. Любой сбой в процессе проверки приводит к ответу с кодом 401,
    за исключением случая отсутствия ключа в переменных окружения — тогда возвращается 500.

    Args:
        BaseHTTPMiddleware: Базовый класс промежуточного ПО FastAPI.
    """
    async def dispatch(self, request: Request, call_next):
        """
        Обрабатывает входящий запрос, проверяя валидность API-ключа.

        Args:
            request (Request): Входящий HTTP-запрос.
            call_next (_type_): Следующий обработчик в цепочке middleware.

        Returns:
            _type_: Ответ сервера: либо результат следующего обработчика при успешной
                    аутентификации, либо JSONResponse с ошибкой (401 или 500).
        """

        try:
            api_key = request.headers.get("X-API-Key")
            
            # 1. Проверка наличия
            if not api_key:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid API key"}
                )
            
            # 2. Проверка формата (только ASCII)
            if not API_KEY_PATTERN.match(api_key):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid API key"}
                )
            
            # 3. Проверка конфигурации
            expected_api_key = os.getenv("API_KEY")
            if not expected_api_key:
                # Логируем внутреннюю ошибку, но не раскрываем клиенту
                print("CRITICAL: API_KEY not set in environment")
                return JSONResponse(
                    status_code=500,
                    content={"detail": "Service misconfigured"}
                )
            
            # 4. Безопасное сравнение
            if not secrets.compare_digest(api_key, expected_api_key):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid API key"}
                )
            
            return await call_next(request)
        
        except Exception:
            # Любая ошибка → 401 или 500 без деталей
            # Поскольку ошибка в аутентификации — лучше 401
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid API key"}
            )


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
    Читает содержимое Excel-файла (.xls или .xlsx) и преобразует его в текстовый формат.

    Функция поддерживает оба формата Excel, используя `openpyxl` для .xlsx и попытки чтения .xls
    сначала через `openpyxl`, затем — через `xlrd`. При неудаче запускается резервный метод
    с использованием LibreOffice. Каждый лист представляется как таблица в текстовом виде
    с заголовками столбцов и данными. Результат объединяется в единую строку.

    Args:
        file_path (str): Путь к Excel-файлу на диске.

    Raises:
        ValueError: Если файл не существует или имеет неподдерживаемое расширение.
        RuntimeError: Если все попытки чтения файла завершились ошибкой.
        ValueError: Если указан неверный путь к файлу.
        RuntimeError: Если возникла ошибка при обработке данных Excel.

    Returns:
        str: Текстовое представление всех листов Excel-файла, включая:
            - название каждого листа,
            - табличные данные в читаемом формате (ограничено 20 строками на лист),
            - очищенные от NaN значения.
            Отсутствующие ячейки заменяются пустыми строками, технические названия колонок (например, 'Unnamed') переименовываются.
            В случае ошибки чтения — вызывается исключение.
    """
    if not os.path.exists(file_path):
        raise ValueError(f"Файл не найден: {file_path}")
    
    base, ext = os.path.splitext(file_path)
    ext = ext.lower()
    
    try:
        # Для .xls файлов используем openpyxl вместо xlrd
        if ext == '.xls':
            try:
                # Пробуем прочитать с openpyxl
                excel_data = pd.read_excel(file_path, sheet_name=None, engine='openpyxl')
            except Exception as openpyxl_error:
                logger.warning(f"openpyxl не смог прочитать .xls файл: {openpyxl_error}. Пробуем xlrd...")
                try:
                    # Пробуем старую версию xlrd
                    excel_data = pd.read_excel(file_path, sheet_name=None, engine='xlrd')
                except Exception as xlrd_error:
                    logger.warning(f"xlrd также не сработал: {xlrd_error}. Пробуем LibreOffice...")
                    return read_excel_with_libreoffice(file_path)
        elif ext == '.xlsx':
            excel_data = pd.read_excel(file_path, sheet_name=None, engine='openpyxl')
        else:
            raise ValueError("Поддерживаются только файлы .xls и .xlsx")

        result = []
        
        for sheet_name, df in excel_data.items():
            result.append(f"Лист: {sheet_name}")
            # Заменяем NaN на пустые строки и преобразуем все в строки
            df = df.fillna('').astype(str)
            
            # Форматируем таблицу для лучшей читаемости
            for col in df.columns:
                # Очищаем названия колонок от технической информации
                if 'unnamed' in str(col).lower():
                    df = df.rename(columns={col: f'Колонка_{df.columns.get_loc(col)}'})
            
            # Используем компактное представление таблицы
            table_text = df.to_string(index=False, max_rows=20)  # Ограничиваем количество строк
            result.append(table_text)
            result.append("")  # Одна пустая строка между листами
        
        # Убираем лишние пустые строки в конце и множественные пробелы
        full_text = "\n".join(result).strip()
        # Заменяем множественные пробелы на один пробел
        full_text = re.sub(r'\s+', ' ', full_text)
        logger.info(f"Успешно извлечен текст из Excel: {len(full_text)} символов")
        return full_text

    except Exception as e:
        logger.error(f"Критическая ошибка при обработке Excel файла: {str(e)}")
        raise RuntimeError(f"Не удалось прочитать Excel файл: {str(e)}")


def read_excel_with_libreoffice(file_path: str) -> str:
    """
    Читает Excel-файл (.xls и др.) с помощью LibreOffice, конвертируя его в .xlsx и извлекая текстовое содержимое.

    Является резервным методом для обработки старых или повреждённых Excel-файлов, которые не удаётся прочитать
    стандартными библиотеками (например, `xlrd` или `openpyxl`). Запускает LibreOffice в headless-режиме,
    конвертирует файл во временный формат .xlsx, затем читает его с помощью pandas и преобразует в строку.

    Args:
        file_path (str): Путь к исходному Excel-файлу (обычно .xls), который необходимо обработать.

    Raises:
        RuntimeError: Если LibreOffice не установлен или недоступен в системе.
        RuntimeError: Если команда конвертации завершилась с ошибкой.
        RuntimeError: Если после конвертации не найден выходной .xlsx-файл.
        RuntimeError: Если произошла ошибка при чтении сконвертированного файла.
        RuntimeError: Если возникла любая другая ошибка на этапе обработки (общее исключение).

    Returns:
        str: Текстовое представление всех листов файла, включая:
            - название каждого листа,
            - табличные данные в виде строки (ограничено 20 строками на лист),
            - очищенные от NaN значений ячейки.
            Все множественные пробелы заменяются на одиночные. В случае успеха — возвращает полный текст;
            при ошибке — выбрасывает исключение с детализацией проблемы.
    """
    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp()
        temp_xlsx_path = os.path.join(temp_dir, "converted.xlsx")
        
        # Проверяем доступность LibreOffice
        try:
            result = subprocess.run(['libreoffice', '--version'], capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                raise RuntimeError("LibreOffice не установлен или недоступен")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            raise RuntimeError("LibreOffice не доступен")
        
        # Конвертируем в xlsx
        cmd = [
            'libreoffice',
            '--headless',
            '--convert-to',
            'xlsx:Calc MS Excel 2007 XML',
            '--outdir',
            temp_dir,
            file_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"Ошибка конвертации: {result.stderr}")
        
        # Ищем сконвертированный файл
        converted_files = [f for f in os.listdir(temp_dir) if f.endswith('.xlsx')]
        if not converted_files:
            raise RuntimeError("Не удалось найти сконвертированный файл")
        
        temp_xlsx_path = os.path.join(temp_dir, converted_files[0])
        excel_data = pd.read_excel(temp_xlsx_path, sheet_name=None, engine='openpyxl')
        
        result = []
        for sheet_name, df in excel_data.items():
            result.append(f"Лист: {sheet_name}")
            df = df.fillna('').astype(str)
            
            # Компактное представление
            table_text = df.to_string(index=False, max_rows=20)
            result.append(table_text)
            result.append("")  # Одна пустая строка между листами
        
        full_text = "\n".join(result).strip()
        # Заменяем множественные пробелы на один пробел
        full_text = re.sub(r'\s+', ' ', full_text)
        return full_text
        
    except Exception as e:
        logger.error(f"Ошибка конвертации через LibreOffice: {str(e)}")
        raise RuntimeError(f"Не удалось обработать Excel файл даже через LibreOffice: {str(e)}")
    finally:
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception as cleanup_error:
                logger.warning(f"Ошибка при очистке временных файлов: {cleanup_error}")


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
    Читает файл и извлекает текстовое содержимое с улучшенной обработкой бинарных файлов

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
        # Если расширение .bin, пробуем определить реальный тип
        if ext == ".bin":
            real_extension = detect_binary_file_type(file_path)
            logger.info(f"Определен реальный тип бинарного файла: {real_extension}")
            
            # Создаем копию файла с правильным расширением для обработки
            if real_extension != ".bin":
                new_path = file_path + real_extension
                shutil.copy2(file_path, new_path)
                try:
                    result = read_file(new_path, original_filename + real_extension)
                    os.unlink(new_path)
                    return result
                except Exception as e:
                    logger.warning(f"Не удалось обработать файл с определенным расширением {real_extension}: {str(e)}")
                    os.unlink(new_path)
                    # Продолжаем с оригинальным .bin файлом

        # Основная логика обработки по расширениям
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
        elif ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff"]:
            # Обработка изображений через OCR
            try:
                text = ocr_image_with_qwen_vl(file_path)
                logger.info(f"Извлечён текст из изображения через OCR: {filename_to_check}, длина: {len(text)}")
                return text
            except Exception as e:
                logger.warning(f"Не удалось извлечь текст из изображения {filename_to_check}: {str(e)}")
                return f"Изображение {os.path.basename(file_path)} (текст не распознан)"
        else:
            # Для неизвестных форматов пробуем определить тип и обработать
            if ext == ".bin":
                # Уже пробовали определить тип выше, если дошли сюда - не удалось
                msg = f"Бинарный файл {os.path.basename(file_path)} (не удалось определить формат)"
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
        name: name
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


def create_summary_report(
    procurement_id: str,
    documents_results: List[Dict],
    document_analysis_results: Dict[str, Dict],
    completeness_check: Dict,
    consistency_result: Dict
) -> Dict[str, Any]:
    """
    Формирует сводный отчёт по результатам анализа комплекта документов закупки.

    Объединяет данные из нескольких источников: результаты обработки файлов,
    детальный анализ каждого документа, проверку комплектности и согласованности.
    Собирает агрегированные сведения (даты, суммы, юрлица, ссылки на законодательство)
    и рассчитывает статистику по документам.

    Args:
        procurement_id (str): Уникальный идентификатор закупки.
        documents_results (List[Dict]): Список результатов обработки загруженных файлов
                                       (метаданные, статус валидации и т.д.).
        document_analysis_results (Dict[str, Dict]): Результаты детального анализа
                                                    по каждому документу (ключ — имя файла).
        completeness_check (Dict): Результаты проверки полноты комплекта документов.
        consistency_result (Dict): Результаты проверки внутренней согласованности данных.

    Returns:
        Dict[str, Any]: Структурированный сводный отчёт, содержащий:
            - procurement_id и timestamp;
            - documents_summary: краткая информация по каждому документу;
            - completeness_check и consistency_check: статусы и выявленные проблемы;
            - aggregated_data: объединённые данные из всех документов (даты, суммы и др.);
            - statistics: статистика по количеству и типам документов.
    """
    summary = {
        "procurement_id": procurement_id,
        "processing_timestamp": pd.Timestamp.now().isoformat(),
        "documents_summary": {},
        "completeness_check": {
            "status": completeness_check.get("status"),
            "declared_attachments": completeness_check.get("declared_attachments", []),
            "missing_documents": completeness_check.get("missing_in_upload", []),
            "provided_documents": completeness_check.get("provided_documents", [])
        },
        "consistency_check": {
            "status": consistency_result.get("status"),
            "issues": consistency_result.get("issues", [])
        }
    }
    
    # Собираем данные из всех документов (исключая conclusion, readability, type_compliance)
    for doc_name, analysis in document_analysis_results.items():
        doc_summary = {}
        
        # Извлекаем только raw_data и другую полезную информацию
        if "raw_data" in analysis:
            raw_data = analysis["raw_data"].copy()
            
            # Очищаем данные от ненужных полей если они есть
            raw_data.pop("conclusion", None)
            raw_data.pop("readability", None)
            raw_data.pop("type_compliance", None)
            
            doc_summary["raw_data"] = raw_data
        
        # Добавляем информацию о документе
        doc_info = next((doc for doc in documents_results if doc["filename"] == doc_name), {})
        doc_summary["document_type"] = doc_info.get("document_type", "Дополнительные материалы")
        doc_summary["is_valid"] = doc_info.get("is_valid", False)
        
        summary["documents_summary"][doc_name] = doc_summary
    
    # Агрегируем ключевые данные из всех документов
    all_dates = []
    all_amounts = []
    all_legal_entities = []
    all_law_references = []
    
    for doc_name, analysis in document_analysis_results.items():
        raw_data = analysis.get("raw_data", {})
        
        # Собираем даты
        if "dates" in raw_data:
            for date_info in raw_data["dates"]:
                date_info["source_document"] = doc_name
                all_dates.append(date_info)
        
        # Собираем суммы
        if "amounts" in raw_data:
            for amount_info in raw_data["amounts"]:
                amount_info["source_document"] = doc_name
                all_amounts.append(amount_info)
        
        # Собираем юридические лица
        if "legal_entities" in raw_data:
            for entity_info in raw_data["legal_entities"]:
                entity_info["source_document"] = doc_name
                all_legal_entities.append(entity_info)
        
        # Собираем ссылки на законодательство
        if "law_references" in raw_data:
            for law_ref in raw_data["law_references"]:
                all_law_references.append({
                    "reference": law_ref,
                    "source_document": doc_name
                })
    
    # Добавляем агрегированные данные в сводный отчет
    summary["aggregated_data"] = {
        "dates": all_dates,
        "amounts": all_amounts,
        "legal_entities": all_legal_entities,
        "law_references": all_law_references
    }
    
    # Добавляем статистику
    summary["statistics"] = {
        "total_documents": len(documents_results),
        "valid_documents": len([doc for doc in documents_results if doc.get("is_valid")]),
        "invalid_documents": len([doc for doc in documents_results if not doc.get("is_valid")]),
        "unique_document_types": list(set(doc.get("document_type", "Дополнительные материалы") for doc in documents_results)),
        "total_amounts_found": len(all_amounts),
        "total_dates_found": len(all_dates),
        "total_legal_entities_found": len(all_legal_entities)
    }
    
    return summary


def generate_overall_conclusion(
    documents_results: List[Dict], 
    completeness_check: Dict, 
    consistency_result: Dict
) -> str:
    """
    Генерирует итоговое заключение по результатам комплексной проверки комплекта документов.

    Формирует текстовый вывод на основе анализа трёх аспектов: валидности отдельных документов,
    полноты комплекта и согласованности данных между документами. Подсчитывает количество проблем
    и формирует итоговую оценку с рекомендациями.

    Args:
        documents_results (List[Dict]): Список результатов проверки каждого документа, 
            где каждый словарь содержит ключ `is_valid` (bool).
        completeness_check (Dict): Результат проверки полноты комплекта, должен содержать:
            - status (str): 'allow' — комплект полный, иначе — неполный.
            - missing_in_upload (List[str]): Список отсутствующих документов (если есть).
        consistency_result (Dict): Результат проверки согласованности данных между документами, должен содержать:
            - status (str): 'ok' — всё согласовано, иначе — есть расхождения.
            - issues (List[dict]): Список выявленных несоответствий.

    Returns:
        str: Текстовое заключение, содержащее:
            - Количество обработанных и проблемных документов.
            - Статус полноты комплекта.
            - Статус согласованности данных.
            - Итоговую оценку: "соответствует", "требует исправлений" или "требует доработки".
            Все пункты объединены в читаемый отчёт, разделённый переносами строк.
    """
    conclusions = []
    
    # Анализируем результаты по документам
    valid_docs = [doc for doc in documents_results if doc.get("is_valid")]
    invalid_docs = [doc for doc in documents_results if not doc.get("is_valid")]
    
    if valid_docs:
        conclusions.append(f"Обработано документов: {len(valid_docs)}")
    
    if invalid_docs:
        conclusions.append(f"Проблемных документов: {len(invalid_docs)}")
    
    # Добавляем информацию о комплектности
    if completeness_check.get("status") == "allow":
        conclusions.append("Комплект документов полный")
    else:
        missing_count = len(completeness_check.get("missing_in_upload", []))
        conclusions.append(f"Отсутствует документов: {missing_count}")
    
    # Добавляем информацию о согласованности
    if consistency_result.get("status") == "ok":
        conclusions.append("Данные согласованы")
    else:
        issues_count = len(consistency_result.get("issues", []))
        conclusions.append(f"Обнаружено расхождений: {issues_count}")
    
    # Формируем итоговую оценку
    total_issues = len(invalid_docs) + len(completeness_check.get("missing_in_upload", [])) + len(consistency_result.get("issues", []))
    
    # ОПРЕДЕЛЯЕМ ОБЩИЙ СТАТУС
    has_errors = (
        len(invalid_docs) > 0 or 
        completeness_check.get("status") != "allow" or 
        consistency_result.get("status") != "ok"
    )
    
    if not has_errors:
        final_assessment = "Комплект документов соответствует требованиям."
        overall_status = "allow"
    elif total_issues <= 2:
        final_assessment = "Комплект документов в основном соответствует требованиям, но требуются исправления."
        overall_status = "deny"
    else:
        final_assessment = "Комплект документов требует значительной доработки."
        overall_status = "deny"
    
    conclusions.append(f"\nИТОГ: {final_assessment}")
    
    # Сохраняем общий статус для использования в ответе
    return "\n".join(conclusions), overall_status


def extract_required_docs_from_analysis(document_analysis_results: Dict[str, Dict]) -> List[str]:
    """
    Извлекает список требуемых документов из результатов анализа исходного документа.
    ИСКЛЮЧАЕТ 'проект контракта' из проверки комплектности.

    Args:
        document_analysis_results (Dict[str, Dict]): Словарь с результатами анализа документов.

    Returns:
        List[str]: Уникальный список требуемых документов, исключая 'проект контракта'.
    """
    required_docs = set()
    
    for doc_name, analysis in document_analysis_results.items():
        if "raw_data" in analysis and "attached_documents_list" in analysis["raw_data"]:
            attached_docs = analysis["raw_data"]["attached_documents_list"]
            if attached_docs:
                # ФИЛЬТРУЕМ - исключаем 'проект контракта'
                filtered_docs = [
                    doc for doc in attached_docs 
                    if doc.lower().strip() != "проект контракта"
                ]
                required_docs.update(filtered_docs)
    
    return list(required_docs)


def extract_provided_docs_from_results(documents_results: List[Dict]) -> List[str]:
    """
    Извлекает список предоставленных документов из результатов обработки загрузки.

    Функция собирает типы документов, которые были успешно загружены и обработаны,
    на основе поля 'document_type' в каждом элементе списка.

    Args:
        documents_results (List[Dict]): Список словарей с результатами обработки каждого загруженного документа.

    Returns:
        List[str]: Список типов предоставленных документов (без дубликатов не требуется, сохраняется порядок).
    """
    return [doc["document_type"] for doc in documents_results if doc.get("document_type")]


def detect_binary_file_type(file_path: str) -> str:
    """
    Определяет тип бинарного файла по его содержимому (сигнатурам)
    
    Args:
        file_path (str): Путь к файлу
        
    Returns:
        str: Расширение файла (.pdf, .docx, .jpg и т.д.) или .bin если не удалось определить
    """
    try:
        with open(file_path, 'rb') as f:
            header = f.read(12)  # Читаем первые 12 байт для лучшего определения
        # PDF - %PDF
        if header.startswith(b'%PDF'):
            return '.pdf'
        # ZIP-based formats (DOCX, XLSX, PPTX, ODT и т.д.)
        if header.startswith(b'PK\x03\x04'):
            # Можно попробовать определить точный тип по структуре ZIP
            try:
                with zipfile.ZipFile(file_path, 'r') as zip_file:
                    namelist = zip_file.namelist()
                    # Проверяем структуру для разных форматов
                    if any(name.startswith('word/') for name in namelist):
                        return '.docx'
                    elif any(name.startswith('xl/') for name in namelist):
                        return '.xlsx'
                    elif any(name.startswith('ppt/') for name in namelist):
                        return '.pptx'
                    else:
                        return '.zip'  # обычный ZIP архив
            except:
                return '.docx'  # по умолчанию считаем DOCX
        # Microsoft Office old formats (DOC, XLS, PPT)
        if header.startswith(b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1'):
            return '.doc'  # по умолчанию DOC
        # JPEG
        if header.startswith(b'\xFF\xD8\xFF'):
            return '.jpg'
        # PNG
        if header.startswith(b'\x89PNG\r\n\x1a\n'):
            return '.png'
        # GIF
        if header.startswith(b'GIF8'):
            return '.gif'
        # BMP
        if header.startswith(b'BM'):
            return '.bmp'
        # TIFF
        if header.startswith(b'II\x2A\x00') or header.startswith(b'MM\x00\x2A'):
            return '.tiff'
        # RAR
        if header.startswith(b'Rar!\x1A\x07\x00') or header.startswith(b'Rar!\x1A\x07\x01'):
            return '.rar'
        # 7Z
        if header.startswith(b'7z\xBC\xAF\x27\x1C'):
            return '.7z'
        # Microsoft Cabinet (CAB)
        if header.startswith(b'MSCF'):
            return '.cab'
        # Windows Executable
        if header.startswith(b'MZ'):
            return '.exe'
        # UTF-8/16 text files with BOM
        if header.startswith(b'\xEF\xBB\xBF'):  # UTF-8 BOM
            return '.txt'
        if header.startswith(b'\xFF\xFE') or header.startswith(b'\xFE\xFF'):  # UTF-16 BOM
            return '.txt'

        # Пробуем определить как текстовый файл
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                f.read(1024)  # Пробуем прочитать как текст
            return '.txt'
        except:
            pass
            
        return '.bin'
        
    except Exception as e:
        logger.error(f"Ошибка определения типа бинарного файла {file_path}: {str(e)}")
        return '.bin'


async def evaluate_documents_batch_internal(task_data: dict):
    """
    Выполняет полный цикл внутренней обработки пакета документов закупки.

    Функция обрабатывает как загруженные файлы, так и документы по ссылкам из облачных хранилищ:
    извлекает текст, определяет тип документа с помощью ИИ, сохраняет результаты в БД,
    проверяет комплектность и согласованность данных, формирует сводный отчёт и кэширует
    итоговые результаты. Поддерживает обработку ошибок на каждом этапе и возвращает
    структурированный результат со статусами по каждому документу и общей оценкой.

    Args:
        task_data (dict): Словарь с данными задачи, содержащий:
            - procurement_id: уникальный идентификатор закупки,
            - files_data: список загруженных файлов с бинарным содержимым,
            - links: список ссылок на документы в облаке,
            - legislation: тип законодательства (например, "44-ФЗ"),
            - procurement_method: способ закупки,
            - expertise_details: дополнительные сведения (не используется напрямую).

    Raises:
        ValueError: Возникает при отсутствии текста в обрабатываемом файле.

    Returns:
        _type_: Словарь с полным результатом обработки, включающий:
            - статус выполнения,
            - список обработанных документов с типами и заключениями,
            - результаты проверок комплектности и согласованности,
            - общее заключение и метаданные обработки.
    """
    try:
        procurement_id = task_data['procurement_id']
        files_data = task_data.get('files_data', [])
        links = task_data.get('links', [])
        legislation = task_data.get('legislation', '44-ФЗ')
        procurement_method = task_data.get('procurement_method', 'Конкурс')
        expertise_details = task_data.get('expertise_details', 'Полный комплект документов о закупке')

        logger.info(f"Начата внутренняя обработка документов для закупки {procurement_id}")
        logger.info(f"Файлы: {len(files_data)}, ссылки: {len(links)}")

        documents_results = []
        document_analysis_results = {}
        files_dict = {}

        # ОБРАБОТКА ФАЙЛОВ ИЗ files_data
        if files_data:
            for file_info in files_data:
                try:
                    filename = file_info.get('filename', 'unknown_file')
                    file_content = file_info.get('content')  # Бинарное содержимое файла
                    file_extension = file_info.get('extension', '.bin')

                    if not file_content:
                        logger.warning(f"Пустое содержимое файла: {filename}")
                        continue

                    # Создаём временный файл
                    with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as tmp:
                        tmp.write(file_content)
                        tmp_path = tmp.name
                        files_dict[filename] = tmp_path

                    try:
                        logger.info(f"Обработка файла: {filename} для закупки {procurement_id}")

                        # Извлечение текста
                        extracted_text = read_file(tmp_path, original_filename=filename)

                        if not extracted_text or "[Нет читаемого текста]" in extracted_text:
                            raise ValueError("Не удалось извлечь текст из файла")

                        # Разделяем большой текст на блоки
                        chunks = split_large_text(extracted_text, max_chunk_size=20000)
                        logger.info(f"Документ разделён на {len(chunks)} блоков")

                        # Анализируем документ по частям
                        analysis_result = await analyze_document_chunks(
                            chunks=chunks,
                            document_name=filename,
                            document_type="",
                            law_type=legislation,
                            procurement_method=procurement_method
                        )

                        if analysis_result["status"] == "error":
                            final_doc_type = "Дополнительные материалы"
                            is_valid = False
                            conclusion = f"Ошибка анализа: {analysis_result['analysis']['conclusion']}"
                        else:
                            analysis = analysis_result["analysis"]
                            actual_type_from_model = analysis["type_compliance"].get("actual_type", "").strip()

                            # Нормализуем тип
                            final_doc_type = actual_type_from_model if actual_type_from_model else "Дополнительные материалы"

                            confidence = analysis["type_compliance"].get("confidence", 0)
                            is_valid = (
                                final_doc_type != "Дополнительные материалы"
                                and confidence >= 0.7
                            )
                            
                            conclusion = analysis["conclusion"]
                            
                            # Сохраняем результаты анализа для проверки комплектности
                            document_analysis_results[filename] = analysis

                            # Сохраняем в БД
                            try:
                                save_raw_data(
                                    procurement_id=procurement_id, 
                                    document_type=final_doc_type, 
                                    full_analysis=analysis
                                )
                                save_clean_conclusion(
                                    procurement_id=procurement_id, 
                                    document_type=final_doc_type, 
                                    conclusion=conclusion
                                )

                                logger.info(f"Сохранено в БД: {procurement_id}, {final_doc_type}")
                            except Exception as db_error:
                                logger.error(f"Ошибка сохранения в БД: {str(db_error)}")
                                conclusion += " [Ошибка сохранения в БД]"

                        # Формируем результат для документа
                        document_result = {
                            "procurement_id": procurement_id,
                            "document_type": final_doc_type,
                            "filename": filename,
                            "is_valid": is_valid,
                            "error": None,
                            "conclusion": conclusion,
                            "source": "uploaded_file"
                        }
                        
                        documents_results.append(document_result)

                    except Exception as e:
                        logger.error(f"Ошибка при обработке {filename}: {str(e)}", exc_info=True)
                        # Формируем результат с ошибкой
                        document_result = {
                            "procurement_id": procurement_id,
                            "document_type": "Дополнительные материалы",
                            "filename": filename,
                            "is_valid": False,
                            "error": str(e),
                            "conclusion": f"Критическая ошибка обработки: {str(e)}",
                            "source": "uploaded_file"
                        }
                        documents_results.append(document_result)

                    finally:
                        if os.path.exists(tmp_path):
                            os.unlink(tmp_path)

                except Exception as e:
                    logger.error(f"Критическая ошибка при обработке файла {filename}: {str(e)}", exc_info=True)
                    document_result = {
                        "procurement_id": procurement_id,
                        "document_type": "Дополнительные материалы",
                        "filename": filename,
                        "is_valid": False,
                        "error": str(e),
                        "conclusion": f"Критическая ошибка: {str(e)}",
                        "source": "uploaded_file"
                    }
                    documents_results.append(document_result)

        # ОБРАБОТКА ССЫЛОК
        if links:
            logger.info(f"Обработка {len(links)} ссылок для закупки {procurement_id}")
            
            for link in links:
                try:
                    logger.info(f"Обработка ссылки: {link}")
                    
                    # Парсим ссылку напрямую
                    parse_result = await parse_cloud_storage_link(link, procurement_id)
                    
                    if parse_result.get("status") == "success":
                        analysis = parse_result.get("analysis", {})
                        filename = parse_result.get("filename", f"cloud_doc_{hash(link)}")
                        
                        # Добавляем анализ в общие результаты
                        document_analysis_results[filename] = analysis
                        
                        # Определяем тип документа и валидность
                        if analysis.get("status") == "success":
                            doc_analysis = analysis.get("analysis", {})
                            actual_type = doc_analysis.get("type_compliance", {}).get("actual_type", "")
                            final_doc_type = actual_type if actual_type else "Дополнительные материалы"
                            confidence = doc_analysis.get("type_compliance", {}).get("confidence", 0)
                            is_valid = (
                                final_doc_type != "Дополнительные материалы" 
                                and confidence >= 0.7
                            )
                            conclusion = doc_analysis.get("conclusion", "Документ обработан из облачного хранилища")
                        else:
                            final_doc_type = "Дополнительные материалы"
                            is_valid = False
                            conclusion = f"Ошибка анализа облачного документа: {analysis.get('error', 'Неизвестная ошибка')}"
                        
                        # Формируем результат для облачного документа
                        document_result = {
                            "procurement_id": procurement_id,
                            "document_type": final_doc_type,
                            "filename": filename,
                            "is_valid": is_valid,
                            "error": None,
                            "conclusion": conclusion,
                            "source": "cloud_storage",
                            "original_link": link
                        }
                        
                        documents_results.append(document_result)
                        
                        # Сохраняем в БД
                        try:
                            if analysis.get("status") == "success":
                                save_raw_data(
                                    procurement_id=procurement_id, 
                                    document_type=final_doc_type, 
                                    full_analysis=doc_analysis
                                )
                                save_clean_conclusion(
                                    procurement_id=procurement_id, 
                                    document_type=final_doc_type, 
                                    conclusion=conclusion
                                )
                                logger.info(f"Сохранен облачный документ: {filename}")
                        except Exception as db_error:
                            logger.error(f"Ошибка сохранения облачного документа в БД: {str(db_error)}")
                    
                    else:
                        # Ошибка парсинга
                        error_msg = parse_result.get("error", "Неизвестная ошибка парсинга")
                        logger.error(f"Ошибка парсинга ссылки {link}: {error_msg}")
                        document_result = {
                            "procurement_id": procurement_id,
                            "document_type": "Дополнительные материалы",
                            "filename": f"failed_link_{hash(link)}",
                            "is_valid": False,
                            "error": error_msg,
                            "conclusion": f"Ошибка обработки ссылки: {error_msg}",
                            "source": "cloud_storage",
                            "original_link": link
                        }
                        documents_results.append(document_result)
                        
                except Exception as e:
                    logger.error(f"Критическая ошибка при обработке ссылки {link}: {str(e)}", exc_info=True)
                    document_result = {
                        "procurement_id": procurement_id,
                        "document_type": "Дополнительные материалы",
                        "filename": f"error_link_{hash(link)}",
                        "is_valid": False,
                        "error": str(e),
                        "conclusion": f"Критическая ошибка обработки ссылки: {str(e)}",
                        "source": "cloud_storage", 
                        "original_link": link
                    }
                    documents_results.append(document_result)

        # ПРОВЕРКА КОМПЛЕКТНОСТИ ДОКУМЕНТОВ
        completeness_check = {}
        
        if document_analysis_results:
            # Используем ИИ для проверки комплектности
            ai_completeness_result = await check_completeness_with_ai(
                procurement_id=procurement_id,
                required_documents=extract_required_docs_from_analysis(document_analysis_results),
                provided_documents=extract_provided_docs_from_results(documents_results)
            )
            
            # Преобразуем результат ИИ в совместимый формат
            completeness_check = {
                "status": "allow" if ai_completeness_result.get("completeness_status") == "полный" else "deny",
                "declared_attachments": extract_required_docs_from_analysis(document_analysis_results),
                "missing_in_upload": ai_completeness_result.get("missing_documents", []),
                "provided_documents": extract_provided_docs_from_results(documents_results),
                "source_docs_for_attached": list(document_analysis_results.keys()),
                "feedback": ai_completeness_result.get("reasoning", ""),
                "detailed_issues": [],
                "final_feedback": f"Статус комплектности: {ai_completeness_result.get('completeness_status', 'неизвестно')}. {ai_completeness_result.get('reasoning', '')}"
            }
            
            # Сохраняем результат проверки комплектности
            save_clean_conclusion(
                procurement_id=procurement_id,
                document_type="completeness_check",
                conclusion=completeness_check["final_feedback"]
            )

        # ПРОВЕРКА СОГЛАСОВАННОСТИ ДАННЫХ
        consistency_result = await check_documents_consistency(procurement_id)
        
        # Формируем общее заключение по согласованности
        overall_consistency_conclusion = consistency_result["conclusion"]
        if consistency_result["status"] == "ok":
            overall_consistency_conclusion = "Данные в документах согласованы."
        else:
            overall_consistency_conclusion = f"Обнаружены расхождения в документах: {consistency_result['conclusion']}"
        
        # Сохраняем результат проверки согласованности
        save_clean_conclusion(
            procurement_id=procurement_id,
            document_type="consistency_check",
            conclusion=overall_consistency_conclusion
        )

        # ФОРМИРУЕМ ОБЩЕЕ ЗАКЛЮЧЕНИЕ
        overall_conclusion_text, overall_status = generate_overall_conclusion(
            documents_results, 
            completeness_check, 
            consistency_result
        )

        # КЭШИРУЕМ РЕЗУЛЬТАТЫ
        document_analysis_cache[procurement_id] = {
            "documents": documents_results,
            "completeness_check": completeness_check,
            "consistency_check": consistency_result,
            "overall_conclusion": overall_status
        }

        # СОЗДАЕМ СВОДНЫЙ ОТЧЕТ
        try:
            summary_report = create_summary_report(
                procurement_id=procurement_id,
                documents_results=documents_results,
                document_analysis_results=document_analysis_results,
                completeness_check=completeness_check,
                consistency_result=consistency_result
            )

            # Сохраняем сводный отчет
            save_summary_report(procurement_id, summary_report)

            logger.info(f"Сводный отчет создан и сохранен для procurement_id={procurement_id}")
        except Exception as e:
            logger.error(f"Ошибка при создании сводного отчета: {str(e)}")

        # ВОЗВРАЩАЕМ РЕЗУЛЬТАТ
        result = {
            "procurement_id": procurement_id,
            "status": "processing_completed",
            "documents_processed": len(documents_results),
            "files_processed": len([d for d in documents_results if d.get("source") == "uploaded_file"]),
            "links_processed": len([d for d in documents_results if d.get("source") == "cloud_storage"]),
            "documents": documents_results,
            "overall_conclusion": overall_status,
            "completeness_summary": {
                "status": completeness_check.get("status", "unknown"),
                "conclusion": completeness_check.get("final_feedback", ""),
                "missing_documents": completeness_check.get("missing_in_upload", [])
            },
            "consistency_summary": {
                "status": consistency_result.get("status", "unknown"),
                "conclusion": overall_consistency_conclusion,
                "issues": consistency_result.get("issues", [])
            }
        }

        logger.info(f"Внутренняя обработка завершена для закупки {procurement_id}. Обработано документов: {len(documents_results)}")
        return result

    except Exception as e:
        logger.error(f"Критическая ошибка в evaluate_documents_batch_internal: {str(e)}", exc_info=True)
        return {
            "procurement_id": task_data.get('procurement_id', 'unknown'),
            "status": "error",
            "error": str(e),
            "documents_processed": 0,
            "documents": []
        }