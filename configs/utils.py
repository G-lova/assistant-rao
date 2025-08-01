import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile

import magic
import pandas as pd
import PyPDF2
import rarfile
import textract
import yaml
import zipfile
from docx import Document
from pptx import Presentation

from dotenv import load_dotenv
from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware

from configs.config import load_config



config = load_config("config_model")
load_dotenv()

logging.getLogger("PyPDF2").setLevel(logging.ERROR)

with open("configs/config_model.yaml", "r", encoding="utf-8") as file:
    config = yaml.safe_load(file)


class APIKeyMiddleware(BaseHTTPMiddleware):
    """
    Middleware для проверки API-ключа в запросах.

    Args:
        BaseHTTPMiddleware (BaseHTTPMiddleware): Базовый класс middleware для обработки HTTP-запросов.
    """
    async def dispatch(self, request: Request, call_next):
        """
        Обрабатывает входящий HTTP-запрос и проверяет наличие и валидность API-ключа.

        Args:
            request (Request): Входящий HTTP-запрос.
            call_next (Callable): Функция для передачи запроса следующему обработчику в цепочке middleware.

        Returns:
            Response: Ответ на HTTP-запрос после его обработки.
        """
        if request.url.path.startswith("/api"):
            self._verify_api_key(request)
        return await call_next(request)

    def _verify_api_key(self, request):
        """
        Проверяет API-ключ в заголовках запроса.

        Args:
            request (Request): Входящий HTTP-запрос.

        Raises:
            HTTPException: Выбрасывается, если API-ключ отсутствует.
            HTTPException: Выбрасывается, если переменная окружения API_KEY_HASH не настроена.
            HTTPException: Выбрасывается, если предоставленный API-ключ неверный.
        """
        api_key = request.headers.get("X-API-Key")
        if not api_key:
            raise HTTPException(status_code=403, detail="API key missing")
        
        hashed_key = hashlib.sha256(api_key.encode()).hexdigest()
        expected_hash = os.getenv("API_KEY_HASH", "").replace("sha256:", "")
        
        if not expected_hash:
            raise HTTPException(status_code=500, detail="API_KEY_HASH not configured")
        if hashed_key != expected_hash:
            raise HTTPException(status_code=403, detail="Invalid API key")


def extract_filename(path):
    """
    Извлекает имя файла без расширения из указанного пути.

    Args:
        path (str): Путь к файлу, из которого нужно извлечь имя.

    Returns:
        str: Имя файла без расширения.
    """
    match = re.search(r'[/\\]([^/\\]+)\.csv$', path)
    return match.group(1) if match else os.path.splitext(os.path.basename(path))[0]


def read_txt_file(file_path: str) -> str:
    """
    Читает содержимое текстового файла и возвращает его в виде строки.

    Args:
        file_path (str): Путь к текстовому файлу, который нужно прочитать.

    Returns:
        str: Содержимое текстового файла в виде строки.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()


def read_doc_file(file_path: str) -> str:
    """
    Читает содержимое DOC или DOCX файла и возвращает его текстовое представление.

    Args:
        file_path (str): Путь к файлу DOC или DOCX, который нужно прочитать.

    Raises:
        ValueError: Выводится, если файл не является валидным DOC или DOCX файлом.
        ValueError: Выводится, если произошла ошибка при чтении DOC файла.
        ValueError: Выводится, если произошла ошибка при чтении DOCX файла.

    Returns:
        str: Текстовое содержимое DOC или DOCX файла.
    """
    # Проверка MIME-типа файла
    mime = magic.Magic(mime=True)
    file_mime_type = mime.from_file(file_path)

    if file_mime_type == "application/msword":  # Валидный .doc файл
        try:
            # Использование textract для извлечения текста из DOC файла
            text_content = textract.process(file_path, encoding='utf-8').decode('utf-8')
            return text_content
        except Exception as e:
            raise ValueError(f"Ошибка при чтении DOC файла {file_path}: {str(e)}")
    elif file_mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":  # Валидный .doc файл
        try:
            document = Document(file_path)
            full_text = []
            for para in document.paragraphs:
                full_text.append(para.text)
            return "\n".join(full_text)
        except Exception as e:
            raise ValueError(f"Ошибка при чтении DOCX файла {file_path}: {str(e)}")
    else:
        raise ValueError(f"Файл {file_path} не является валидным DOC или DOCX файлом. MIME-тип: {file_mime_type}")


def read_pptx_file(file_path: str) -> str:
    """
    Читает текстовое содержимое из PPTX файла.

    Args:
        file_path (str): Путь к файлу PPTX, который нужно прочитать.

    Returns:
        str: Текстовое содержимое всех слайдов PPTX файла, объединенное в одну строку.
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
    Читает текстовое содержимое из PDF файла.

    Args:
        file_path (str): Путь к файлу PDF, который нужно прочитать.

    Returns:
        str: Текстовое содержимое всех страниц PDF файла, объединенное в одну строку.
    """
    text = []
    with open(file_path, 'rb') as f:
        reader = PyPDF2.PdfReader(f)
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text.append(page_text)
    return '\n'.join(text)


def read_excel_file(file_path: str) -> str:
    """
    Читает содержимое Excel файла (.xls или .xlsx) и возвращает его в виде строки.
    Для файлов формата .xls функция сначала использует LibreOffice для конвертации их в формат .xlsx.
    Содержимое всех листов извлекается и форматируется в читаемом текстовом представлении.

    Args:
        file_path (str): Путь к Excel файлу, который нужно прочитать.

    Raises:
        ValueError: Выбрасывается, если файл не существует или имеет неподдерживаемое расширение.
        RuntimeError: Выбрасывается, если произошла ошибка при конвертации или чтении Excel файла.

    Returns:
        str: Строка, содержащая содержимое всех листов Excel файла.
    """
    # Проверка существования файла
    if not os.path.exists(file_path):
        raise ValueError(f"Файл не найден: {file_path}")
    
    # Проверка расширения файла
    base, ext = os.path.splitext(file_path)
    ext = ext.lower()
    
    if ext not in ('.xls', '.xlsx'):
        raise ValueError("Поддерживаются только файлы .xls и .xlsx")
    
    temp_dir = None
    temp_xlsx_path = None
    
    try:
        # Конвертация .xls в .xlsx с использованием LibreOffice
        if ext == '.xls':
            temp_dir = tempfile.mkdtemp()
            temp_xlsx_path = os.path.join(temp_dir, os.path.basename(base) + ".xlsx")
            
            # Команда для конвертации
            cmd = [
                'libreoffice', 
                '--headless',
                '--convert-to', 
                'xlsx', 
                '--outdir', 
                temp_dir,
                file_path
            ]
            
            # Выполнение конвертации
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode != 0:
                raise RuntimeError(f"Ошибка конвертации: {result.stderr}")
            
            if not os.path.exists(temp_xlsx_path):
                # LibreOffice может немного изменить имя файла
                converted_files = [f for f in os.listdir(temp_dir) if f.endswith('.xlsx')]
                if not converted_files:
                    raise RuntimeError("Не удалось найти сконвертированный файл")
                temp_xlsx_path = os.path.join(temp_dir, converted_files[0])
            
            file_to_read = temp_xlsx_path
        else:
            file_to_read = file_path
        
        # Чтение данных из Excel
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


def process_archive(file_path: str, archive_class) -> str:
    """
    Обрабатывает архивный файл и возвращает текстовое содержимое всех файлов.

    Args:
        file_path (str): Путь к архивному файлу, который нужно обработать.
        archive_class (_type_): Класс для работы с архивом (например, ZipFile или TarFile).

    Returns:
        str: Текстовое представление содержимого всех файлов в архиве.
    """
    contents = []
    with archive_class(file_path) as archive:
        for file_info in archive.infolist():
            if not file_info.is_dir():
                with archive.open(file_info) as file:
                    try:
                        content = file.read().decode('utf-8')
                        contents.append(f"=== File: {file_info.filename} ===\n{content}")
                    except UnicodeDecodeError:
                        contents.append(f"=== File: {file_info.filename} (binary) ===")
    return '\n'.join(contents)


def read_file(file_path: str) -> str:
    """Читает содержимое файла поддерживаемого формата и возвращает его текстовое представление.
    
    Поддерживаемые форматы:
    - Текстовые файлы: .txt, .csv, .json
    - Документы: .doc, .docx
    - PDF: .pdf
    - Таблицы: .xls, .xlsx
    - Презентации: .pptx
    
    Для бинарных файлов неподдерживаемых форматов возвращает информационное сообщение.

    Args:
        file_path (str): Путь к файлу, который необходимо прочитать. Должен быть абсолютным или относительным путем.

    Raises:
        ValueError: Возникает при ошибках чтения файла:
            - Файл не существует или недоступен
            - Ошибка декодирования содержимого
            - Ошибка в специализированных функциях чтения (read_doc_file, read_pdf_file и т.д.)

    Returns:
        str: Текстовое содержимое файла. Для бинарных файлов возвращает строку с информацией о файле.
    """
    _, ext = os.path.splitext(file_path)
    ext = ext.lower()

    try:
        if ext == ".txt":
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        elif ext in [".doc", ".docx"]:
            return read_doc_file(file_path)
        elif ext == ".pdf":
            return read_pdf_file(file_path)
        elif ext in [".xlsx", ".xls"]:
            return read_excel_file(file_path)
        elif ext == ".pptx":
            return read_pptx_file(file_path)
        elif ext in [".csv", ".json"]:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        else:
            # Для бинарных файлов возвращаем информацию о файле
            return f"Бинарный файл {os.path.basename(file_path)} (формат {ext})"
    except Exception as e:
        raise ValueError(f"Ошибка чтения файла: {str(e)}")