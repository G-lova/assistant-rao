import os
from dotenv import load_dotenv

load_dotenv()

class Config:
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
        return {
            "temperature": cls.TEMPERATURE,
            "max_new_tokens": cls.MAX_NEW_TOKENS
        }