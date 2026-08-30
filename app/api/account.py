from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import (
    create_access_token,
    current_user,
    hash_password,
    is_legacy_hash,
    verify_legacy_password,
    verify_password,
)
from app.models.entities import UserAccount
from app.schemas.dtos import (
    CreateMemoryCardRequest,
    LoginRequest,
    ResetPasswordRequest,
    ScreeningSubmitRequest,
    UpdateMemoryCardRequest,
    UpdateUserProfileRequest,
    UserProfileResponse,
    authority,
)
from app.services.data_deletion import PRIVACY_NOTICE, DataDeletionService
from app.services.memory_cards import MemoryCardService
from app.services.screening import ScreeningService
from app.services.user_profile import UserProfileService

router = APIRouter()


def _password_matches(user: UserAccount, password: str) -> bool:
    if is_legacy_hash(user.password_hash):
        return verify_legacy_password(password, user.password_hash)
    return verify_password(password, user.password_hash)


def _memory_card_response(card) -> dict:
    return {
        "id": card.id,
        "content": card.content,
        "source": card.source,
        "confirmed": card.confirmed,
        "createdAt": card.created_at.isoformat(),
        "updatedAt": card.updated_at.isoformat(),
    }


@router.get("/api/privacy")
def get_privacy_notice():
    return {"notice": PRIVACY_NOTICE}


@router.delete("/api/account")
def delete_account(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        counts = DataDeletionService(db).delete_all_user_data(user.id)
        return {"deleted": True, "details": counts}
    except Exception as exc:
        raise HTTPException(500, f"Deletion failed, data rolled back: {exc}") from exc


@router.post("/api/auth/login")
def login(request: LoginRequest, db: Annotated[Session, Depends(get_db)]):
    user = db.query(UserAccount).filter(UserAccount.username == request.username).first()
    if user is None or not _password_matches(user, request.password):
        raise HTTPException(401, "Bad credentials")
    if is_legacy_hash(user.password_hash):
        return {"resetRequired": True, "username": user.username}
    token = create_access_token(user)
    return {"accessToken": token, "tokenType": "Bearer", "expiresIn": 86400}


@router.post("/api/auth/reset")
def reset_password(request: ResetPasswordRequest, db: Annotated[Session, Depends(get_db)]):
    user = db.query(UserAccount).filter(UserAccount.username == request.username).first()
    if user is None or not _password_matches(user, request.oldPassword):
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


@router.get("/api/profile/exam")
def get_exam_profile(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
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


@router.get("/api/screening/results")
def list_screening_results(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    screening = ScreeningService(db)
    return [screening.to_response(result) for result in screening.list_results(user.id)]


@router.get("/api/screening/results/{result_id}")
def get_screening_result(
    result_id: int,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
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
    return screening.to_response(result)


@router.get("/api/memory-cards")
def list_memory_cards(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
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
    card = MemoryCardService(db).create_card(user.id, request.content)
    return _memory_card_response(card)


@router.put("/api/memory-cards/{card_id}")
def update_memory_card(
    card_id: int,
    request: UpdateMemoryCardRequest,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
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
    try:
        card = MemoryCardService(db).confirm_card(user.id, card_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return _memory_card_response(card)
