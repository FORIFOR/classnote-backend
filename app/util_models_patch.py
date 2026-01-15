
from enum import Enum
from typing import Optional, List, Any, Dict
from pydantic import BaseModel

class HighlightType(str, Enum):
    important = "important"
    question = "question"
    todo = "todo"

class SummaryRequest(BaseModel):
    summary: str
