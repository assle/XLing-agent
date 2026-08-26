from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.orm import Session

from app.core.enums import ExamStage
from app.models.entities import UserProfile


VALID_STAGES = {stage.value for stage in ExamStage}


class UserProfileService:
    def __init__(self, db: Session):
        self.db = db

    def get_profile(self, user_id: int) -> UserProfile | None:
        return self.db.query(UserProfile).filter(UserProfile.user_id == user_id).first()

    def get_or_create(self, user_id: int) -> UserProfile:
        profile = self.get_profile(user_id)
        if profile is None:
            profile = UserProfile(user_id=user_id)
            self.db.add(profile)
            self.db.commit()
            self.db.refresh(profile)
        return profile

    def update_profile(
        self,
        user_id: int,
        exam_stage: str | None = None,
        target_exam: str | None = None,
        exam_date: str | None = None,
    ) -> UserProfile:
        # Validate exam_stage if a non-empty value is provided
        stage_value: str | None = None
        stage_provided = exam_stage is not None
        if stage_provided and exam_stage != "":
            if exam_stage not in VALID_STAGES:
                raise ValueError(f"Invalid exam stage: {exam_stage}. Valid: {VALID_STAGES}")
            stage_value = exam_stage

        profile = self.get_or_create(user_id)

        if stage_provided:
            profile.exam_stage = stage_value

        if target_exam is not None:
            profile.target_exam = target_exam.strip() or None

        if exam_date is not None:
            profile.exam_date = self._parse_date(exam_date) if exam_date.strip() else None

        profile.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(profile)
        return profile

    def get_stage_context(self, user_id: int) -> str:
        """Return the user's exam stage for agent runtime context."""
        profile = self.get_profile(user_id)
        if profile is None or not profile.exam_stage:
            return ""
        return profile.exam_stage

    @staticmethod
    def _parse_date(date_str: str) -> date:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                return datetime.strptime(date_str.strip(), fmt).date()
            except ValueError:
                continue
        raise ValueError(f"Invalid date format: {date_str}. Use YYYY-MM-DD.")
