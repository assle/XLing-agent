import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import (
    create_access_token,
    current_user,
    verify_password,
)
from app.core.time import utc_isoformat
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import (
    CreateMemoryCardRequest,
    LoginRequest,
    ScreeningSubmitRequest,
    SupportProfileResponse,
    UpdateMemoryCardRequest,
    UpdateSupportProfileRequest,
    authority,
)
from app.services.data_deletion import PRIVACY_NOTICE, DataDeletionService
from app.services.escalation import EscalationService
from app.services.memory_cards import MemoryCardService
from app.services.review import ReviewService
from app.services.screening import ScreeningService
from app.services.user_profile import UserProfileService

router = APIRouter()


def _memory_card_response(card) -> dict:
    """把记忆卡片转换为网页使用的字段。

    保留内容、来源和确认状态，并将创建和更新时间转换成可传输的标准文本。
    """
    return {
        "id": card.id,
        "content": card.content,
        "source": card.source,
        "confirmed": card.confirmed,
        "createdAt": utc_isoformat(card.created_at),
        "updatedAt": utc_isoformat(card.updated_at),
    }


@router.get("/api/privacy")
def get_privacy_notice():
    # 返回项目统一维护的隐私说明。
    # 直接复用隐私服务中的正文，避免接口另存一份不同步的说明。
    return {"notice": PRIVACY_NOTICE}


@router.delete("/api/account")
def delete_account(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    # 删除当前用户的数据，并区分业务数据删除与会话执行状态清理结果。
    # 返回各类删除数量和待清理标志；外部状态尚待清理时 deleted 为 False，但业务数据可能已经删除。
    try:
        counts = DataDeletionService(db).delete_all_user_data(user.id)
        cleanup_pending = bool(counts.get("checkpoint_cleanup_pending", 0))
        return {
            "deleted": not cleanup_pending,
            "businessDataDeleted": True,
            "checkpointCleanupPending": cleanup_pending,
            "details": counts,
        }
    except Exception as exc:
        raise HTTPException(500, f"Deletion failed, data rolled back: {exc}") from exc


@router.post("/api/auth/login")
def login(request: LoginRequest, db: Annotated[Session, Depends(get_db)]):
    # 按用户名查找账户、核对密码并签发登录凭证。
    # 用户不存在和密码不匹配统一返回 401，避免通过错误文字区分账户是否存在。
    # 响应中的 expiresIn 当前固定为 86400；实际凭证有效期仍由签发函数读取配置决定。
    user = db.query(UserAccount).filter(UserAccount.username == request.username).first()
    if user is None or not verify_password(request.password, user.password_hash):
        raise HTTPException(401, "Bad credentials")
    token = create_access_token(user)
    return {"accessToken": token, "tokenType": "Bearer", "expiresIn": 86400}


@router.get("/api/profile")
def profile(user: Annotated[UserAccount, Depends(current_user)]):
    # 返回当前用户的公开账户信息和角色列表。
    # 不返回密码校验值；角色通过统一转换函数包装成网页约定的结构。
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name,
        "roles": [authority(role) for role in user.roles],
    }


@router.get("/api/profile/support")
def get_support_profile(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    # 读取用户自愿填写的支持背景。
    # 尚无背景记录时返回字段为空的响应对象，不为只读请求创建数据库记录。
    profile = UserProfileService(db).get_profile(user.id)
    if profile is None:
        return SupportProfileResponse()
    return SupportProfileResponse(
        currentConcern=profile.current_concern,
        supportGoal=profile.support_goal,
        preferredSupportStyle=profile.preferred_support_style,
    )


@router.put("/api/profile/support")
def update_support_profile(
    request: UpdateSupportProfileRequest,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    # 更新当前用户的关注问题、支持目标和偏好支持方式。
    # 将请求字段传给背景服务保存，再用保存后的值构造响应。
    profile = UserProfileService(db).update_support_background(
        user.id,
        current_concern=request.currentConcern,
        support_goal=request.supportGoal,
        preferred_support_style=request.preferredSupportStyle,
    )
    return SupportProfileResponse(
        currentConcern=profile.current_concern,
        supportGoal=profile.support_goal,
        preferredSupportStyle=profile.preferred_support_style,
    )


@router.get("/api/screening/results")
def list_screening_results(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    # 列出当前用户的自愿量表筛查历史。
    # 结果逐条使用筛查服务的统一格式输出，不把筛查分数解释成诊断。
    screening = ScreeningService(db)
    return [screening.to_response(result) for result in screening.list_results(user.id)]


@router.get("/api/screening/results/{result_id}")
def get_screening_result(
    result_id: int,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    # 按编号读取当前用户的一条筛查结果。
    # 结果不存在或不属于当前用户时返回 404，不暴露他人记录。
    screening = ScreeningService(db)
    result = screening.get_result(user.id, result_id)
    if result is None:
        raise HTTPException(404, "Screening result not found")
    return screening.to_response(result)


@router.get("/api/screening/{scale_type}")
def get_screening_scale(
    scale_type: str,
    _: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    # 返回指定量表的题目、选项和使用说明。
    # scale_type 指定量表种类；不支持的类型由服务层报错并转换成 400 响应。
    try:
        return ScreeningService(db).get_scale_info(scale_type)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/screening/{scale_type}/submit")
def submit_screening(
    scale_type: str,
    request: ScreeningSubmitRequest,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    # 校验并保存用户主动提交的量表答案，必要时建立安全升级会话。
    # 普通结果直接返回；被标记需立即关注时先保存关联会话，再调用安全升级服务。
    # 筛查、会话与审核的保存有各自步骤，不是一笔统一提交的数据库事务。
    screening = ScreeningService(db)
    try:
        result = screening.submit_screening(
            user.id,
            scale_type,
            request.answers,
            request.triggerSource or "voluntary",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    response = screening.to_response(result)
    # 筛查中的特定答案需要立即关注时，建立一个独立会话承接后续人工审核。
    if result.high_risk_flagged:
        session = ChatSession(
            public_id=uuid.uuid4().hex,
            user_id=user.id,
            title="自愿量表筛查安全升级",
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        escalation = EscalationService(db, ReviewService(db, get_settings())).check_screening_escalation(
            user_id=user.id,
            session_id=session.id,
            report_id=None,
            thread_id=session.public_id,
            high_risk_flagged=True,
            current_difficulty=f"{result.scale_type} 自愿量表筛查出现需要立即关注的答案",
        )
        response["escalated"] = escalation.should_escalate
        response["safetyMessage"] = escalation.user_message
    else:
        response["escalated"] = False
    return response


@router.get("/api/memory-cards")
def list_memory_cards(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    # 返回当前用户可查看和管理的全部记忆卡片。
    # 包括已确认卡片和待确认建议；用于对话的长期记忆另有已确认筛选。
    return [
        _memory_card_response(card)
        for card in MemoryCardService(db).list_cards(user.id)
    ]


@router.post("/api/memory-cards")
def create_memory_card(
    request: CreateMemoryCardRequest,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    # 保存用户主动创建的记忆卡片并返回其状态。
    # 内容和确认方式由记忆卡片服务处理，用户归属来自当前登录身份。
    card = MemoryCardService(db).create_card(user.id, request.content)
    return _memory_card_response(card)


@router.put("/api/memory-cards/{card_id}")
def update_memory_card(
    card_id: int,
    request: UpdateMemoryCardRequest,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    # 修改当前用户自己的记忆卡片内容。
    # card_id 为路径编号；不存在或归属不符时将服务层错误转换为 404。
    try:
        card = MemoryCardService(db).update_card(user.id, card_id, request.content)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return _memory_card_response(card)


@router.delete("/api/memory-cards/{card_id}")
def delete_memory_card(
    card_id: int,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    # 删除当前用户自己的一条记忆卡片。
    # 服务层成功提交后返回 deleted 标志；不存在或归属不符时响应 404。
    try:
        MemoryCardService(db).delete_card(user.id, card_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"deleted": True}


@router.post("/api/memory-cards/{card_id}/confirm")
def confirm_memory_card(
    card_id: int,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    # 将当前用户的一条卡片确认为可供长期记忆使用。
    # 服务层先验证归属并保存确认状态，再返回更新后的卡片。
    try:
        card = MemoryCardService(db).confirm_card(user.id, card_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return _memory_card_response(card)
