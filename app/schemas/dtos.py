from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    sessionId: Optional[str] = None
    noMemory: Optional[bool] = None


class ChatStreamEvent(BaseModel):
    sessionId: Optional[str] = None
    content: Optional[str] = None
    message: Optional[str] = None
    noMemory: Optional[bool] = None
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
    createdAt: datetime


class ConversationMessageResponse(BaseModel):
    role: str
    content: str
    createdAt: datetime


class ConversationResponse(BaseModel):
    sessionId: str
    title: str
    noMemory: bool = False
    messages: list[ConversationMessageResponse]


class ToolRecordResponse(BaseModel):
    id: int
    reportId: int
    status: str
    message: str
    createdAt: datetime
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
    runAfter: datetime
    lastError: str
    createdAt: datetime
    updatedAt: datetime


class DeadLetterResponse(BaseModel):
    id: int
    jobId: Optional[int] = None
    reportId: int
    kind: str
    reason: str
    payload: str
    createdAt: datetime


class AiMessage(BaseModel):
    role: str
    content: str


def authority(role: str) -> dict[str, Any]:
    return {"authority": role}


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class ResetPasswordRequest(BaseModel):
    username: str = Field(min_length=1)
    oldPassword: str = Field(min_length=1)
    newPassword: str = Field(min_length=6)


class UpdateUserProfileRequest(BaseModel):
    examStage: Optional[str] = None
    targetExam: Optional[str] = None
    examDate: Optional[str] = None


class UserProfileResponse(BaseModel):
    examStage: Optional[str] = None
    targetExam: Optional[str] = None
    examDate: Optional[str] = None


class CreateMemoryCardRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class UpdateMemoryCardRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


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
