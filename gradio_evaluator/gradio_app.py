import os
import json

import gradio as gr
import requests
from dotenv import load_dotenv
from typing import Dict, Any


load_dotenv()

# Загрузка переменных окружения
API_ACCESS = os.getenv("API_ACCESS")
API_KEY = os.getenv("API_KEY")


def call_evaluation_api(procurement_id: str, files, legislation: str, procurement_method: str, expertise_details: str) -> Dict[Any, Any]:
    """
    Вызывает внешнее API для анализа загруженных документов по закупке.

    Отправляет набор файлов и метаданных (ID закупки, законодательство, способ закупки и др.)
    на сервер обработки через HTTP-запрос. Получает структурированный ответ с результатами
    анализа каждого документа, включая определение типа, соответствие и извлечённые данные.
    Обеспечивает корректное закрытие файловых дескрипторов после отправки.

    Args:
        procurement_id (str): Уникальный идентификатор закупки, используется для привязки документов.
        files (list): Список объектов файлов (например, временных файлов), подготовленных для отправки.
        legislation (str): Нормативный акт, регулирующий закупку (например, "44-ФЗ", "223-ФЗ").
        procurement_method (str): Способ проведения закупки (например, "Конкурс", "Аукцион").
        expertise_details (str): Дополнительная информация о цели экспертизы (может использоваться в будущем).

    Returns:
        Dict[Any, Any]: JSON-ответ от API в виде словаря. Возможные ключи:
            - results (List[dict]): Результаты анализа по каждому документу.
            - errors (List[str]): Ошибки, возникшие при обработке.
            - error (str): Сообщение об общей ошибке, если запрос не удался.
            При успешном выполнении возвращает данные от сервера; при ошибке — соответствующее описание.
    """
    if not procurement_id.strip():
        return {"error": "ID закупки обязателен"}
    
    if not files:
        return {"error": "Не загружено ни одного файла"}

    try:
        # Подготавливаем файлы для отправки
        file_list = [("files", (f.name, open(f.name, "rb"), f"application/octet-stream")) for f in files]
        
        data = {
            "procurement_id": procurement_id,
            "legislation": legislation,
            "procurement_method": procurement_method,
            "expertise_details": expertise_details
        }

        headers = {"X-API-Key": API_KEY}
        
        response = requests.post(
            f"{API_ACCESS}/evaluate-documents",
            files=file_list,
            data=data,
            headers=headers,
            timeout=600
        )

        # Закрываем файлы после отправки
        for _, (_, f_obj, _) in enumerate(file_list):
            f_obj.close()

        if response.status_code == 200:
            return response.json()
        else:
            try:
                error_detail = response.json().get("detail", response.text)
            except:
                error_detail = response.text
            return {"error": f"Ошибка API: {response.status_code} – {error_detail}"}

    except Exception as e:
        return {"error": f"Ошибка соединения: {str(e)}"}


with gr.Blocks(title="Эксперт по закупкам") as demo:
    gr.Markdown("# 📄 Анализ комплекта документов закупки")

    with gr.Row():
        with gr.Column(scale=2):
            procurement_id = gr.Textbox(label="ID закупки", placeholder="Введите уникальный ID закупки")
            legislation = gr.Dropdown(
                label="Законодательство",
                choices=["44-ФЗ", "223-ФЗ"],
                value="44-ФЗ"
            )
            procurement_method = gr.Dropdown(
                label="Способ закупки",
                choices=["Конкурс", "Аукцион", "Запрос котировок", "Закупка у единственного поставщика"],
                value="Конкурс"
            )
            expertise_details = gr.Dropdown(
                label="Тип экспертизы",
                choices=[
                    "Полный комплект документов о закупке",
                    "Описание объекта закупки",
                    "Обоснование начальной (максимальной) цены контракта"
                ],
                value="Полный комплект документов о закупке"
            )
            upload_btn = gr.UploadButton("📤 Загрузить документы", file_count="multiple", file_types=["pdf", "docx", "jpg", "jpeg", "png", "xls", "xlsx"])

        with gr.Column(scale=3):
            output = gr.JSON(label="Результат анализа")

    upload_btn.upload(
        fn=call_evaluation_api,
        inputs=[procurement_id, upload_btn, legislation, procurement_method, expertise_details],
        outputs=output
    )

    # Дополнительно: кнопка очистки
    clear_btn = gr.Button("Очистить")
    clear_btn.click(fn=lambda: (None, None), inputs=None, outputs=[upload_btn, output])

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=20141)