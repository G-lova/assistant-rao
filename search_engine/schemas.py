from pydantic import BaseModel


class Document(BaseModel):
    name: str
    legislation: str
    text: str


class SearchResult(BaseModel):
    name: str
    legislation: str
    text_snippet: str
    score: float