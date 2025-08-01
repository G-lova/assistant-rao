import os
import tempfile

import nbformat
from nbformat.reader import NotJSONError


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