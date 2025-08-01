import os

from dotenv import load_dotenv

import yaml


load_dotenv()


def load_config(config_name: str = "config_model") -> dict:
    """
    Загружает конфигурацию из YAML-файла и переопределяет чувствительные данные из переменных окружения.

    Args:
        config_name (str, optional): Имя конфигурационного файла без расширения. По умолчанию "config_model".

    Raises:
        FileNotFoundError: Выбрасывается, если конфигурационный файл не найден.

    Returns:
        dict: Словарь с загруженными конфигурационными данными.
    """
    config_path = os.path.join(os.path.dirname(__file__), f"{config_name}.yaml")
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file {config_path} not found.")
    with open(config_path, "r", encoding="utf-8") as file:
        config_data = yaml.safe_load(file)
    
    # Переопределение данных из .env
    config_data["url"] = os.getenv("API_URL")
    config_data["model_name"] = os.getenv("MODEL_NAME")
    return config_data