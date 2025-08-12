import os
import json
import gradio as gr
import logging
import tempfile
import time
from dotenv import load_dotenv
import requests
from procurement_requirements import PROCUREMENT_REQUIREMENTS
from typing import List, Dict
import shutil

from utils import (get_required_documents, show_documents_content, update_document_list, add_document, clear_documents_list, toggle_explanation_visibility)

load_dotenv()


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


with gr.Blocks(title="Проверка документов закупок") as demo:
    gr.Markdown("## Проверка документов закупок")
    
    with gr.Row():
        with gr.Column():
            procurement_id = gr.Textbox(
                label="ID закупки*",
                placeholder="Введите идентификатор закупки",
                interactive=True
            )
            
            expertise_object = gr.Dropdown(
                label="Объект экспертизы*",
                choices=[
                    "Документация о проведении закупки",
                    "Отчетные материалы (результаты исполнения Контракта или Договора)"
                ],
                value="Документация о проведении закупки"
            )
            
            legislation = gr.Dropdown(
                label="Законодательное регулирование*",
                choices=["44-ФЗ", "223-ФЗ"],
                value="44-ФЗ"
            )
            
            procurement_method = gr.Dropdown(
                label="Способ закупки*",
                choices=[
                    "Конкурс", "Аукцион", "Запрос котировок", 
                    "Закупка у единственного поставщика", "Выполнение НИР",
                    "Поставка товара", "Оказание услуг или выполнение работ"
                ],
                value="Конкурс"
            )
            
            expertise_details = gr.Dropdown(
                label="Детали экспертизы*",
                choices=[
                    "Полный комплект документов о закупке",
                    "Описание объекта закупки",
                    "Обоснование начальной (максимальной) цены контракта",
                    "Порядок оценки заявок участников закупки",
                    "Приемка по Контракту еще не проводилась",
                    "Приемка по Контракту завершена"
                ],
                value="Полный комплект документов о закупке"
            )
            
            eis_link = gr.Textbox(label="Ссылка на ЕИС (опционально)")
            
            # Блок для работы с документами
            with gr.Group():
                gr.Markdown("### Документы*")
                with gr.Row():
                    doc_name = gr.Dropdown(
                        label="Тип документа*",
                        choices=[],
                        interactive=True,
                        allow_custom_value=False
                    )
                    doc_explanation = gr.Textbox(
                        label="Комментарий (если документ отсутствует)*",
                        placeholder="Укажите причину отсутствия документа...",
                        interactive=True,
                        visible=False
                    )
                with gr.Row():
                    doc_file = gr.File(
                        label="Загрузить документ",
                        file_count="single",
                        file_types=[".pdf", ".docx", ".doc", ".xlsx", ".xls", ".txt", ".csv", ".zip", ".rar"],
                        interactive=True
                    )
                with gr.Row():
                    add_doc_btn = gr.Button("Добавить документ", variant="primary")
                    clear_docs_btn = gr.Button("Очистить список")
                docs_json = gr.Textbox(visible=False)
                docs_display = gr.JSON(
                    label="Текущие документы",
                    interactive=False
                )
            
            # Массовая загрузка файлов
            mass_files = gr.File(
                label="Массовая загрузка документов (автоматическое определение типа)",
                file_count="multiple",
                file_types=[".pdf", ".docx", ".doc", ".xlsx", ".xls", ".txt", ".csv", ".zip", ".rar"]
            )
            
            submit_btn = gr.Button("Проверить документы", variant="primary")
        
        with gr.Column():
            # Результаты
            results_output = gr.JSON(label="Результаты проверки")
            
            # Детализированный вывод
            with gr.Tabs():
                with gr.Tab("Статус проверки"):
                    status_output = gr.JSON(label="Статус проверки")
                
                with gr.Tab("Требуемые документы"):
                    required_docs = gr.JSON(label="Список обязательных документов")
                
                with gr.Tab("Предоставленные документы"):
                    provided_docs = gr.JSON(label="Список загруженных документов")
                
                with gr.Tab("Отсутствующие документы"):
                    missing_docs = gr.JSON(label="Список отсутствующих документов")
                
                with gr.Tab("Содержимое документов"):
                    docs_content = gr.JSON(label="Первые 20 символов каждого документа")
                
                with gr.Tab("Ошибки"):
                    errors_output = gr.JSON(label="Ошибки обработки")

    # Инициализация списка документов при загрузке
    demo.load(
        fn=lambda: update_document_list("44-ФЗ", "Конкурс", "Полный комплект документов о закупке"),
        outputs=doc_name
    )
    
    # Обновление списка документов при изменении параметров
    for component in [legislation, procurement_method, expertise_details]:
        component.change(
            update_document_list,
            [legislation, procurement_method, expertise_details],
            doc_name
        )
    
    # Переключение видимости поля объяснения
    doc_name.change(
        toggle_explanation_visibility,
        inputs=doc_name,
        outputs=doc_explanation
    )
    
    # Добавление документа
    add_doc_btn.click(
        add_document,
        [docs_json, doc_name, doc_explanation, doc_file],
        [docs_json, docs_display]
    ).then(
        lambda: (None, "", None),  # Очищаем поля после добавления
        [],
        [doc_name, doc_explanation, doc_file]
    )
    
    # Очистка списка документов
    clear_docs_btn.click(
        clear_documents_list,
        [],
        [docs_json, docs_display]
    )
    
    # Основная проверка документов
    submit_btn.click(
        show_documents_content,
        [
            procurement_id,
            expertise_object,
            legislation,
            procurement_method,
            expertise_details,
            eis_link,
            mass_files,
            docs_json
        ],
        results_output
    ).then(
        lambda x: {
            "status": x.get("status", "deny"),
            "message": "Проверка завершена успешно" if x.get("status") == "allow" else "Обнаружены проблемы",
            "error": x.get("error")
        },
        results_output,
        status_output
    ).then(
        lambda x: x.get("required_documents", []),
        results_output,
        required_docs
    ).then(
        lambda x: x.get("provided_documents", []),
        results_output,
        provided_docs
    ).then(
        lambda x: x.get("missing_documents", []),
        results_output,
        missing_docs
    ).then(
        lambda x: {k: v[:20] for k, v in x.get("documents_content", {}).items()},
        results_output,
        docs_content
    ).then(
        lambda x: x.get("errors", []),
        results_output,
        errors_output
    )


if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=20141)