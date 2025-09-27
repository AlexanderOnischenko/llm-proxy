# src/models/llm.py
from pydantic import BaseModel
from typing import List, Dict, Any

class LLMRequest(BaseModel):
    method: str
    args: List[Any] = []
    kwargs: Dict[str, Any] = {}
    sender: str = "unknown"