import os
import json
import tempfile
import requests
import time
from typing import List, Dict, Any

import gradio as gr
from dotenv import load_dotenv

from utils import (
    get_document_code_by_label,
    format_final_response
)
from procurement_requirements import DOCUMENT_CODE_TO_LABEL


load_dotenv()


API_ACCESS = os.getenv("API_ACCESS")
API_KEY = os.getenv("API_KEY")

if not API_ACCESS or not API_KEY:
    raise ValueError("Переменные окружения API_ACCESS и API_KEY обязательны")


# === Импортируем справочник из configs ===
LABEL_TO_CODE = {v: k for k, v in DOCUMENT_CODE_TO_LABEL.items()}
DOCUMENT_LABELS = sorted(DOCUMENT_CODE_TO_LABEL.values())


def add_document_to_list(
    current_media: str,
    selected_doc: str,
    file_obj,
    doc_link: str,
    comment: str
) -> tuple:
    """
    Добавляет или обновляет документ в списке прикреплённых материалов в формате JSON.

    Принимает текущий список документов (в виде JSON-строки), тип выбранного документа,
    загруженный файл, ссылку и комментарий, определяет код документа по его названию,
    формирует новую запись и либо заменяет существующую с тем же кодом, либо добавляет как новую.
    Возвращает обновлённый список в виде JSON-строки (два одинаковых значения для совместимости с Gradio).

    Args:
        current_media (str): Текущий список документов в формате JSON-строки (может быть пустым).
        selected_doc (str): Название выбранного типа документа (например, "Извещение").
        file_obj (_type_): Объект загруженного файла (ожидается атрибут name и существующий путь).
        doc_link (str): Ссылка на документ в облаке (опционально).
        comment (str): Комментарий к документу (опционально).

    Returns:
        tuple: Кортеж из двух одинаковых строк — обновлённого JSON-списка документов.
    """
    if not selected_doc:
        return current_media, current_media

    try:
        current_list = json.loads(current_media) if current_media else []
    except json.JSONDecodeError:
        current_list = []

    code = LABEL_TO_CODE.get(selected_doc, "unknown")

    files_list = []
    if file_obj and hasattr(file_obj, 'name') and os.path.exists(file_obj.name):
        files_list = [os.path.basename(file_obj.name)]

    links_list = []
    if doc_link and doc_link.strip():
        links_list = [doc_link.strip()]

    new_item = {
        "code": code,
        "name": selected_doc,
        "links": links_list,
        "files": files_list,
        "comment": comment or ""
    }

    for i, item in enumerate(current_list):
        if item["code"] == code:
            current_list[i] = new_item
            break
    else:
        current_list.append(new_item)

    json_str = json.dumps(current_list, ensure_ascii=False)
    return json_str, json_str


def clear_document_list():
    return "", ""


def process_evaluation(
    procurement_id: str,
    legislation: str,
    procurement_method: str,
    organization: str,
    eis_link: str,
    media_json: str,
    files: list
):
    """
    Запускает и отслеживает процесс комплексной экспертизы документов закупки через внешний API.

    Выполняет валидацию входных данных, формирует multipart-запрос с файлами и метаданными,
    отправляет задачу на обработку, а затем опрашивает статус выполнения до завершения,
    ошибки или таймаута. Поддерживает ассоциацию файлов с кодами из media_json и корректное
    закрытие ресурсов. Возвращает промежуточные и финальные сообщения через генератор.

    Args:
        procurement_id (str): Уникальный идентификатор закупки.
        legislation (str): Тип законодательства (например, "44-ФЗ").
        procurement_method (str): Способ закупки (например, "Конкурс").
        organization (str): Наименование заказчика.
        eis_link (str): Ссылка на закупку в ЕИС (опционально).
        media_json (str): JSON-строка с описанием медиафайлов и их кодов (опционально).
        files (list): Список объектов файлов для загрузки (например, из Gradio.File).

    Returns:
        _type_: Не возвращает напрямую (используется yield). Конечный результат — кортеж из
            строки с сообщением и словаря с данными.

    Yields:
        _type_: Последовательность кортежей (сообщение: str, данные: dict), отражающих
            текущий статус обработки: запуск задачи, ожидание, ошибка или финальный результат.
    """
    if not procurement_id.strip():
        return "❌ Укажите ID закупки", {}
    if not organization.strip():
        return "❌ Укажите заказчика", {}
    if not files and not media_json:
        return "❌ Загрузите хотя бы один документ или укажите ссылку", {}

    try:
        # === 1. Подготовка файлов с префиксами code___filename ===
        file_objects = []
        if files:
            for f in files:
                if not hasattr(f, 'name') or not os.path.exists(f.name):
                    continue
                orig_name = os.path.basename(f.name)
                code = "unknown"
                try:
                    media_list = json.loads(media_json) if media_json else []
                    for item in media_list:
                        if orig_name in [os.path.basename(p) for p in item.get("files", [])]:
                            code = item["code"]
                            break
                except:
                    pass
                prefixed_name = f"{code}___{orig_name}"
                file_objects.append(("files", (prefixed_name, open(f.name, "rb"), "application/octet-stream")))

        # === 2. Отправка задачи ===
        form_data = {
            "procurement_id": procurement_id,
            "type": legislation,
            "checkType2": procurement_method,
            "object": "Закупки",
            "users.organization": organization,
            "linkDocs": eis_link or "",
            "media_json": media_json or "[]"
        }

        headers = {"X-API-Key": API_KEY}

        response = requests.post(
            f"{API_ACCESS}/evaluate-documents",
            files=file_objects,
            data=form_data,
            headers=headers,
            timeout=60
        )

        # Закрываем файлы
        for _, (_, f_obj, _) in file_objects:
            f_obj.close()

        if response.status_code != 200:
            try:
                err = response.json().get("detail", response.text)
            except:
                err = response.text
            return f"❌ Ошибка запуска задачи: {response.status_code} — {err}", {"error": err}

        task_info = response.json()
        task_id = task_info.get("task_id")
        if not task_id:
            return "❌ Не получен task_id", {"error": "Не получен task_id"}

        yield "🚀 Задача запущена. Ожидание результата...", {"status": "processing", "task_id": task_id}

        # === 3. Polling результата ===
        polling_url = f"{API_ACCESS}/task/{task_id}"
        max_wait = 600  # 10 минут
        poll_interval = 5  # секунд
        waited = 0

        while waited < max_wait:
            time.sleep(poll_interval)
            waited += poll_interval

            try:
                result_resp = requests.get(polling_url, headers=headers, timeout=10)
                if result_resp.status_code == 200:
                    task_result = result_resp.json()
                    if task_result.get("status") == "completed":
                        final_result = task_result.get("result", {})
                        if isinstance(final_result, str):
                            try:
                                final_result = json.loads(final_result)
                            except:
                                pass
                        formatted = format_final_response(final_result)
                        yield formatted, final_result
                        return
                    elif task_result.get("status") in ("pending", "started", "retrying"):
                        yield f"⏳ Задача в обработке... ({waited}/{max_wait} сек)", {"status": "processing", "waited": waited}
                        continue
                    elif task_result.get("status") == "failed":
                        error_detail = task_result.get("error", "Неизвестная ошибка")
                        yield f"❌ Задача завершилась с ошибкой: {error_detail}", {"error": error_detail}
                        return
                else:
                    yield f"⚠️ Ошибка опроса статуса: {result_resp.status_code}", {"error": "polling_error"}

            except Exception as e:
                yield f"⚠️ Исключение при опросе: {str(e)}", {"error": str(e)}

        # Таймаут
        yield f"❌ Превышено время ожидания результата ({max_wait} сек)", {"error": "timeout"}

    except Exception as e:
        import traceback
        traceback.print_exc()
        yield f"❌ Исключение: {str(e)}", {"error": str(e)}


# === Gradio Interface ===

with gr.Blocks(title="Анализ закупки", theme=gr.themes.Soft()) as demo:
    gr.Markdown("# 📄 Анализ комплекта документов закупки")

    with gr.Row():
        procurement_id = gr.Textbox(label="ID закупки *", placeholder="PR_12345")
        organization = gr.Textbox(label="Заказчик (организация) *")
        legislation = gr.Dropdown(label="Законодательство *", choices=["44-ФЗ", "223-ФЗ"], value="44-ФЗ")
        procurement_method = gr.Dropdown(
            label="Способ закупки *",
            choices=["Конкурс", "Аукцион", "Запрос котировок", "Закупка у единственного поставщика"],
            value="Конкурс"
        )
        eis_link = gr.Textbox(label="Ссылка на ЕИС (опционально)")

    gr.Markdown("### ➕ Добавить документ")

    with gr.Row():
        available_docs = gr.Dropdown(
            label="Выберите документ",
            choices=DOCUMENT_LABELS,
            value=None,
            interactive=True
        )
        doc_file = gr.File(label="Файл")
        doc_link = gr.Textbox(label="Ссылка на документ")
        doc_comment = gr.Textbox(label="Комментарий")

    with gr.Row():
        add_btn = gr.Button("➕ Добавить")
        clear_btn = gr.Button("🗑️ Очистить список")

    media_json_state = gr.State("")
    current_media_display = gr.JSON(label="Текущий комплект документов")

    add_btn.click(
        add_document_to_list,
        inputs=[media_json_state, available_docs, doc_file, doc_link, doc_comment],
        outputs=[media_json_state, current_media_display]
    )

    clear_btn.click(clear_document_list, outputs=[media_json_state, current_media_display])

    gr.Markdown("---")
    submit_btn = gr.Button("🚀 Запустить анализ", variant="primary")

    with gr.Tabs():
        with gr.Tab("📋 Отчёт"):
            output_text = gr.Markdown(value="Результат появится здесь...")
        with gr.Tab("🔧 JSON"):
            json_output = gr.JSON()

    # Для сбора файлов
    upload_for_api = gr.File(file_count="multiple", visible=False)

    submit_btn.click(
        fn=process_evaluation,
        inputs=[
            procurement_id,
            legislation,
            procurement_method,
            organization,
            eis_link,
            media_json_state,
            upload_for_api
        ],
        outputs=[output_text, json_output]
    )

    # Синхронизируем загруженные файлы
    upload_for_api.change(lambda x: x, upload_for_api, upload_for_api)


if __name__ == "__main__":
    demo.queue()
    demo.launch(
        server_name="0.0.0.0",
        server_port=20141,
        show_error=True
    )