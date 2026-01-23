import os
from functools import lru_cache
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# class Config:
#     """Base configuration."""
#     DB_USER: str = "admin"
#     DB_PASSWORD: str = "password"
#     DB_HOST: str = "localhost"
#     DB_PORT: str = "5432"
#     DB_NAME: str = "mydb"

#     @property
#     def DATABASE_URL(self):
#         # Default construction
#         return f"postgresql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

class LocalConfig():
    """Local configuration."""
    def __init__(self):
        self.DB_USER = os.getenv("DB_USER")
        self.DB_PASSWORD = os.getenv("DB_PASSWORD")
        # Support both naming conventions for now
        self.DB_HOST = os.getenv("DB_HOST")
        self.DB_PORT = os.getenv("DB_PORT")
        self.DB_NAME = os.getenv("DB_NAME")
        
        self._database_url = f"postgresql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        self.AI_MODEL = os.getenv("AI_MODEL", "nemotron-3-nano:30b-cloud")

    @property
    def DATABASE_URL(self):
        if self._database_url:
            return self._database_url
        return super().DATABASE_URL

class ProductionConfig():
    """Production configuration."""
    def __init__(self):
        # specific production env vars can be used here
        self.DB_USER = os.getenv("PROD_DB_USER")
        self.DB_PASSWORD = os.getenv("PROD_DB_PASSWORD")
        self.DB_HOST = os.getenv("PROD_DB_HOST")
        self.DB_PORT = os.getenv("PROD_DB_PORT")
        self.DB_NAME = os.getenv("PROD_DB_NAME")
        
        self._database_url = f"postgresql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        self.AI_MODEL = os.getenv("PROD_AI_MODEL", "qwen2.5:32b-instruct")

    @property
    def DATABASE_URL(self):
        if self._database_url:
            return self._database_url
        return super().DATABASE_URL

@lru_cache()
def get_settings():
    env = os.getenv("APP_ENV", "local")
    if env == "production":
        return ProductionConfig()
    return LocalConfig()

settings = get_settings()
