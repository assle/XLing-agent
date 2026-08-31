from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import require_admin
from app.models.entities import UserAccount
from app.schemas.dtos import KnowledgeIngestRequest, KnowledgeIngestResponse, ReviewDecisionRequest
from app.services.knowledge import KnowledgeService
from app.services.report import ReportService
from app.services.review import REVIEW_DECISIONS, ReviewService

router = APIRouter()


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
def admin_conversation(
    session_id: str,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
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


@router.post("/api/admin/knowledge/file")
async def ingest_file(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile = File(...),
):
    chunks = KnowledgeService(db, get_settings()).ingest_file(
        file.filename or "uploaded-file",
        await file.read(),
    )
    return KnowledgeIngestResponse(source=file.filename or "uploaded-file", chunks=chunks)


@router.get("/api/admin/reviews")
def list_reviews(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    all: bool = False,
):
    reviews = ReviewService(db, get_settings())
    return reviews.list_all() if all else reviews.list_pending()


@router.post("/api/admin/reviews/{review_id}/decision")
async def decide_review(
    review_id: int,
    request: ReviewDecisionRequest,
    user: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    decision = (request.decision or "").strip().lower()
    if decision not in REVIEW_DECISIONS:
        raise HTTPException(400, f"Invalid decision: {decision}. Valid: {sorted(REVIEW_DECISIONS)}")

    reviews = ReviewService(db, get_settings())
    review = reviews.get_review(review_id)
    if review is None or review.status != "pending":
        raise HTTPException(404, f"Review {review_id} not found or not pending")

    decision_details = {
        "referral_target": request.referralTarget,
        "next_step": request.nextStep,
        "follow_up_owner": request.followUpOwner,
        "follow_up_at": request.followUpAt,
    }

    def record_decision():
        return reviews.mark_decision(
            review_id,
            decision,
            request.note or "",
            user.username,
            **decision_details,
        )

    try:
        if decision in {"approve", "reject"}:
            response_text, degraded = await reviews.resume_and_respond(
                review,
                approved=decision == "approve",
            )
            if degraded:
                reviews.mark_escalated(review_id)
                return {"status": "escalated", "responsePreview": response_text[:200]}
            decided = record_decision()
            return {"status": decided.status, "responsePreview": response_text[:200]}

        decided = record_decision()
        student_message = reviews.persist_student_message(decided)
        return {"status": decided.status, "studentMessage": student_message or None}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
