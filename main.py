import os
import shutil
import tempfile
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import List
from configs.schemas import DocumentContentResponse, APIError
from configs.utils import APIKeyMiddleware, read_file

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(APIKeyMiddleware)

@app.post(
    "/get-documents-content",
    response_model=List[DocumentContentResponse],
    responses={400: {"model": APIError}, 500: {"model": APIError}}
)
async def get_documents_content(
    files: List[UploadFile] = File(...)
):
    """
    Функция в разработке
    """
    if not files:
        raise HTTPException(
            status_code=400,
            detail="Необходимо загрузить хотя бы один документ"
        )

    results = []
    temp_dir = tempfile.mkdtemp()
    
    try:
        for file in files:
            file_path = os.path.join(temp_dir, file.filename)
            try:
                # Сохраняем файл для обработки
                with open(file_path, "wb") as buffer:
                    buffer.write(await file.read())
                
                # Читаем содержимое файла с помощью utils.read_file()
                content = read_file(file_path)
                
                # Получаем метаинформацию
                file_size = os.path.getsize(file_path)
                content_type = file.content_type or "unknown"
                
                results.append(DocumentContentResponse(
                    filename=file.filename,
                    content_type=content_type,
                    content=content,
                    size=file_size,
                    is_valid=True
                ))
                
            except Exception as e:
                results.append(DocumentContentResponse(
                    filename=file.filename,
                    content_type=file.content_type or "unknown",
                    content="",
                    size=0,
                    is_valid=False,
                    error=f"Ошибка при обработке файла: {str(e)}"
                ))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    return results

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=20142)