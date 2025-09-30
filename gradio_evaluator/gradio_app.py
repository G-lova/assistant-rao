import os
import json

import gradio as gr
import requests
from dotenv import load_dotenv
from typing import Dict, Any, List


load_dotenv()


# Загрузка переменных окружения
API_ACCESS = os.getenv("API_ACCESS")
API_KEY = os.getenv("API_KEY")

# Данные для аутентификации
#AUTH_USERNAME = os.getenv("GRADIO_USERNAME")  # Логин по умолчанию
#AUTH_PASSWORD = os.getenv("GRADIO_PASSWORD")  # Пароль по умолчанию


#def authenticate(username: str, password: str) -> bool:
#    """Проверяет правильность логина и пароля"""
#    return username == AUTH_USERNAME and password == AUTH_PASSWORD


def call_evaluation_api(procurement_id: str, files, legislation: str, procurement_method: str, expertise_details: str) -> Dict[Any, Any]:
    """
    Вызывает внешнее API для анализа загруженных документов по закупке.
    """
    if not procurement_id.strip():
        return {"error": "ID закупки обязателен"}
    
    if not files:
        return {"error": "Не загружено ни одного файла"}

    try:
        # Правильно подготавливаем файлы для отправки
        file_list = []
        for f in files:
            if hasattr(f, 'name') and os.path.exists(f.name):
                # Получаем MIME-type на основе расширения файла
                filename = os.path.basename(f.name)
                file_extension = os.path.splitext(filename)[1].lower()
                
                # Определяем content_type на основе расширения
                content_type = "application/octet-stream"  # по умолчанию
                if file_extension in ['.pdf']:
                    content_type = 'application/pdf'
                elif file_extension in ['.doc', '.docx']:
                    content_type = 'application/msword'
                elif file_extension in ['.xls', '.xlsx']:
                    content_type = 'application/vnd.ms-excel'
                elif file_extension in ['.jpg', '.jpeg']:
                    content_type = 'image/jpeg'
                elif file_extension == '.png':
                    content_type = 'image/png'
                elif file_extension == '.txt':
                    content_type = 'text/plain'
                
                file_list.append(("files", (filename, open(f.name, "rb"), content_type)))
        
        if not file_list:
            return {"error": "Нет валидных файлов для отправки"}
        
        data = {
            "procurement_id": procurement_id,
            "legislation": legislation,
            "procurement_method": procurement_method,
            "expertise_details": expertise_details
        }

        headers = {"X-API-Key": API_KEY}
        
        print(f"🔍 Отправка запроса на {API_ACCESS}/evaluate-documents")
        print(f"🔍 Количество файлов: {len(file_list)}")
        print(f"🔍 Данные: {data}")

        response = requests.post(
            f"{API_ACCESS}/evaluate-documents",
            files=file_list,
            data=data,
            headers=headers,
            timeout=360
        )

        # Закрываем файлы после отправки
        for _, file_tuple in file_list:
            filename, file_obj, content_type = file_tuple
            file_obj.close()

        print(f"🔍 Статус ответа: {response.status_code}")

        if response.status_code == 200:
            try:
                return response.json()
            except json.JSONDecodeError as e:
                print(f"🔍 Ошибка парсинга JSON: {e}")
                print(f"🔍 Ответ сервера: {response.text[:500]}")
                return {"error": f"Ошибка парсинга JSON ответа: {str(e)}"}
        else:
            try:
                error_detail = response.json().get("detail", response.text)
            except:
                error_detail = response.text
            print(f"🔍 Ошибка API: {response.status_code} - {error_detail}")
            return {"error": f"Ошибка API: {response.status_code} – {error_detail}"}

    except Exception as e:
        print(f"🔍 Исключение при вызове API: {str(e)}")
        import traceback
        traceback.print_exc()
        return {"error": f"Ошибка соединения: {str(e)}"}


def format_documents_output(api_response: Dict) -> str:
    """
    Форматирует вывод документов для красивого отображения в Gradio.
    """
    if "error" in api_response:
        return f"❌ Ошибка: {api_response['error']}"
    
    output_parts = []
    
    # Заголовок
    output_parts.append(f"## 📋 Результаты анализа закупки: {api_response.get('procurement_id', 'N/A')}")
    output_parts.append("")
    
    # Раздел по документам
    output_parts.append("### 📄 Анализ документов")
    documents = api_response.get("documents", [])
    
    if not documents:
        output_parts.append("Нет данных о документах")
    else:
        for i, doc in enumerate(documents, 1):
            status_emoji = "✅" if doc.get("is_valid") else "❌"
            output_parts.append(f"**{i}. {doc.get('filename', 'N/A')}** {status_emoji}")
            output_parts.append(f"   - **Тип:** {doc.get('document_type', 'N/A')}")
            output_parts.append(f"   - **Статус:** {'Валиден' if doc.get('is_valid') else 'Невалиден'}")
            
            if doc.get("error"):
                output_parts.append(f"   - **Ошибка:** {doc['error']}")
            
            conclusion = doc.get('conclusion', 'N/A')
            # Обрезаем длинное заключение
            if len(conclusion) > 300:
                conclusion = conclusion[:300] + "..."
            output_parts.append(f"   - **Заключение:** {conclusion}")
            output_parts.append("")
    
    # Общее заключение
    overall_conclusion = api_response.get("overall_conclusion", "")
    if overall_conclusion:
        output_parts.append("### 📊 Общее заключение")
        # Добавляем эмодзи для лучшей визуализации
        overall_conclusion = overall_conclusion.replace("Обработано документов:", "✅ Обработано документов:")
        overall_conclusion = overall_conclusion.replace("Комплект документов полный", "✅ Комплект документов полный")
        overall_conclusion = overall_conclusion.replace("Данные согласованы", "✅ Данные согласованы")
        overall_conclusion = overall_conclusion.replace("ИТОГ:", "\n\n📊 ИТОГ:")
        output_parts.append(overall_conclusion)
        output_parts.append("")
    
    # Сводка по комплектности
    completeness = api_response.get("completeness_summary", {})
    if completeness:
        output_parts.append("### 📦 Проверка комплектности")
        status_emoji = "✅" if completeness.get("status") == "allow" else "❌"
        conclusion = completeness.get('conclusion', 'N/A')
        conclusion = conclusion.replace("Комплект документов полный", "✅ Комплект документов полный")
        conclusion = conclusion.replace("Комплект документов неполный", "❌ Комплект документов неполный")
        output_parts.append(f"{status_emoji} **Статус:** {conclusion}")
        
        missing_docs = completeness.get("missing_documents", [])
        if missing_docs:
            output_parts.append("**Отсутствующие документы:**")
            for doc in missing_docs:
                output_parts.append(f"   - {doc}")
        else:
            output_parts.append("**Все необходимые документы присутствуют**")
        output_parts.append("")
    
    # Сводка по согласованности
    consistency = api_response.get("consistency_summary", {})
    if consistency:
        output_parts.append("### 🔍 Проверка согласованности")
        status_emoji = "✅" if consistency.get("status") == "ok" else "⚠️"
        conclusion = consistency.get('conclusion', 'N/A')
        conclusion = conclusion.replace("Данные в документах согласованы", "✅ Данные в документах согласованы")
        conclusion = conclusion.replace("Обнаружены расхождения", "❌ Обнаружены расхождения")
        output_parts.append(f"{status_emoji} **Статус:** {conclusion}")
        
        issues = consistency.get("issues", [])
        if issues:
            output_parts.append("**Обнаруженные расхождения:**")
            for issue in issues:
                if isinstance(issue, dict):
                    output_parts.append(f"   - **{issue.get('field', 'N/A')}:** {issue.get('details', 'N/A')}")
                else:
                    output_parts.append(f"   - {issue}")
        else:
            output_parts.append("**Расхождения не обнаружены**")
    
    return "\n".join(output_parts)


def safe_json_output(api_response: Dict) -> Dict:
    """
    Безопасно подготавливает данные для JSON вывода.
    """
    if "error" in api_response:
        return {"error": api_response["error"]}
    
    try:
        # Просто возвращаем оригинальный ответ, так как он уже валидный JSON
        return api_response
        
    except Exception as e:
        return {"error": f"Ошибка подготовки JSON: {str(e)}"}


def process_evaluation(procurement_id: str, files, legislation: str, procurement_method: str, expertise_details: str):
    """
    Основная функция обработки для Gradio интерфейса.
    """
    if not procurement_id.strip():
        return "❌ Введите ID закупки", {"message": "Введите ID закупки"}
    
    if not files:
        return "❌ Загрузите хотя бы один документ", {"message": "Загрузите документы"}
    
    # Вызываем API
    api_response = call_evaluation_api(procurement_id, files, legislation, procurement_method, expertise_details)
    
    # Проверяем на ошибки
    if "error" in api_response:
        error_msg = f"❌ Ошибка: {api_response['error']}"
        return error_msg, {"error": api_response["error"]}
    
    # Форматируем вывод
    formatted_output = format_documents_output(api_response)
    json_output = safe_json_output(api_response)
    
    return formatted_output, json_output


def clear_all():
    """
    Очищает все поля интерфейса.
    """
    return (
        "",  # procurement_id
        "44-ФЗ",  # legislation
        "Конкурс",  # procurement_method
        "Полный комплект документов о закупке",  # expertise_details
        "## 📋 Результаты анализа\n\nЗагрузите документы для анализа...",  # output_text
        {"message": "Готов к анализу"}  # json_output
    )


#def login(username: str, password: str):
#    """Обработчик входа в систему"""
#    if authenticate(username, password):
#        return gr.update(visible=True), gr.update(visible=False), ""
#    else:
#        return gr.update(visible=False), gr.update(visible=True), "❌ Неверный логин или пароль"


def logout():
    """Обработчик выхода из системы"""
    return gr.update(visible=False), gr.update(visible=True), ""


# Создаем интерфейс Gradio
with gr.Blocks(title="Эксперт по закупкам", theme=gr.themes.Soft()) as demo:
    gr.Markdown("# 📄 Анализ комплекта документов закупки")
    gr.Markdown("Загрузите документы закупки для автоматического анализа комплектности и согласованности.")

    with gr.Row():
        with gr.Column(scale=1):
            gr.Markdown("### ⚙️ Параметры закупки")

            procurement_id = gr.Textbox(
                label="ID закупки *",
                placeholder="Введите уникальный ID закупки...",
                value="PR_12345"
            )

            legislation = gr.Dropdown(
                label="Законодательство *",
                choices=["44-ФЗ", "223-ФЗ"],
                value="44-ФЗ"
            )

            procurement_method = gr.Dropdown(
                label="Способ закупки *",
                choices=["Конкурс", "Аукцион", "Запрос котировок", "Закупка у единственного поставщика"],
                value="Конкурс"
            )

            expertise_details = gr.Dropdown(
                label="Тип экспертизы *",
                choices=[
                    "Полный комплект документов о закупке",
                    "Описание объекта закупки",
                    "Обоснование начальной (максимальной) цены контракта"
                ],
                value="Полный комплект документов о закупке"
            )

            upload_btn = gr.UploadButton(
                "📤 Загрузить документы",
                file_count="multiple",
                file_types=[".pdf", ".docx", ".doc", ".xls", ".xlsx", ".jpg", ".jpeg", ".png"]
            )

            with gr.Row():
                submit_btn = gr.Button("🚀 Начать анализ", variant="primary")
                clear_btn = gr.Button("🧹 Очистить", variant="secondary")

        with gr.Column(scale=2):
            gr.Markdown("### 📊 Результаты анализа")

            with gr.Tab("📋 Форматированный отчет"):
                output_text = gr.Markdown(
                    value="## 📋 Результаты анализа\n\nЗагрузите документы для анализа..."
                )

            with gr.Tab("🔧 Сырые данные (JSON)"):
                json_output = gr.JSON(
                    value={"message": "Результаты появятся здесь после анализа..."}
                )

    # Информационный блок
    gr.Markdown("---")
    with gr.Accordion("ℹ️ Информация о системе", open=False):
        gr.Markdown("""
        **Возможности системы:**

        - ✅ Автоматическое определение типов документов
        - ✅ Проверка комплектности документов
        - ✅ Анализ согласованности данных между документами
        - ✅ Формирование детального заключения по каждому документу
        - ✅ Общая оценка комплекта документов

        **Поддерживаемые форматы:** PDF, DOCX, DOC, XLS, XLSX, JPG, JPEG, PNG

        **Пример ID закупки:** PR_12345
        """)

    # Обработчики событий
    submit_btn.click(
        fn=process_evaluation,
        inputs=[procurement_id, upload_btn, legislation, procurement_method, expertise_details],
        outputs=[output_text, json_output]
    )

    clear_btn.click(
        fn=clear_all,
        inputs=[],
        outputs=[procurement_id, legislation, procurement_method, expertise_details, output_text, json_output]
    )


if __name__ == "__main__":
    print("🚀 Запуск Gradio интерфейса на порту 20141...")
    print(f"🔍 API endpoint: {API_ACCESS}")
    print(f"🔍 API key: {'установлен' if API_KEY else 'отсутствует'}")
    
#    # Проверка наличия учётных данных
#    if not AUTH_USERNAME or not AUTH_PASSWORD:
#        raise ValueError("❌ Переменные GRADIO_USERNAME и GRADIO_PASSWORD должны быть заданы в .env")
#
#    print(f"🔐 Требуется аутентификация: {AUTH_USERNAME} / [пароль скрыт]")

    # Включаем встроенную аутентификацию Gradio
    demo.launch(
        server_name="0.0.0.0",
        server_port=20141,
        share=False,
        show_error=True,
#        auth=(AUTH_USERNAME, AUTH_PASSWORD)
    )