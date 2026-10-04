from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.entities import MemoryCard


class MemoryCardService:
    """管理用户可查看、确认、修改和删除的长期记忆卡片。"""

    def __init__(self, db: Session):
        """保存记忆卡片服务使用的数据库会话。

        初始化本身不读取或创建卡片。
        """
        self.db = db

    def list_cards(self, user_id: int, include_pending: bool = True) -> list[MemoryCard]:
        """按创建时间倒序列出指定用户的卡片。

        include_pending 默认为 True，包含待确认建议；为 False 时仅返回已确认卡片。
        """
        query = self.db.query(MemoryCard).filter(MemoryCard.user_id == user_id)
        if not include_pending:
            query = query.filter(MemoryCard.confirmed == True)  # noqa: E712
        return query.order_by(MemoryCard.created_at.desc()).all()

    def list_confirmed(self, user_id: int) -> list[MemoryCard]:
        """查询可作为长期记忆使用的已确认卡片。

        委托列表查询添加确认状态过滤，不将待确认建议加入对话背景。
        """
        return self.list_cards(user_id, include_pending=False)

    def create_card(self, user_id: int, content: str, source: str = "user") -> MemoryCard:
        """保存一条已确认的记忆卡片。

        user_id 指定归属，content 去除首尾空白，source 默认表示用户创建；提交后返回刷新对象。
        """
        card = MemoryCard(
            user_id=user_id,
            content=content.strip(),
            source=source,
            confirmed=True,
        )
        self.db.add(card)
        self.db.commit()
        self.db.refresh(card)
        return card

    def suggest_card(self, user_id: int, content: str) -> MemoryCard:
        """保存一条来源为系统、尚未确认的卡片建议。

        建议会出现在管理列表中，但只有用户确认后才进入已确认的对话背景。
        """
        card = MemoryCard(
            user_id=user_id,
            content=content.strip(),
            source="system",
            confirmed=False,
        )
        self.db.add(card)
        self.db.commit()
        self.db.refresh(card)
        return card

    def confirm_card(self, user_id: int, card_id: int) -> MemoryCard:
        """校验归属后把卡片标记为已确认并保存。

        不存在或属于其他用户时抛出 ValueError；成功返回刷新后的卡片。
        """
        card = self._get_owned_card(user_id, card_id)
        card.confirmed = True
        self.db.commit()
        self.db.refresh(card)
        return card

    def update_card(self, user_id: int, card_id: int, content: str) -> MemoryCard:
        """校验归属后更新卡片内容并保存。

        去除新内容首尾空白，保持卡片来源和确认状态不变。
        """
        card = self._get_owned_card(user_id, card_id)
        card.content = content.strip()
        self.db.commit()
        self.db.refresh(card)
        return card

    def delete_card(self, user_id: int, card_id: int) -> None:
        """校验归属后删除一条卡片并确认保存。

        成功不返回对象；无权访问或不存在时由归属检查抛错。
        """
        card = self._get_owned_card(user_id, card_id)
        self.db.delete(card)
        self.db.commit()

    def get_confirmed_context(self, user_id: int) -> str:
        """将已确认卡片整理成供对话执行器使用的分行背景文本。

        没有卡片时返回空字符串；不会包含尚待确认的系统建议。
        """
        cards = self.list_confirmed(user_id)
        if not cards:
            return ""
        return "\n".join(f"- {card.content}" for card in cards)

    def _get_owned_card(self, user_id: int, card_id: int) -> MemoryCard:
        """读取卡片并验证它属于指定用户。

        不存在和归属不符统一抛出 ValueError，避免服务上层误用他人记录。
        """
        card = self.db.get(MemoryCard, card_id)
        if card is None or card.user_id != user_id:
            raise ValueError("Card not found or not owned by user")
        return card
