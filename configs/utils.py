import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import secrets
import magic
import pandas as pd
import numpy as np
import PyPDF2
import rarfile
import textract
import yaml
import zipfile
import io
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

config = Config.get_model_config()
load_dotenv()

logging.getLogger("PyPDF2").setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in ["/docs", "/openapi.json", "/redoc"]:
            return await call_next(request)
        try:
            api_key = request.headers.get("X-API-Key")
            if not api_key:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "API key missing"}
                )
            expected_api_key = os.getenv("API_KEY")
            if not expected_api_key:
                return JSONResponse(
                    status_code=500,
                    content={"detail": "API_KEY not configured"}
                )
            if not secrets.compare_digest(api_key, expected_api_key):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Invalid API key"}
                )
            return await call_next(request)
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"detail": f"Internal server error: {str(e)}"}
            )


def get_required_documents(legislation: str, procurement_method: str, expertise_details: str) -> List[str]:
    """Получает список обязательных документов из словаря требований"""
    return (PROCUREMENT_REQUIREMENTS.get(legislation, {})
                                  .get(procurement_method, {})
                                  .get(expertise_details, []))


def extract_filename(path):
    match = re.search(r'[/\\]([^/\\]+)\.csv$', path)
    return match.group(1) if match else os.path.splitext(os.path.basename(path))[0]


def read_txt_file(file_path: str) -> str:
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()


def read_doc_file(file_path: str) -> str:
    try:
        if not os.path.exists(file_path):
            raise ValueError("Файл не существует")
        if os.path.getsize(file_path) == 0:
            raise ValueError("Файл пустой")
        if file_path.lower().endswith('.docx'):
            return extract_text_and_images_from_docx(file_path)
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
    presentation = Presentation(file_path)
    text_content = []
    for slide in presentation.slides:
        for shape in slide.shapes:
            if hasattr(shape, "text"):
                text_content.append(shape.text)
    return "\n".join(text_content)


def read_pdf_file(file_path: str) -> str:
    full_text = []
    try:
        with open(file_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for page_num, page in enumerate(reader.pages, 1):
                page_text = page.extract_text()
                if page_text and page_text.strip():
                    full_text.append(f"Страница {page_num} (текст):\n{page_text}\n")
        # Если текста мало — конвертируем в изображение и передаём в модель
        if len("\n".join(full_text).strip()) < 100:
            return f"Бинарный PDF (нечитаемый): {os.path.basename(file_path)}"
        return "\n".join(full_text).strip()
    except Exception as e:
        raise ValueError(f"Ошибка чтения PDF: {str(e)}")


def read_excel_file(file_path: str) -> str:
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
            if not os.path.exists(temp_xlsx_path):
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


def process_archive(file_path: str, archive_class) -> str:
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


def extract_text_and_images_from_docx(file_path: str) -> str:
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
            image_files = []
            for name in doc_zip.namelist():
                if name.startswith('word/media/') and name.split('.')[-1].lower() in ['png', 'jpg', 'jpeg']:
                    image_files.append(name)
            for img_file in image_files:
                try:
                    img_data = doc_zip.read(img_file)
                    img_path = os.path.join(temp_dir, os.path.basename(img_file))
                    with open(img_path, 'wb') as f:
                        f.write(img_data)
                    # Не делаем OCR — передаём изображение в модель
                    image_texts.append(f"[Изображение: {os.path.basename(img_file)}] (OCR будет в модели)")
                except Exception as e:
                    logger.error(f"Ошибка обработки изображения {img_file}: {str(e)}")
                    continue
            if image_texts:
                result.append("\n".join(image_texts))
            return "\n".join(result)
    except Exception as e:
        logger.error(f"Полная ошибка обработки DOCX: {str(e)}")
        raise ValueError(f"Ошибка обработки DOCX: {str(e)}")


def read_file(file_path: str, original_filename: str = None) -> str:
    """
    Читает файл по пути, используя original_filename для определения расширения.
    """
    filename_to_check = original_filename or os.path.basename(file_path)
    _, ext = os.path.splitext(filename_to_check)
    ext = ext.lower()

    try:
        if ext == ".txt":
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        elif ext == ".pdf":
            return read_pdf_file(file_path)
        elif ext in [".xlsx", ".xls"]:
            return read_excel_file(file_path)
        elif ext == ".pptx":
            return read_pptx_file(file_path)
        elif ext in [".csv", ".json"]:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        elif ext in [".doc", ".docx"]:
            return read_doc_file(file_path)
        else:
            return f"Бинарный файл {os.path.basename(file_path)} (формат {ext})"
    except Exception as e:
        raise ValueError(f"Ошибка чтения файла: {str(e)}")


def check_procurement_completeness(law_type: str, procurement_type: str, check_type: str, files: Dict[str, str]) -> Dict:
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