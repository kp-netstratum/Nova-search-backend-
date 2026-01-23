import os
from functools import lru_cache
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class LocalConfig():
    """Local configuration."""
    def __init__(self):
        self.DB_USER = os.getenv("DB_USER")
        self.DB_PASSWORD = os.getenv("DB_PASSWORD")
        self.DB_HOST = os.getenv("DB_HOST")
        self.DB_PORT = os.getenv("DB_PORT")
        self.DB_NAME = os.getenv("DB_NAME")
        
        self._database_url = f"postgresql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        self.AI_MODEL = os.getenv("AI_MODEL", "nemotron-3-nano:30b-cloud")

    @property
    def DATABASE_URL(self):
        return self._database_url

class ProductionConfig():
    """Production configuration."""
    def __init__(self):
        self.DB_USER = os.getenv("PROD_DB_USER")
        self.DB_PASSWORD = os.getenv("PROD_DB_PASSWORD")
        self.DB_HOST = os.getenv("PROD_DB_HOST")
        self.DB_PORT = os.getenv("PROD_DB_PORT")
        self.DB_NAME = os.getenv("PROD_DB_NAME")
        
        self._database_url = f"postgresql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        self.AI_MODEL = os.getenv("PROD_AI_MODEL", "qwen2.5:32b-instruct")

    @property
    def DATABASE_URL(self):
        return self._database_url

@lru_cache()
def get_settings():
    env = os.getenv("APP_ENV", "local")
    if env == "production":
        return ProductionConfig()
    return LocalConfig()

settings = get_settings()
