from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import require_admin
from app.models.entities import UserAccount
from app.schemas.dtos import (
    HumanReplyRequest,
    KnowledgeIngestRequest,
    KnowledgeIngestResponse,
    OffsiteContactRequest,
    ReviewDecisionRequest,
)
from app.services.knowledge import KnowledgeService
from app.services.report import ReportService
from app.services.review import ReviewConflictError, ReviewOutcome, ReviewService

router = APIRouter()


@router.get("/api/admin/reports")
def admin_reports(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    # 向授权审核人员返回最近的安全评估记录。
    # 管理员权限由接口依赖检查；查询和返回字段由 ReportService 负责。
    return ReportService(db).latest_reports()


@router.get("/api/admin/excel-records")
def admin_excel(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    # 向授权审核人员返回表格写入记录。
    # 读取已有记录，不在此生成表格；管理员身份验证在进入函数前完成。
    return ReportService(db).excel_records()


@router.get("/api/admin/alerts")
def admin_alerts(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    # 向授权审核人员返回通知记录。
    # 记录中的状态描述投递结果，此接口只查询，不触发新的通知。
    return ReportService(db).alert_records()


@router.get("/api/admin/tool-jobs")
def admin_tool_jobs(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    # 向授权审核人员返回工具任务及执行状态。
    # 使用本次请求的数据库会话查询，不在接口内调度任务。
    return ReportService(db).tool_jobs()


@router.get("/api/admin/dead-letters")
def admin_dead_letters(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    # 列出已进入失败留存区的任务，供授权审核人员排查。
    # 这里只展示服务层查询结果，不自动重试或删除失败任务。
    return ReportService(db).dead_letters()


@router.get("/api/admin/conversations/{session_id}")
def admin_conversation(
    session_id: str,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    # 按会话公开编号读取管理员可查看的对话详情。
    # session_id 是请求路径中的编号；服务层报告不存在时转换为 404 响应。
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
    # 将提交的来源名称和正文导入知识库，并返回生成的片段数量。
    # 操作可能更新数据库及检索索引；来源解析与切片由知识服务执行。
    chunks = KnowledgeService(db, get_settings()).ingest(request.source, request.content)
    return KnowledgeIngestResponse(source=request.source, chunks=chunks)


@router.get("/api/admin/knowledge/status")
def knowledge_status(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    # 返回当前知识库的数量及检索组件状态。
    # 仅允许管理员访问；返回内容由当前配置下的知识服务生成。
    return KnowledgeService(db, get_settings()).status()


@router.post("/api/admin/knowledge/rebuild-vector")
def rebuild_knowledge_vector(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    # 根据现有知识片段重新建立向量检索索引。
    # 成功时返回处理数量；服务层 RuntimeError 转为 503 响应，表示当前服务不可用。
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
    # 读取上传文件后交给知识服务解析和导入。
    # 读取上传内容时异步等待，让服务有机会处理其他请求；缺少文件名时使用统一占位名称。
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
    # 按查询参数返回全部审核记录或仅待处理记录。
    # all 默认为 False；排序和字段整理统一由审核服务负责。
    reviews = ReviewService(db, get_settings())
    return reviews.list_all() if all else reviews.list_active()


@router.post("/api/admin/reviews/{review_id}/claim")
def claim_review(
    review_id: int,
    user: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        review = ReviewService(db, get_settings()).claim(review_id, user.username)
        return {"status": review.status, "claimedBy": review.claimed_by, "claimedAt": review.claimed_at}
    except ReviewConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/api/admin/reviews/{review_id}/reply")
def reply_to_user(
    review_id: int,
    request: HumanReplyRequest,
    user: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        message = ReviewService(db, get_settings()).reply(
            review_id, request.content, user.username, request.clientRequestId,
        )
        return {"status": "in_progress", "messageId": message.id}
    except ReviewConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/admin/reviews/{review_id}/contact")
def record_offsite_contact(
    review_id: int,
    request: OffsiteContactRequest,
    user: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        contact = ReviewService(db, get_settings()).record_offsite_contact(
            review_id,
            contact_method=request.contactMethod,
            contacted_at=request.contactedAt,
            outcome=request.outcome,
            next_step=request.nextStep,
            recorded_by=user.username,
            client_request_id=request.clientRequestId,
        )
        return {"status": "in_progress", "contactId": contact.id}
    except ReviewConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/admin/reviews/{review_id}/decision")
def decide_review(
    review_id: int,
    request: ReviewDecisionRequest,
    user: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        outcome = ReviewOutcome(
            decision=request.decision,
            note=request.note or "",
            referral_target=request.referralTarget,
            next_step=request.nextStep,
            follow_up_owner=request.followUpOwner,
            follow_up_at=request.followUpAt,
            completion_basis=request.completionBasis,
        )
        decided = ReviewService(db, get_settings()).mark_decision(review_id, outcome, user.username)
        message = ReviewService.support_outcome_message(decided)
        return {
            "status": decided.status,
            "supportOutcomeMessage": message,
            "studentMessage": message,
        }
    except ReviewConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
