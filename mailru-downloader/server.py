import os
import tempfile
import subprocess
import shutil
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List

app = FastAPI()

class DownloadRequest(BaseModel):
    url: str

@app.post("/download")
async def download_mailru_file(request: DownloadRequest):
    url = request.url.strip()
    if not url.startswith("https://cloud.mail.ru/public/"):
        raise HTTPException(status_code=400, detail="Некорректная ссылка")

    # Очищаем links.txt
    with open("links.txt", "w", encoding="utf-8") as f:
        f.write(url + "\n")

    # Запускаем PHP-скрипт
    try:
        result = subprocess.run(
            ["php/php.exe", "cloud_mail_downloader.php"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd="/app"
        )
        if result.returncode != 0:
            raise Exception(f"PHP error: {result.stderr}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка скачивания: {str(e)}")

    # Ищем скачанный файл в downloads/
    downloaded_files = []
    for root, dirs, files in os.walk("downloads"):
        for file in files:
            downloaded_files.append(os.path.join(root, file))

    if not downloaded_files:
        raise HTTPException(status_code=404, detail="Файл не найден после скачивания")

    # Берём первый файл
    file_path = downloaded_files[0]
    filename = os.path.basename(file_path)

    # Перемещаем файл во временную папку для доступа извне
    temp_dir = "/tmp/mailru_downloads"
    os.makedirs(temp_dir, exist_ok=True)
    new_path = os.path.join(temp_dir, filename)
    shutil.move(file_path, new_path)

    # Очищаем downloads
    shutil.rmtree("downloads")
    os.mkdir("downloads")

    return {
        "status": "success",
        "file_path": new_path,
        "filename": filename
    }