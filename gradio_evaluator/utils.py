import os
import tempfile
import json
import logging
import time
import shutil
import io

import requests
import gradio as gr
from typing import List, Dict

from procurement_requirements import PROCUREMENT_REQUIREMENTS


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_temp_file(content: str, suffix: str) -> str:
    """
    Создает временный файл с указанным содержимым и суффиксом.

    Args:
        content (str): Содержимое файла.
        suffix (str): Суффикс для имени временного файла.

    Returns:
        str: Путь к созданному временному файлу.
    """
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=suffix, mode="w", encoding="utf-8"
    ) as tmp:
        tmp.write(content)
        return tmp.name


def save_uploaded_file(file) -> str:
    """
    Сохраняет загруженный файл во временную директорию.

    Args:
        file (_type_): Загруженный файл.

    Returns:
        str: Путь к сохраненному файлу.
    """
    # Создание временной директории для загруженных файлов
    upload_dir = os.path.join(tempfile.gettempdir(), "gradio_uploads")
    os.makedirs(upload_dir, exist_ok=True)
    # Сохранение файла во временную директорию
    file_path = os.path.join(upload_dir, os.path.basename(file.name))
    with open(file_path, "wb") as buffer:
        buffer.write(file.read())
    return file_path


def cleanup_temp_files(file_path: str):
    """
    Удаляет временный файл, если он существует.

    Args:
        file_path (str): Путь к временному файлу.
    """
    if os.path.exists(file_path):
        os.remove(file_path)


def get_required_documents(legislation: str, procurement_method: str, expertise_details: str) -> List[str]:
    """Получает список обязательных документов из словаря требований"""
    return (PROCUREMENT_REQUIREMENTS.get(legislation, {})
                                  .get(procurement_method, {})
                                  .get(expertise_details, []))


def show_documents_content(
    procurement_id: str,
    expertise_object: str,
    legislation: str,
    procurement_method: str,
    expertise_details: str,
    eis_link: str,
    mass_files: List,
    docs_json: str
) -> Dict:
    """
    Обрабатывает загруженные документы: отправляет в API, проверяет комплектность.
    Поддерживает:
    - Ручную загрузку (через docs_json)
    - Массовую загрузку (через mass_files)
    """
    try:
        api_url = os.getenv("API_ACCESS")
        api_key = os.getenv("API_KEY")
        if not api_url:
            raise ValueError("API_ACCESS не задан в .env")
        if not api_key:
            raise ValueError("API_KEY не задан в .env")

        # Получаем список обязательных документов
        required_docs = get_required_documents(legislation, procurement_method, expertise_details)

        # Подготавливаем результаты
        results = {
            "status": "allow",
            "required_documents": required_docs,
            "provided_documents": [],
            "missing_documents": [],
            "documents_content": {},
            "errors": []
        }

        # --- 1. Обработка документов из ручного списка (docs_json) ---
        try:
            docs_list = json.loads(docs_json) if docs_json else []
            for doc in docs_list:
                doc_name = doc.get("document_name")
                if not doc_name:
                    continue

                if doc.get("status") == "uploaded" and doc.get("file_path"):
                    file_path = doc["file_path"]

                    # Проверяем файл
                    if not os.path.exists(file_path):
                        results["errors"].append(f"Файл не найден: {file_path}")
                        continue
                    if os.path.getsize(file_path) == 0:
                        results["errors"].append(f"Файл пуст: {file_path}")
                        continue

                    logger.info(f"Отправляю документ: {doc_name} ({file_path})")

                    # Открываем файл и отправляем в API
                    try:
                        with open(file_path, "rb") as f:
                            response = requests.post(
                                f"{api_url}/evaluate-documents",
                                files={
                                    "file": (os.path.basename(file_path), f, "application/octet-stream")
                                },
                                data={
                                    "procurement_id": procurement_id,
                                    "document_type": doc_name,
                                    "legislation": legislation,
                                    "procurement_method": procurement_method,
                                    "expertise_details": expertise_details
                                },
                                headers={"X-API-Key": api_key},
                                timeout=60
                            )
                        if response.status_code == 200:
                            doc_data = response.json()
                            results["provided_documents"].append(doc_name)
                            results["documents_content"][doc_name] = doc_data.get("content", "")[:100]
                        else:
                            error_msg = f"Ошибка API для {doc_name}: {response.status_code} — {response.text}"
                            results["errors"].append(error_msg)
                            logger.error(error_msg)
                    except Exception as e:
                        error_msg = f"Ошибка отправки {doc_name}: {str(e)}"
                        results["errors"].append(error_msg)
                        logger.error(error_msg, exc_info=True)

                elif doc.get("status") == "missing":
                    results["missing_documents"].append({
                        "document_name": doc_name,
                        "explanation": doc.get("explanation", "Не указана причина отсутствия")
                    })

        except json.JSONDecodeError as e:
            error_msg = "Некорректный JSON списка документов"
            results["errors"].append(error_msg)
            logger.error(f"{error_msg}: {str(e)}", exc_info=True)

        # --- 2. Обработка массовой загрузки (mass_files) ---
        if mass_files:
            for file in mass_files:
                if not hasattr(file, 'name'):
                    continue
                file_path = file.name
                filename = os.path.basename(file_path)

                if not os.path.exists(file_path):
                    results["errors"].append(f"Массовый файл не найден: {filename}")
                    continue
                if os.path.getsize(file_path) == 0:
                    results["errors"].append(f"Массовый файл пуст: {filename}")
                    continue

                # Попробуем определить тип документа по имени
                doc_type = None
                for required_doc in required_docs:
                    if required_doc.lower() in filename.lower():
                        doc_type = required_doc
                        break
                doc_type = doc_type or filename  # fallback

                logger.info(f"Массовая загрузка: {filename} → тип: {doc_type}")

                try:
                    with open(file_path, "rb") as f:
                        response = requests.post(
                            f"{api_url}/evaluate-documents",
                            files={
                                "file": (os.path.basename(file_path), f, "application/octet-stream")
                            },
                            data={
                                "procurement_id": procurement_id,
                                "document_type": doc_name,
                                "legislation": legislation,
                                "procurement_method": procurement_method,
                                "expertise_details": expertise_details
                            },
                            headers={"X-API-Key": api_key},
                            timeout=60
                        )
                    if response.status_code == 200:
                        doc_data = response.json()
                        results["provided_documents"].append(doc_type)
                        results["documents_content"][doc_type] = doc_data.get("content", "")[:100]
                    else:
                        error_msg = f"Ошибка API (масс.) для {filename}: {response.status_code} — {response.text}"
                        results["errors"].append(error_msg)
                        logger.error(error_msg)
                except Exception as e:
                    error_msg = f"Ошибка отправки (масс.) {filename}: {str(e)}"
                    results["errors"].append(error_msg)
                    logger.error(error_msg, exc_info=True)

        # --- 3. Проверка комплектности: отсутствующие документы ---
        for req_doc in required_docs:
            if (req_doc not in results["provided_documents"] and
                not any(md["document_name"] == req_doc for md in results["missing_documents"])):
                results["missing_documents"].append({
                    "document_name": req_doc,
                    "explanation": "Не предоставлен"
                })

        # --- 4. Финальный статус ---
        if results["missing_documents"] or results["errors"]:
            results["status"] = "deny"

        return results

    except Exception as e:
        logger.error(f"Критическая ошибка в show_documents_content: {str(e)}", exc_info=True)
        return {
            "status": "deny",
            "error": str(e),
            "required_documents": [],
            "provided_documents": [],
            "missing_documents": [],
            "documents_content": {},
            "errors": [str(e)]
        }


def update_document_list(legislation: str, procurement_method: str, expertise_details: str) -> Dict:
    """Обновляет список документов на основе выбранных параметров"""
    try:
        required = (PROCUREMENT_REQUIREMENTS.get(legislation, {})
                                 .get(procurement_method, {})
                                 .get(expertise_details, []))
        return {
            "choices": required,
            "value": required[0] if required else None,
            "__type__": "update"
        }
    except Exception as e:
        logger.error(f"Error updating document list: {str(e)}")
        return {
            "choices": [],
            "value": None,
            "__type__": "update"
        }


def add_document(current_json: str, doc_name: str, explanation: str, doc_file) -> tuple:
    """Добавляет документ в список (либо как загруженный, либо как отсутствующий)"""
    if not doc_name:
        return current_json, []

    try:
        current_list = json.loads(current_json) if current_json else []
    except json.JSONDecodeError:
        current_list = []

    file_path = None
    if doc_file is not None:
        # Gradio уже сохранил файл во временную директорию
        file_path = doc_file.name  # Это путь к временному файлу
        if not os.path.exists(file_path):
            raise gr.Error("Файл не найден на сервере")
        if os.path.getsize(file_path) == 0:
            raise gr.Error("Файл пустой")

    # Проверяем, нет ли уже такого документа
    existing_doc_index = next(
        (i for i, doc in enumerate(current_list) if doc.get('document_name') == doc_name),
        None
    )

    doc_data = {
        "document_name": doc_name,
        "status": "uploaded" if file_path else "missing",
    }
    if file_path:
        doc_data["file_path"] = file_path
    else:
        doc_data["explanation"] = explanation or "Не указана причина отсутствия"

    if existing_doc_index is not None:
        current_list[existing_doc_index] = doc_data
    else:
        current_list.append(doc_data)

    return json.dumps(current_list, ensure_ascii=False), current_list


def clear_documents_list() -> tuple:
    """Очищает список документов"""
    return None, []


def toggle_explanation_visibility(document_name):
    """Переключает видимость поля объяснения"""
    return gr.Textbox.update(visible=bool(document_name))