from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.entities import MemoryCard


class MemoryCardService:
    """User-visible, editable long-term memory cards (issue 04)."""

    def __init__(self, db: Session):
        self.db = db

    def list_cards(self, user_id: int, include_pending: bool = True) -> list[MemoryCard]:
        query = self.db.query(MemoryCard).filter(MemoryCard.user_id == user_id)
        if not include_pending:
            query = query.filter(MemoryCard.confirmed == True)  # noqa: E712
        return query.order_by(MemoryCard.created_at.desc()).all()

    def list_confirmed(self, user_id: int) -> list[MemoryCard]:
        return self.list_cards(user_id, include_pending=False)

    def create_card(self, user_id: int, content: str, source: str = "user") -> MemoryCard:
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
        """Create an unconfirmed card (system suggestion). Not visible in context until confirmed."""
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
        card = self._get_owned_card(user_id, card_id)
        card.confirmed = True
        self.db.commit()
        self.db.refresh(card)
        return card

    def update_card(self, user_id: int, card_id: int, content: str) -> MemoryCard:
        card = self._get_owned_card(user_id, card_id)
        card.content = content.strip()
        self.db.commit()
        self.db.refresh(card)
        return card

    def delete_card(self, user_id: int, card_id: int) -> None:
        card = self._get_owned_card(user_id, card_id)
        self.db.delete(card)
        self.db.commit()

    def get_confirmed_context(self, user_id: int) -> str:
        """Return confirmed cards as a context string for agent runtime."""
        cards = self.list_confirmed(user_id)
        if not cards:
            return ""
        return "\n".join(f"- {card.content}" for card in cards)

    def _get_owned_card(self, user_id: int, card_id: int) -> MemoryCard:
        card = self.db.get(MemoryCard, card_id)
        if card is None or card.user_id != user_id:
            raise ValueError("Card not found or not owned by user")
        return card
