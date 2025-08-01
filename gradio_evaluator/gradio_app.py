import os
import requests
import gradio as gr
from dotenv import load_dotenv

load_dotenv()

def show_documents_content(files: list) -> dict:
    """
    Отображает содержимое загруженных документов через API.
    Функция отправляет файлы на API-эндпоинт для получения их содержимого,
    обрабатывает ответ и форматирует его для удобного отображения.

    Args:
        files (list): Список файловых объектов, полученных через интерфейс загрузки.
                      Каждый элемент должен иметь атрибут name с именем файла.
                      Поддерживаются файлы: PDF, DOCX, XLSX, TXT, CSV, DOC, PPTX.

    Returns:
        dict: Словарь с результатами обработки документов, содержащий:

    Raises:
        Неявно обрабатывает все исключения, возвращая их в виде сообщения об ошибке.
    """
    if not files:
        return {"error": "Необходимо загрузить документы"}
    
    try:
        api_url = os.getenv("API_ACCESS")
        files_data = [("files", open(file.name, "rb")) for file in files]
        
        response = requests.post(
            f"{api_url}/get-documents-content",
            files=files_data
        )
        response.raise_for_status()
        
        api_results = response.json()
        
        # Форматируем результаты для отображения
        formatted_results = []
        for result in api_results:
            if result.get("is_valid", False):
                formatted_results.append({
                    "filename": result["filename"],
                    "content": result["content"][:5000] + "..." if len(result["content"]) > 5000 else result["content"],
                    "size": f"{result['size']} bytes",
                    "status": "success"
                })
            else:
                formatted_results.append({
                    "filename": result["filename"],
                    "error": result.get("error", "Unknown error"),
                    "status": "error"
                })
        
        return {"documents": formatted_results}
    except Exception as e:
        return {"error": f"Ошибка при обращении к API: {str(e)}"}

with gr.Blocks(title="Просмотр документов закупок") as demo:
    gr.Markdown("## Просмотр содержимого документов закупок")
    
    with gr.Row():
        with gr.Column():
            file_input = gr.File(
                label="Загрузите документы",
                file_count="multiple",
                file_types=[".pdf", ".docx", ".xlsx", ".txt", ".csv", ".doc", ".pptx"]
            )
            submit_btn = gr.Button("Показать содержимое", variant="primary")
        
        with gr.Column():
            results_output = gr.JSON(
                label="Содержимое документов",
                interactive=False
            )
    
    submit_btn.click(
        fn=show_documents_content,
        inputs=file_input,
        outputs=results_output
    )

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=20141)