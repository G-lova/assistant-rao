import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile

import magic
import cv2
import pandas as pd
import numpy as np
import PyPDF2
import rarfile
import textract
import yaml
import zipfile
import io
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx import Presentation
from paddleocr import PaddleOCR
from pdf2image import convert_from_path
from dotenv import load_dotenv
from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware

from configs.config import load_config


config = load_config("config_model")
load_dotenv()

logging.getLogger("PyPDF2").setLevel(logging.ERROR)


with open("configs/config_model.yaml", "r", encoding="utf-8") as file:
    config = yaml.safe_load(file)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Создаем экземпляр PaddleOCR
ocr_engine = PaddleOCR(
    use_angle_cls=True,
    lang='ru',
    use_gpu=False,
    show_log=False,
    use_doc_orientation_classify=False,
    use_doc_unwarping=False,
    use_textline_orientation=False
)


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
    Читает текст из файла формата .doc или .docx по указанному пути.

    Поддерживает извлечение текста из DOCX (с использованием специальной функции, 
    извлекающей также изображения) и DOC (через библиотеку textract, без поддержки изображений).
    Проверяет существование файла, его размер и корректность расширения.

    Args:
        file_path (str): Путь к файлу документа (.doc или .docx).

    Raises:
        ValueError: Если файл не существует.
        ValueError: Если файл пустой.
        ValueError: Если формат файла не поддерживается (не .doc и не .docx).
        ValueError: Если возникает ошибка при чтении файла DOC.
        ValueError: Если происходит ошибка обработки файла в целом.

    Returns:
        str: Извлечённый текст из документа в виде строки.
    """
    try:
        if not os.path.exists(file_path):
            raise ValueError("Файл не существует")
        
        if os.path.getsize(file_path) == 0:
            raise ValueError("Файл пустой")
        
        if file_path.lower().endswith('.docx'):
            return extract_text_and_images_from_docx(file_path)
        
        # Для DOC используем textract (без обработки изображений)
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
    Извлекает текст из PDF-файла с использованием комбинированного подхода.

    Сначала функция пытается извлечь текст напрямую из PDF-документа.
    Если извлечённый текст слишком короткий (менее 100 символов), 
    предполагается, что документ сканированный или текст нечитаем, 
    и тогда применяется OCR-обработка через конвертацию страниц в изображения 
    и распознавание текста с помощью Tesseract.

    Args:
        file_path (str): Путь к PDF-файлу, который необходимо прочитать.

    Returns:
        str: Извлечённый текст из всех страниц PDF, объединённый в одну строку.
             Каждая страница помечена как "Страница X (текст)" или "Страница X (OCR)".
             Возвращает пустую строку, если текст не был извлечён.
    """
    full_text = []
    
    try:
        # 1. Сначала пробуем извлечь обычный текст
        with open(file_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for page_num, page in enumerate(reader.pages, 1):
                page_text = page.extract_text()
                if page_text and page_text.strip():
                    full_text.append(f"Страница {page_num} (текст):\n{page_text}\n")
        
        # 2. Если текста мало - делаем OCR
        if len("\n".join(full_text).strip()) < 100:
            
            images = convert_from_path(file_path, dpi=300)
            for img_num, image in enumerate(images, 1):
                try:
                    with io.BytesIO() as output:
                        image.save(output, format='PNG')
                        ocr_text = extract_text_from_image(output.getvalue())
                        if ocr_text.strip():
                            full_text.append(f"Страница {img_num} (OCR):\n{ocr_text}\n")
                except Exception as e:
                    logger.error(f"Ошибка страницы {img_num}: {str(e)}")
        
        return "\n".join(full_text).strip()
    
    except Exception as e:
        raise ValueError(f"Ошибка чтения PDF: {str(e)}")


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


def extract_text_from_image(image_data: bytes) -> str:
    """
    Извлекает текст из изображения с использованием OCR-движка PaddleOCR.

    Функция принимает байтовые данные изображения, декодирует их в формат,
    пригодный для обработки OpenCV, и применяет оптическое распознавание
    символов для извлечения читаемого текста. Поддерживает распознавание
    на нескольких языках и классификацию текста (например, поворот).

    Args:
        image_data (bytes): Байтовое представление изображения (например, PNG, JPG),
                            из которого необходимо извлечь текст.

    Returns:
        str: Извлечённый текст в виде одной строки, объединённой через пробел.
             Возвращает пустую строку, если текст не был распознан или произошла ошибка.
    """

    try:
        # Конвертация в numpy array
        nparr = np.frombuffer(image_data, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        # Распознавание текста
        result = ocr_engine.ocr(img, cls=True)
        
        # Обработка результатов
        texts = []
        if result and result[0]:
            for line in result[0]:
                if line and len(line) >= 2:
                    text = line[1][0]  # Получаем текст
                    texts.append(text)
        
        return " ".join(texts).strip()
    
    except Exception as e:
        logger.error(f"OCR Error: {str(e)}")
        return ""


def extract_text_and_images_from_docx(file_path: str) -> str:
    """
    Извлекает текст и распознаёт текст с изображений из документа формата DOCX.

    Функция извлекает весь текст из абзацев и таблиц документа, а также находит
    встроенные изображения (PNG, JPG, JPEG) через прямое чтение ZIP-структуры DOCX.
    Для каждого изображения выполняется OCR с помощью функции extract_text_from_image,
    и распознанный текст добавляется в результат с пояснением источника.

    Временные файлы изображений сохраняются во временной директории, которая
    автоматически удаляется после завершения обработки.

    Args:
        file_path (str): Путь к файлу DOCX, который необходимо обработать.

    Raises:
        ValueError: Если возникает ошибка при чтении или обработке DOCX-файла.

    Returns:
        str: Объединённый текст, содержащий:
             - обычный текст из абзацев и таблиц;
             - распознанный текст с изображений с пометкой "[Текст с изображения ...]".
             Все элементы разделены переносами строк. Возвращает пустую строку,
             если текст и изображения отсутствуют или не были распознаны.
    """

    try:
        # Создаем временную директорию для изображений
        with tempfile.TemporaryDirectory() as temp_dir:
            doc = Document(file_path)
            result = []
            
            # 1. Извлекаем весь текст
            for para in doc.paragraphs:
                if para.text.strip():
                    result.append(para.text)
            
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        if cell.text.strip():
                            result.append(cell.text)
            
            # 2. Извлекаем изображения более надежным способом
            image_texts = []
            doc_zip = zipfile.ZipFile(file_path)
            
            # Ищем все файлы изображений в документе
            image_files = []
            for name in doc_zip.namelist():
                if name.startswith('word/media/') and name.split('.')[-1].lower() in ['png', 'jpg', 'jpeg']:
                    image_files.append(name)
            
            # Обрабатываем найденные изображения
            for img_file in image_files:
                try:
                    # Извлекаем изображение во временную директорию
                    img_data = doc_zip.read(img_file)
                    img_path = os.path.join(temp_dir, os.path.basename(img_file))
                    with open(img_path, 'wb') as f:
                        f.write(img_data)
                    
                    # Распознаем текст
                    ocr_text = extract_text_from_image(img_data)
                    if ocr_text.strip():
                        image_texts.append(f"[Текст с изображения {os.path.basename(img_file)}]: {ocr_text}")
                except Exception as e:
                    logger.error(f"Ошибка обработки изображения {img_file}: {str(e)}")
                    continue
            
            # 3. Комбинируем результаты
            if image_texts:
                result.append("\n".join(image_texts))
            
            return "\n".join(result)
    
    except Exception as e:
        logger.error(f"Полная ошибка обработки DOCX: {str(e)}")
        raise ValueError(f"Ошибка обработки DOCX: {str(e)}")


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