from pydantic import BaseModel
from typing import List, Optional

class CrawlRequest(BaseModel):
    url: str
    max_pages: int = 20

class SearchRequest(BaseModel):
    url: str

class ChatRequest(BaseModel):
    message: str
    site: str
    history: List[dict] = []
