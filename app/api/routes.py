from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.agents.factory import agent_framework_status
from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import current_user, require_admin, hash_password, verify_password, is_legacy_hash, verify_legacy_password, create_access_token
from app.models.entities import UserAccount
from app.schemas.dtos import KnowledgeIngestRequest, KnowledgeIngestResponse, ChatRequest, LoginRequest, ResetPasswordRequest, UpdateUserProfileRequest, UserProfileResponse, CreateMemoryCardRequest, UpdateMemoryCardRequest, ScreeningSubmitRequest, CheckInSubmitRequest, ReviewDecisionRequest, authority
from app.services.chat import ChatService
from app.services.knowledge import KnowledgeService
from app.services.model_assets import finetuned_model_status
from app.services.report import ReportService

router = APIRouter()


@router.get("/actuator/health")
def health():
    from app.services.risk_trajectory import RiskTrajectoryHealth
    return {"status": "UP", "riskTrajectory": RiskTrajectoryHealth.snapshot()}


# ---------------------------------------------------------------------------
# Privacy notice + data deletion (issue 13)
# ---------------------------------------------------------------------------

@router.get("/api/privacy")
def get_privacy_notice():
    from app.services.data_deletion import PRIVACY_NOTICE
    return {"notice": PRIVACY_NOTICE}


@router.delete("/api/account")
def delete_account(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    from app.services.data_deletion import DataDeletionService
    try:
        counts = DataDeletionService(db).delete_all_user_data(user.id)
        return {"deleted": True, "details": counts}
    except Exception as exc:
        raise HTTPException(500, f"Deletion failed, data rolled back: {exc}") from exc

# ---------------------------------------------------------------------------
# Auth endpoints (issue 01: bcrypt + JWT migration)
# ---------------------------------------------------------------------------

@router.post("/api/auth/login")
def login(request: LoginRequest, db: Annotated[Session, Depends(get_db)]):
    user = db.query(UserAccount).filter(UserAccount.username == request.username).first()
    if user is None:
        raise HTTPException(401, "Bad credentials")
    if is_legacy_hash(user.password_hash):
        if not verify_legacy_password(request.password, user.password_hash):
            raise HTTPException(401, "Bad credentials")
        return {"resetRequired": True, "username": user.username}
    if not verify_password(request.password, user.password_hash):
        raise HTTPException(401, "Bad credentials")
    token = create_access_token(user)
    return {"accessToken": token, "tokenType": "Bearer", "expiresIn": 86400}


@router.post("/api/auth/reset")
def reset_password(request: ResetPasswordRequest, db: Annotated[Session, Depends(get_db)]):
    user = db.query(UserAccount).filter(UserAccount.username == request.username).first()
    if user is None:
        raise HTTPException(401, "Bad credentials")
    if is_legacy_hash(user.password_hash):
        if not verify_legacy_password(request.oldPassword, user.password_hash):
            raise HTTPException(401, "Bad credentials")
    else:
        if not verify_password(request.oldPassword, user.password_hash):
            raise HTTPException(401, "Bad credentials")
    user.password_hash = hash_password(request.newPassword)
    db.commit()
    token = create_access_token(user)
    return {"accessToken": token, "tokenType": "Bearer", "expiresIn": 86400}


@router.get("/api/profile")
def profile(user: Annotated[UserAccount, Depends(current_user)]):
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name,
        "roles": [authority(role) for role in user.roles],
    }

# ---------------------------------------------------------------------------
# User profile / exam stage (issue 03)
# ---------------------------------------------------------------------------

@router.get("/api/profile/exam")
def get_exam_profile(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    from app.services.user_profile import UserProfileService
    profile = UserProfileService(db).get_profile(user.id)
    if profile is None:
        return UserProfileResponse()
    return UserProfileResponse(
        examStage=profile.exam_stage,
        targetExam=profile.target_exam,
        examDate=profile.exam_date.isoformat() if profile.exam_date else None,
    )


@router.put("/api/profile/exam")
def update_exam_profile(
    request: UpdateUserProfileRequest,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    from app.services.user_profile import UserProfileService
    try:
        profile = UserProfileService(db).update_profile(
            user.id,
            exam_stage=request.examStage,
            target_exam=request.targetExam,
            exam_date=request.examDate,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return UserProfileResponse(
        examStage=profile.exam_stage,
        targetExam=profile.target_exam,
        examDate=profile.exam_date.isoformat() if profile.exam_date else None,
    )





# ---------------------------------------------------------------------------
# Voluntary explicit screening: PHQ-9 / GAD-7 (issue 05)
# ---------------------------------------------------------------------------

@router.get("/api/screening/results")
def list_screening_results(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    from app.services.screening import ScreeningService
    svc = ScreeningService(db)
    results = svc.list_results(user.id)
    return [svc.to_response(r) for r in results]


@router.get("/api/screening/results/{result_id}")
def get_screening_result(
    result_id: int,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    from app.services.screening import ScreeningService
    svc = ScreeningService(db)
    result = svc.get_result(user.id, result_id)
    if result is None:
        raise HTTPException(404, "Screening result not found")
    return svc.to_response(result)


@router.get("/api/screening/{scale_type}")
def get_screening_scale(
    scale_type: str,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    from app.services.screening import ScreeningService
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
    from app.services.screening import ScreeningService
    try:
        result = ScreeningService(db).submit_screening(
            user.id, scale_type, request.answers, request.triggerSource or "voluntary"
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return ScreeningService(db).to_response(result)


# ---------------------------------------------------------------------------
# Memory cards (issue 04)
# ---------------------------------------------------------------------------

@router.get("/api/memory-cards")
def list_memory_cards(user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    from app.services.memory_cards import MemoryCardService
    cards = MemoryCardService(db).list_cards(user.id)
    return [{"id": c.id, "content": c.content, "source": c.source, "confirmed": c.confirmed,
             "createdAt": c.created_at.isoformat(), "updatedAt": c.updated_at.isoformat()} for c in cards]


@router.post("/api/memory-cards")
def create_memory_card(request: CreateMemoryCardRequest, user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    from app.services.memory_cards import MemoryCardService
    card = MemoryCardService(db).create_card(user.id, request.content)
    return {"id": card.id, "content": card.content, "source": card.source, "confirmed": card.confirmed}


@router.put("/api/memory-cards/{card_id}")
def update_memory_card(card_id: int, request: UpdateMemoryCardRequest, user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    from app.services.memory_cards import MemoryCardService
    try:
        card = MemoryCardService(db).update_card(user.id, card_id, request.content)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"id": card.id, "content": card.content, "confirmed": card.confirmed}


@router.delete("/api/memory-cards/{card_id}")
def delete_memory_card(card_id: int, user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    from app.services.memory_cards import MemoryCardService
    try:
        MemoryCardService(db).delete_card(user.id, card_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"deleted": True}


@router.post("/api/memory-cards/{card_id}/confirm")
def confirm_memory_card(card_id: int, user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    from app.services.memory_cards import MemoryCardService
    try:
        card = MemoryCardService(db).confirm_card(user.id, card_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"id": card.id, "content": card.content, "confirmed": card.confirmed}

@router.post("/api/chat/stream")
async def chat_stream(
    request: ChatRequest,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    if "ROLE_ADMIN" in user.roles:
        raise HTTPException(403, "管理员账号只能查看后台记录，不能发起学生对话。")
    service = ChatService(db, get_settings())
    return StreamingResponse(service.stream_chat(user, request), media_type="text/event-stream")


@router.get("/api/agent/status")
def agent_status(user: Annotated[UserAccount, Depends(current_user)]):
    settings = get_settings()
    provider = settings.ai_provider.lower()
    model = settings.ollama_model if provider == "ollama" else settings.openai_model if provider == "openai" else "mock"
    return {
        "provider": provider,
        "model": model,
        "realModelEnabled": provider in {"ollama", "openai"},
        "agentFramework": agent_framework_status(settings),
        "finetunedModel": finetuned_model_status(settings),
        "agents": [
            {"name": "MemoryAgent", "status": "READY", "description": "短期上下文与长期记忆摘要"},
            {"name": "SupervisorAgent", "status": "READY", "description": "消息分流"},
            {"name": "RiskGuardianAgent", "status": "READY", "description": "安全风险评估与分级"},
            {"name": "KnowledgeAgent", "status": "READY", "description": "相关支持内容检索"},
            {"name": "CompanionAgent", "status": "READY", "description": "普通陪伴式回复"},
            {"name": "CounselorAgent", "status": "READY", "description": "咨询式支持回复"},
        ],
       "loop": {"type": "bounded", "maxSteps": 8},
   }


@router.get("/api/sessions")
def list_sessions(user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).list_sessions(user.id)

@router.get("/api/sessions/{session_id}")
def get_session(session_id: str, user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    conv = ReportService(db).conversation_for_user(user.id, session_id)
    if conv is None:
        raise HTTPException(404, "Session not found")
    return conv


@router.get("/api/reports/me")
def my_reports(user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).latest_reports(user.id)


@router.get("/api/admin/reports")
def admin_reports(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).latest_reports()


@router.get("/api/admin/excel-records")
def admin_excel(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).excel_records()


@router.get("/api/admin/alerts")
def admin_alerts(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).alert_records()


@router.get("/api/admin/tool-jobs")
def admin_tool_jobs(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).tool_jobs()


@router.get("/api/admin/dead-letters")
def admin_dead_letters(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).dead_letters()


@router.get("/api/admin/conversations/{session_id}")
def admin_conversation(session_id: str, _: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    try:
        return ReportService(db).conversation(session_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/api/admin/knowledge")
def ingest_knowledge(
    request: KnowledgeIngestRequest,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    chunks = KnowledgeService(db, get_settings()).ingest(request.source, request.content)
    return KnowledgeIngestResponse(source=request.source, chunks=chunks)


@router.get("/api/admin/knowledge/status")
def knowledge_status(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return KnowledgeService(db, get_settings()).status()


@router.post("/api/admin/knowledge/rebuild-vector")
def rebuild_knowledge_vector(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    try:
        indexed = KnowledgeService(db, get_settings()).rebuild_vector_index()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"indexedChunks": indexed}


@router.post("/api/admin/knowledge/backup")
def backup_knowledge_vector(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    try:
        snapshot = KnowledgeService(db, get_settings()).backup_vector_index()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"snapshot": snapshot}


@router.post("/api/admin/knowledge/file")
async def ingest_file(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile = File(...),
):
    chunks = KnowledgeService(db, get_settings()).ingest_file(file.filename or "uploaded-file", await file.read())
    return KnowledgeIngestResponse(source=file.filename or "uploaded-file", chunks=chunks)


# ---------------------------------------------------------------------------
# Next-day feedback (issue 10); API paths retain check-ins for compatibility.
# ---------------------------------------------------------------------------

@router.get("/api/check-ins/pending")
def get_pending_checkins(user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    from app.services.checkin import CheckInService
    from app.services.action_plan import ActionPlanService
    plans = CheckInService(db).get_pending_plans(user.id)
    return [ActionPlanService(db).to_response(p) for p in plans]


@router.post("/api/check-ins")
def submit_checkin(request: CheckInSubmitRequest, user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    from app.services.checkin import CheckInService
    svc = CheckInService(db)
    first_submission = svc.get_by_plan(request.planId) is None
    try:
        checkin = svc.submit_checkin(
            user.id, request.planId, request.improvementStatus,
            request.notes or "", request.itemStates,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    response = svc.to_response(checkin)
    # Support-loop wiring: worsening or no-improvement feedback triggers safety escalation.
    # review exactly once (idempotent resubmissions do not re-escalate).
    safety_message = _maybe_escalate_checkin(db, user, request) if first_submission else ""
    response["escalated"] = bool(safety_message)
    if safety_message:
        response["safetyMessage"] = safety_message
    return response


def _maybe_escalate_checkin(db: Session, user: UserAccount, request: CheckInSubmitRequest) -> str:
    """Run the feedback escalation check; return the student-facing safety
    message when a human review was created, else ""."""
    from app.models.entities import ChatSession
    from app.services.action_plan import ActionPlanService
    from app.services.escalation import EscalationService
    from app.services.review import ReviewService

    plan = ActionPlanService(db).get_plan(user.id, request.planId)
    session = db.get(ChatSession, plan.session_id) if plan and plan.session_id else None
    result = EscalationService(db, ReviewService(db, get_settings())).check_checkin_escalation(
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
def list_checkins(user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    from app.services.checkin import CheckInService
    svc = CheckInService(db)
    checkins = svc.list_checkins(user.id)
    return [svc.to_response(c) for c in checkins]

# ---------------------------------------------------------------------------
# Action plans (issue 09)
# ---------------------------------------------------------------------------

@router.get("/api/action-plans")
def list_action_plans(user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    from app.services.action_plan import ActionPlanService
    plans = ActionPlanService(db).list_plans(user.id)
    return [ActionPlanService(db).to_response(p) for p in plans]


@router.get("/api/action-plans/{plan_id}")
def get_action_plan(plan_id: int, user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    from app.services.action_plan import ActionPlanService
    plan = ActionPlanService(db).get_plan(user.id, plan_id)
    if plan is None:
        raise HTTPException(404, "Plan not found")
    return ActionPlanService(db).to_response(plan)


@router.post("/api/action-plans/items/{item_id}/complete")
def complete_action_item(item_id: int, user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    from app.services.action_plan import ActionPlanService
    item = ActionPlanService(db).mark_item_completed(user.id, item_id)
    if item is None:
        raise HTTPException(404, "Item not found or already completed")
    return {"id": item.id, "completed": item.completed}


@router.put("/api/action-plans/items/{item_id}")
def replace_action_item(item_id: int, request: UpdateMemoryCardRequest, user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    from app.services.action_plan import ActionPlanService
    item = ActionPlanService(db).replace_item(user.id, item_id, request.content)
    if item is None:
        raise HTTPException(404, "Item not found, already completed, or not owned")
    return {"id": item.id, "content": item.content}

# ---------------------------------------------------------------------------
# Review queue endpoints (issue 06: counselor human review)
# ---------------------------------------------------------------------------

@router.get("/api/admin/reviews")
def list_reviews(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    all: bool = False,
):
    from app.services.review import ReviewService

    svc = ReviewService(db, get_settings())
    if all:
        return svc.list_all()
    return svc.list_pending()


@router.post("/api/admin/reviews/{review_id}/decision")
async def decide_review(
    review_id: int,
    request: ReviewDecisionRequest,
    user: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    """Record one of four reviewer decisions with an optional note (issue 11).

    approve / reject also resume the interrupted agent run so the student
    receives the response; refer / monitor only record the outcome.
    """
    from app.services.review import REVIEW_DECISIONS, ReviewService

    decision = (request.decision or "").strip().lower()
    if decision not in REVIEW_DECISIONS:
        raise HTTPException(400, f"Invalid decision: {decision}. Valid: {sorted(REVIEW_DECISIONS)}")

    settings = get_settings()
    review_svc = ReviewService(db, settings)
    review = review_svc.get_review(review_id)
    if review is None or review.status != "pending":
        raise HTTPException(404, f"Review {review_id} not found or not pending")

    decision_details = {
        "referral_target": request.referralTarget,
        "next_step": request.nextStep,
        "follow_up_owner": request.followUpOwner,
        "follow_up_at": request.followUpAt,
    }

    def record_decision():
        return review_svc.mark_decision(
            review_id, decision, request.note or "", user.username, **decision_details
        )

    try:
        if decision in {"approve", "reject"}:
            response_text, degraded = await review_svc.resume_and_respond(review, approved=(decision == "approve"))
            if degraded:
                review_svc.mark_escalated(review_id)
                return {"status": "escalated", "responsePreview": response_text[:200]}
            decided = record_decision()
            return {"status": decided.status, "responsePreview": response_text[:200]}

        decided = record_decision()
        student_message = review_svc.persist_student_message(decided)
        return {"status": decided.status, "studentMessage": student_message or None}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
