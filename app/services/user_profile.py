from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.time import utc_now
from app.models.entities import UserProfile


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

    def update_support_background(
        self,
        user_id: int,
        current_concern: str | None = None,
        support_goal: str | None = None,
        preferred_support_style: str | None = None,
    ) -> UserProfile:
        profile = self.get_or_create(user_id)
        if current_concern is not None:
            profile.current_concern = current_concern.strip() or None
        if support_goal is not None:
            profile.support_goal = support_goal.strip() or None
        if preferred_support_style is not None:
            profile.preferred_support_style = preferred_support_style.strip() or None
        profile.updated_at = utc_now()
        self.db.commit()
        self.db.refresh(profile)
        return profile

    def get_support_context(self, user_id: int) -> str:
        profile = self.get_profile(user_id)
        if profile is None:
            return ""
        fields = [
            ("当前关注", profile.current_concern),
            ("支持目标", profile.support_goal),
            ("偏好方式", profile.preferred_support_style),
        ]
        return "\n".join(f"{label}：{value}" for label, value in fields if value)
