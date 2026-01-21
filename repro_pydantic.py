
from typing import Optional, List, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field

# Mocking SessionResponse/SessionDetailResponse structure
class SessionResponse(BaseModel):
    id: str
    clientSessionId: Optional[str] = None
    source: Optional[str] = None
    title: str
    mode: str
    userId: str
    status: str
    createdAt: datetime
    tags: Optional[List[str]] = None
    isOwner: Optional[bool] = None
    sharedWithCount: Optional[int] = None
    sharedUserIds: Optional[List[str]] = []
    reactionCounts: Optional[Dict[str, int]] = {}
    canManage: Optional[bool] = None
    ownerUserId: Optional[str] = None
    ownerId: Optional[str] = None
    participantUserIds: List[str] = []
    participants: Optional[dict] = None
    visibility: str = "private"
    autoTags: List[str] = []
    topicSummary: Optional[str] = None
    summaryStatus: Optional[str] = "not_started"
    quizStatus: Optional[str] = "not_started"
    diarizationStatus: Optional[str] = "not_started"
    highlightsStatus: Optional[str] = "not_started"
    isArchived: bool = False
    lastOpenedAt: Optional[datetime] = None
    startedAt: Optional[datetime] = None
    endedAt: Optional[datetime] = None
    durationSec: Optional[float] = None
    hasTranscript: bool = False
    cloudTicket: Optional[str] = None
    cloudAllowedUntil: Optional[datetime] = None
    cloudStatus: Optional[str] = None

class SessionDetailResponse(SessionResponse):
    transcriptText: Optional[str] = None
    notes: Optional[str] = None
    assets: Optional[Any] = None

current_user_uid = "H2oQZPuK9EhnA9NUr6QqESNP6sa2"
data = {
    "id": "151e8c26-f439-4076-bfd8-cb2cf5b6a5b8",
    "title": "Test Session",
    "mode": "on_device",
    "userId": current_user_uid,
    "status": "active",
    "createdAt": datetime.now(),
}

# Logic from sessions.py
owner_id = data.get("ownerUserId") or data.get("ownerUid") or data.get("userId")
data["ownerUserId"] = owner_id
is_owner = (owner_id == current_user_uid)
data["isOwner"] = is_owner
data["canManage"] = is_owner
data["ownerId"] = owner_id

print(f"Data before model: ownerId={data.get('ownerId')}, isOwner={data.get('isOwner')}")

# Try creating the model
try:
    model = SessionDetailResponse(**data)
    print("Model created successfully")
    print(f"Model dump: {model.model_dump()}")
    json_out = model.model_dump_json()
    print(f"JSON Output: {json_out}")
except Exception as e:
    print(f"Model creation failed: {e}")
