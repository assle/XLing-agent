from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Optional

from pydantic import BaseModel, Field, PlainSerializer

from app.core.time import utc_isoformat

UtcDateTime = Annotated[datetime, PlainSerializer(utc_isoformat, return_type=str, when_used="json")]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    sessionId: Optional[str] = None
    noMemory: Optional[bool] = None


class ChatStreamEvent(BaseModel):
    sessionId: Optional[str] = None
    content: Optional[str] = None
    message: Optional[str] = None
    noMemory: Optional[bool] = None
    code: Optional[str] = None
    type: str


class KnowledgeIngestRequest(BaseModel):
    source: str
    content: str


class KnowledgeIngestResponse(BaseModel):
    source: str
    chunks: int


class ReportResponse(BaseModel):
    id: int
    sessionId: str
    username: str
    displayName: str
    content: str
    intent: str
    emotion: str
    emotionScore: float
    riskLevel: str
    confidence: float
    summary: str
    createdAt: UtcDateTime


class ConversationMessageResponse(BaseModel):
    role: str
    content: str
    createdAt: UtcDateTime


class ConversationResponse(BaseModel):
    sessionId: str
    title: str
    noMemory: bool = False
    pendingReview: bool = False
    messages: list[ConversationMessageResponse]


class ToolRecordResponse(BaseModel):
    id: int
    reportId: int
    status: str
    message: str
    createdAt: UtcDateTime
    channel: Optional[str] = None
    recipient: Optional[str] = None
    filePath: Optional[str] = None


class ToolJobResponse(BaseModel):
    id: int
    reportId: int
    kind: str
    status: str
    attempts: int
    maxAttempts: int
    dependsOnJobId: Optional[int] = None
    runAfter: UtcDateTime
    lastError: str
    createdAt: UtcDateTime
    updatedAt: UtcDateTime


class DeadLetterResponse(BaseModel):
    id: int
    jobId: Optional[int] = None
    reportId: int
    kind: str
    reason: str
    payload: str
    createdAt: UtcDateTime


class AiMessage(BaseModel):
    role: str
    content: str


def authority(role: str) -> dict[str, Any]:
    """将一个角色名称包装成接口约定的权限对象。

    role 原样放入 authority 字段，不在此验证角色或授予权限。
    """
    return {"authority": role}


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class UpdateSupportProfileRequest(BaseModel):
    currentConcern: Optional[str] = Field(default=None, max_length=1000)
    supportGoal: Optional[str] = Field(default=None, max_length=1000)
    preferredSupportStyle: Optional[str] = Field(default=None, max_length=32)


class SupportProfileResponse(BaseModel):
    currentConcern: Optional[str] = None
    supportGoal: Optional[str] = None
    preferredSupportStyle: Optional[str] = None


class CreateMemoryCardRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class UpdateMemoryCardRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class ReplaceActionItemRequest(BaseModel):
    content: str = Field(min_length=1, max_length=500)


class ScreeningSubmitRequest(BaseModel):
    answers: list[int] = Field(min_length=1)
    triggerSource: Optional[str] = "voluntary"


class CheckInSubmitRequest(BaseModel):
    planId: int
    improvementStatus: str
    notes: Optional[str] = ""
    itemStates: Optional[list[dict]] = None


class ReviewDecisionRequest(BaseModel):
    decision: str = Field(min_length=1)
    note: Optional[str] = ""
    referralTarget: Optional[str] = Field(default=None, max_length=200)
    nextStep: Optional[str] = Field(default=None, max_length=500)
    followUpOwner: Optional[str] = Field(default=None, max_length=128)
    followUpAt: Optional[datetime] = None
