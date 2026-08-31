from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import current_user
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import (
    ChatRequest,
    CheckInSubmitRequest,
    ReplaceActionItemRequest,
)
from app.services.action_plan import ActionPlanService
from app.services.chat import ChatService
from app.services.checkin import CheckInService
from app.services.escalation import EscalationService
from app.services.report import ReportService
from app.services.review import ReviewService

router = APIRouter()


@router.post("/api/chat/stream")
async def chat_stream(
    request: ChatRequest,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    if "ROLE_ADMIN" in user.roles:
        raise HTTPException(403, "管理员账号只能查看后台记录，不能发起学生对话。")
    return StreamingResponse(
        ChatService(db, get_settings()).stream_chat(user, request),
        media_type="text/event-stream",
    )


@router.get("/api/sessions")
def list_sessions(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    return ReportService(db).list_sessions(user.id)


@router.get("/api/sessions/{session_id}")
def get_session(
    session_id: str,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    conversation = ReportService(db).conversation_for_user(user.id, session_id)
    if conversation is None:
        raise HTTPException(404, "Session not found")
    return conversation


@router.get("/api/check-ins/pending")
def get_pending_checkins(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    plans = CheckInService(db).get_pending_plans(user.id)
    action_plans = ActionPlanService(db)
    return [action_plans.to_response(plan) for plan in plans]


@router.post("/api/check-ins")
def submit_checkin(
    request: CheckInSubmitRequest,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    checkins = CheckInService(db)
    first_submission = checkins.get_by_plan(request.planId) is None
    try:
        checkin = checkins.submit_checkin(
            user.id,
            request.planId,
            request.improvementStatus,
            request.notes or "",
            request.itemStates,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    response = checkins.to_response(checkin)
    safety_message = (
        _maybe_escalate_checkin(db, user, request)
        if first_submission
        else ""
    )
    response["escalated"] = bool(safety_message)
    if safety_message:
        response["safetyMessage"] = safety_message
    return response


def _maybe_escalate_checkin(
    db: Session,
    user: UserAccount,
    request: CheckInSubmitRequest,
) -> str:
    plan = ActionPlanService(db).get_plan(user.id, request.planId)
    session = db.get(ChatSession, plan.session_id) if plan and plan.session_id else None
    result = EscalationService(
        db,
        ReviewService(db, get_settings()),
    ).check_checkin_escalation(
        user_id=user.id,
        session_id=session.id if session else None,
        report_id=None,
        thread_id=session.public_id if session else "",
        improvement_status=request.improvementStatus,
        current_difficulty=request.notes or "",
    )
    if result.should_escalate and result.review_id is not None:
        return result.user_message
    return ""


@router.get("/api/check-ins")
def list_checkins(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    checkins = CheckInService(db)
    return [checkins.to_response(checkin) for checkin in checkins.list_checkins(user.id)]


@router.get("/api/action-plans")
def list_action_plans(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    action_plans = ActionPlanService(db)
    return [
        action_plans.to_response(plan)
        for plan in action_plans.list_plans(user.id)
    ]


@router.get("/api/action-plans/{plan_id}")
def get_action_plan(
    plan_id: int,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    action_plans = ActionPlanService(db)
    plan = action_plans.get_plan(user.id, plan_id)
    if plan is None:
        raise HTTPException(404, "Plan not found")
    return action_plans.to_response(plan)


@router.post("/api/action-plans/items/{item_id}/complete")
def complete_action_item(
    item_id: int,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    item = ActionPlanService(db).mark_item_completed(user.id, item_id)
    if item is None:
        raise HTTPException(404, "Item not found or already completed")
    return {"id": item.id, "completed": item.completed}


@router.put("/api/action-plans/items/{item_id}")
def replace_action_item(
    item_id: int,
    request: ReplaceActionItemRequest,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    item = ActionPlanService(db).replace_item(user.id, item_id, request.content)
    if item is None:
        raise HTTPException(404, "Item not found, already completed, or not owned")
    return {"id": item.id, "content": item.content}
