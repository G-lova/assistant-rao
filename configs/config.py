import os

from dotenv import load_dotenv


load_dotenv()


class Config:
    """
    Класс для хранения и управления конфигурационными параметрами приложения.

    Содержит настройки модели, API и безопасности, загружаемые из переменных окружения.
    Предоставляет метод для получения конфигурации генерации модели.
    """
    # Model settings
    TEMPERATURE = float(os.getenv("MODEL_TEMPERATURE"))
    MAX_NEW_TOKENS = int(os.getenv("MODEL_MAX_TOKENS"))
    
    # API settings
    URL = os.getenv("API_URL")
    MODEL_NAME = os.getenv("MODEL_NAME")
    
    # Security
    API_KEY = os.getenv("API_KEY")
    API_KEY_HASH = os.getenv("API_KEY_HASH")
    
    @classmethod
    def get_model_config(cls):
        """
        Возвращает конфигурацию параметров генерации для языковой модели.

        Returns:
            dict: Словарь с ключами:
                - temperature: параметр креативности модели;
                - max_new_tokens: максимальное количество генерируемых токенов.
        """
        return {
            "temperature": cls.TEMPERATURE,
            "max_new_tokens": cls.MAX_NEW_TOKENS
        }