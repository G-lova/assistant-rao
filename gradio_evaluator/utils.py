import os
import tempfile
import json
import logging

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
    """
    Возвращает список обязательных документов в зависимости от законодательства, способа закупки и деталей экспертизы.

    Функция извлекает требования к комплекту документов из вложенной структуры словаря
    `PROCUREMENT_REQUIREMENTS`, используя три уровня фильтрации:
    законодательство (например, "44-ФЗ"), способ закупки (например, "Конкурс") и
    тип экспертизы (например, "Полный комплект документов о закупке").

    Args:
        legislation (str): Тип законодательства (например, "44-ФЗ", "223-ФЗ").
        procurement_method (str): Способ проведения закупки (например, "Аукцион", "Конкурс").
        expertise_details (str): Детали или цель экспертизы, определяющие полноту требуемого комплекта.

    Returns:
        List[str]: Список названий документов, которые должны быть представлены.
                   Возвращает пустой список, если комбинация не найдена.
    """
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
    Обрабатывает и анализирует комплект документов закупки, проверяя их наличие, содержание и соответствие требованиям.

    Функция:
    1. Определяет список обязательных документов на основе законодательства, способа закупки и типа экспертизы.
    2. Обрабатывает документы из ручного списка (docs_json), отправляя каждый в API для анализа содержимого.
    3. Обрабатывает дополнительно загруженные файлы (mass_files), определяя их тип и отправляя на анализ.
    4. Проверяет полноту комплекта, выявляя отсутствующие документы.
    5. Формирует итоговый отчёт с указанием статуса (allow/deny), списка предоставленных и недостающих документов,
       краткого содержания, а также ошибок.

    Args:
        procurement_id (str): Уникальный идентификатор закупки.
        expertise_object (str): Объект экспертизы (например, наименование лота).
        legislation (str): Тип законодательства (например, "44-ФЗ").
        procurement_method (str): Способ проведения закупки (например, "Конкурс").
        expertise_details (str): Детали экспертизы, влияющие на состав документов.
        eis_link (str): Ссылка на закупку в ЕИС (не используется напрямую, но может быть зарезервирована).
        mass_files (List): Список загруженных файлов (например, через Streamlit), каждый с атрибутом `.name`.
        docs_json (str): JSON-строка с информацией о документах: их названия, статус (uploaded/missing), пути.

    Raises:
        ValueError: Если не заданы обязательные переменные окружения API_ACCESS или API_KEY.

    Returns:
        Dict: Словарь с результатами проверки, содержащий:
            - status: "allow" (все документы предоставлены) или "deny" (есть ошибки или недостающие документы);
            - required_documents: список обязательных документов;
            - provided_documents: список успешно обработанных документов;
            - missing_documents: список недостающих документов с пояснениями;
            - documents_content: словарь с кратким содержанием каждого обработанного документа (первые 100 символов);
            - errors: список сообщений об ошибках (проблемы с файлами, API и т.д.).
            - type_compliance_issues: список проблем с соответствием типов документов.
            В случае критической ошибки возвращается шаблон с "status": "deny" и деталями.
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
            "type_compliance_issues": [],
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
                            
                            # Проверяем соответствие типа документа
                            if not doc_data.get("is_valid", False):
                                expected_type = doc_name
                                actual_type = "неизвестно"
                                content = doc_data.get("content", "")
                                
                                # Пытаемся извлечь информацию о несоответствии типа
                                if "НЕСООТВЕТСТВИЕ ТИПА:" in content:
                                    type_info = content.split("НЕСОТВЕТСТВИЕ ТИПА:")[1].split("\n")[0].strip()
                                    results["type_compliance_issues"].append({
                                        "document_name": doc_name,
                                        "issue": type_info,
                                        "severity": "high"
                                    })
                                else:
                                    results["type_compliance_issues"].append({
                                        "document_name": doc_name,
                                        "issue": "Документ не соответствует заявленному типу",
                                        "severity": "high"
                                    })
                                
                                results["errors"].append(f"Документ {doc_name} не соответствует заявленному типу")
                            
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
                                "document_type": doc_type,
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
                        
                        # Проверяем соответствие типа документа
                        if not doc_data.get("is_valid", False):
                            expected_type = doc_type
                            content = doc_data.get("content", "")
                            
                            if "НЕСООТВЕТСТВИЕ ТИПА:" in content:
                                type_info = content.split("НЕСОТВЕТСТВИЕ ТИПА:")[1].split("\n")[0].strip()
                                results["type_compliance_issues"].append({
                                    "document_name": doc_type,
                                    "issue": type_info,
                                    "severity": "high"
                                })
                            else:
                                results["type_compliance_issues"].append({
                                    "document_name": doc_type,
                                    "issue": "Документ не соответствует заявленному типу",
                                    "severity": "high"
                                })
                            
                            results["errors"].append(f"Документ {doc_type} не соответствует заявленному типу")
                            
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
        if (results["missing_documents"] or 
            results["errors"] or 
            results["type_compliance_issues"]):
            results["status"] = "deny"
            
        # Добавляем детализированную информацию о проблемах
        if results["type_compliance_issues"]:
            results["feedback"] = "Обнаружены проблемы с соответствием типов документов:"
            for issue in results["type_compliance_issues"]:
                results["feedback"] += f"\n- {issue['document_name']}: {issue['issue']}"

        return results

    except Exception as e:
        logger.error(f"Критическая ошибка в show_documents_content: {str(e)}", exc_info=True)
        return {
            "status": "deny",
            "error": str(e),
            "required_documents": [],
            "provided_documents": [],
            "missing_documents": [],
            "type_compliance_issues": [],
            "documents_content": {},
            "errors": [str(e)]
        }


def update_document_list(legislation: str, procurement_method: str, expertise_details: str) -> Dict:
    """
    Обновляет список доступных документов на основе выбранных параметров закупки.

    Функция извлекает список обязательных документов из вложенной структуры `PROCUREMENT_REQUIREMENTS`
    в зависимости от указанного законодательства, способа закупки и деталей экспертизы.
    Возвращает структуру, совместимую с интерфейсом Gradio, для динамического обновления выпадающего списка.

    Args:
        legislation (str): Тип законодательства (например, "44-ФЗ").
        procurement_method (str): Способ проведения закупки (например, "Конкурс").
        expertise_details (str): Детали экспертизы, определяющие набор требуемых документов.

    Returns:
        Dict: Словарь с полями:
            - choices: список названий документов для отображения в интерфейсе;
            - value: значение по умолчанию (первый документ в списке или None);
            - __type__: тип ответа для Gradio (всегда "update").
            При ошибке возвращает пустой список и None.
    """
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
    """
    Добавляет новый документ в список или обновляет существующий.

    Функция работает с JSON-представлением списка документов, добавляя запись о новом документе
    с указанием его статуса (загружен или отсутствует), пути к файлу (если есть) и пояснения (если отсутствует).
    Поддерживает обновление записи, если документ с таким именем уже существует.

    Args:
        current_json (str): Текущее состояние списка документов в формате JSON (может быть пустым).
        doc_name (str): Название документа (например, "Извещение", "Документация о закупке").
        explanation (str): Пояснение, почему документ отсутствует (используется, если файл не загружен).
        doc_file: Загруженный файл документа (тип от Gradio, с атрибутом `.name` — путь к временному файлу).

    Raises:
        gr.Error: Если загруженный файл не найден на сервере.
        gr.Error: Если загруженный файл пустой.

    Returns:
        tuple: Кортеж из двух элементов:
            - Обновлённый список документов в виде JSON-строки (с кодировкой UTF-8);
            - Тот же список в виде Python-объекта (список словарей) для удобства дальнейшей обработки.
    """
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
    """
    Очищает список документов, возвращая пустое состояние.

    Returns:
        tuple: Кортеж из двух элементов:
            - None: означает отсутствие загруженного файла (сброс в Gradio);
            - Пустой список: текущий список документов после очистки.
    """
    return None, []


def toggle_explanation_visibility(document_name):
    """
    Управляет видимостью поля пояснения для отсутствующего документа.

    Возвращает конфигурацию для обновления компонента Gradio: показывает поле ввода
    пояснения, если выбрано имя документа, и скрывает его, если выбор снят.

    Args:
        document_name: Название выбранного документа (если есть) или пустое значение.

    Returns:
        gr.Textbox.update: Объект обновления для Gradio-компоненты, устанавливающий видимость.
    """
    return gr.Textbox.update(visible=bool(document_name))