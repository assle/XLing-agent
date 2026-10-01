from typing import Annotated

from anyio import CancelScope
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.types import Send

from app.core import diagnostics
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
from app.services.chat import ChatService, PendingReviewError
from app.services.checkin import CheckInService
from app.services.escalation import EscalationService
from app.services.report import ReportService
from app.services.review import ReviewService

router = APIRouter()


class ChatStreamingResponse(StreamingResponse):
    """Close suspended streams in their consuming task when the connection ends."""

    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                with CancelScope(shield=True):
                    await close()


@router.post("/api/chat/stream")
async def chat_stream(
    request: ChatRequest,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    # 为已登录的普通用户创建持续推送对话事件的响应。
    # request 提供消息和会话信息；管理员账号在此被拒绝。
    # 返回流式响应对象，消息处理和内容生成由聊天服务在流被读取时推进。
    if "ROLE_ADMIN" in user.roles:
        raise HTTPException(403, "管理员账号只能查看后台记录，不能发起学生对话。")
    service = ChatService(db, get_settings())
    try:
        service.ensure_chat_available(user, request.sessionId)
    except PendingReviewError as exc:
        with diagnostics.execution("chat.rejected", session_id=request.sessionId):
            diagnostics.emit("chat.blocked", reason_code="PENDING_REVIEW")
        raise HTTPException(409, str(exc)) from exc
    return ChatStreamingResponse(
        service.stream_chat(user, request),
        media_type="text/event-stream",
    )


@router.get("/api/sessions")
def list_sessions(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    # 列出当前用户自己的历史会话。
    # 使用认证得到的用户编号查询，客户端不能通过参数指定其他用户。
    return ReportService(db).list_sessions(user.id)


@router.get("/api/sessions/{session_id}")
def get_session(
    session_id: str,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    # 读取属于当前用户的指定会话和消息。
    # session_id 是公开会话编号；不存在或不属于当前用户时统一返回 404。
    conversation = ReportService(db).conversation_for_user(user.id, session_id)
    if conversation is None:
        raise HTTPException(404, "Session not found")
    return conversation


@router.get("/api/check-ins/pending")
def get_pending_checkins(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    # 返回当前用户尚需提交次日反馈的行动计划。
    # 先按反馈规则筛选计划，再使用行动计划服务的统一格式返回。
    plans = CheckInService(db).get_pending_plans(user.id)
    action_plans = ActionPlanService(db)
    return [action_plans.to_response(plan) for plan in plans]


@router.post("/api/check-ins")
def submit_checkin(
    request: CheckInSubmitRequest,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    # 保存行动计划的次日反馈，并在首次提交后检查是否需要安全升级。
    # 请求包含改善情况、备注和逐项完成状态；非法内容由服务层拒绝并转换为 400。
    # 重复提交更新已有反馈，不重复执行首次提交后的安全升级检查。
    checkins = CheckInService(db)
    # 先记住本次是否首次提交，保存后已有记录的判断结果会发生变化。
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
    # 反馈保存成功后才检查安全升级；重复提交不再次创建相同处理。
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
    """根据次日反馈的恶化情况关联原会话并检查人工审核需要。

    db、user、request 提供数据库、归属和反馈内容；仅在确实生成审核记录时返回安全提示，否则返回空字符串。
    """
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
    # 返回当前用户的次日反馈历史。
    # 逐条使用反馈服务转换为接口字段，不改变反馈记录。
    checkins = CheckInService(db)
    return [checkins.to_response(checkin) for checkin in checkins.list_checkins(user.id)]


@router.get("/api/action-plans")
def list_action_plans(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    # 列出当前用户的行动计划并附上各项内容。
    # 查询和返回格式均通过行动计划服务统一处理。
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
    # 读取属于当前用户的一份行动计划。
    # plan_id 来自请求路径；查不到匹配归属的计划时返回 404。
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
    # 将当前用户可操作的行动项标记为完成。
    # item_id 指定行动项；服务层返回空值时响应 404，成功时返回编号和完成状态。
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
    # 替换当前用户尚可修改的行动项内容。
    # request.content 是新文本；不存在、已完成或不属于用户时返回 404，成功时返回实际保存的内容。
    item = ActionPlanService(db).replace_item(user.id, item_id, request.content)
    if item is None:
        raise HTTPException(404, "Item not found, already completed, or not owned")
    return {"id": item.id, "content": item.content}
