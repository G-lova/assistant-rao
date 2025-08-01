import os
import gradio as gr
from dotenv import load_dotenv

load_dotenv()

def check_documents(files: list) -> dict:
    """
    Функция в разработке
    """
    if not files:
        return {"error": "Необходимо загрузить документы"}
    
    try:
        # Здесь будет реальный запрос к API
        # Сейчас возвращаем заглушку
        results = []
        for file in files:
            results.append({
                "document": file.name,
                "status": "Проверено",
                "issues": ["Пример проблемы: не хватает подписи"],
                "recommendations": ["Добавить подпись ответственного лица"]
            })
        return {"results": results}
    except Exception as e:
        return {"error": str(e)}

with gr.Blocks(title="Проверка документов закупок") as demo:
    gr.Markdown("## Система проверки документов закупок")
    
    with gr.Row():
        with gr.Column():
            file_input = gr.File(
                label="Загрузите документы (PDF, DOCX, XLSX)",
                file_count="multiple",
                file_types=[".pdf", ".docx", ".xlsx"]
            )
            submit_btn = gr.Button("Проверить документы", variant="primary")
        
        with gr.Column():
            results_output = gr.JSON(
                label="Результаты проверки",
                interactive=False
            )
    
    submit_btn.click(
        fn=check_documents,
        inputs=file_input,
        outputs=results_output
    )

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=20141)