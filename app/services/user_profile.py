from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.time import utc_now
from app.models.entities import UserProfile


class UserProfileService:
    def __init__(self, db: Session):
        """保存支持背景读写所使用的数据库会话。

        初始化不自动创建用户背景。
        """
        self.db = db

    def get_profile(self, user_id: int) -> UserProfile | None:
        """查询指定用户已有的支持背景，缺失时返回 None。

        不创建记录，适合只读接口和对话上下文读取。
        """
        return self.db.query(UserProfile).filter(UserProfile.user_id == user_id).first()

    def get_or_create(self, user_id: int) -> UserProfile:
        """获取用户支持背景，尚无记录时创建并确认保存。

        返回已有或新建的数据库对象；调用它可能产生写入。
        """
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
        """按传入字段更新用户自愿提供的支持背景。

        参数为 None 表示不修改该字段，空白文本表示清空；更新时间后提交并刷新记录。
        """
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
        """将非空支持背景整理成带中文标签的多行文本。

        无记录时返回空字符串；只使用已保存的关注问题、目标和偏好，不推断其他信息。
        """
        profile = self.get_profile(user_id)
        if profile is None:
            return ""
        fields = [
            ("当前关注", profile.current_concern),
            ("支持目标", profile.support_goal),
            ("偏好方式", profile.preferred_support_style),
        ]
        return "\n".join(f"{label}：{value}" for label, value in fields if value)
