from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from datetime import datetime

from app.core.time import utc_isoformat


@dataclass(frozen=True)
class SupportEvent:
    event_type: str
    review_id: int
    session_id: str
    support_status: str
    message_id: int | None = None
    role: str | None = None
    content: str | None = None
    created_at: datetime | None = None
    review: dict | None = None

    def payload(self) -> dict:
        return {
            "type": self.event_type,
            "reviewId": self.review_id,
            "sessionId": self.session_id,
            "supportStatus": self.support_status,
            "messageId": self.message_id,
            "role": self.role,
            "content": self.content,
            "createdAt": utc_isoformat(self.created_at) if self.created_at else None,
            "review": self.review,
        }


@dataclass(eq=False)
class Subscription:
    queue: asyncio.Queue
    loop: asyncio.AbstractEventLoop
    admin: bool
    session_id: str | None
    active: bool = True


class SupportEventBroker:
    def __init__(self, queue_size: int = 100):
        self.queue_size = queue_size
        self._subscriptions: set[Subscription] = set()
        self._lock = threading.Lock()

    def subscribe(self, *, admin: bool, session_id: str | None = None) -> Subscription:
        subscription = Subscription(
            queue=asyncio.Queue(maxsize=self.queue_size),
            loop=asyncio.get_running_loop(),
            admin=admin,
            session_id=session_id,
        )
        with self._lock:
            self._subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        subscription.active = False
        with self._lock:
            self._subscriptions.discard(subscription)

    def publish(self, event: SupportEvent) -> None:
        with self._lock:
            subscriptions = list(self._subscriptions)
        for subscription in subscriptions:
            if not subscription.active:
                continue
            if not subscription.admin and (
                subscription.session_id != event.session_id
                or event.event_type not in {"support_message", "status_changed"}
            ):
                continue
            try:
                subscription.loop.call_soon_threadsafe(self._offer, subscription, event)
            except RuntimeError:
                self.unsubscribe(subscription)

    async def stream(self, subscription: Subscription, heartbeat_seconds: float = 15.0):
        try:
            while subscription.active:
                try:
                    event = await asyncio.wait_for(
                        subscription.queue.get(), timeout=heartbeat_seconds,
                    )
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield (
                    f"event: {event.event_type}\n"
                    f"data: {json.dumps(event.payload(), ensure_ascii=False, default=str)}\n\n"
                )
        finally:
            self.unsubscribe(subscription)

    def _offer(self, subscription: Subscription, event: SupportEvent) -> None:
        if not subscription.active:
            return
        if subscription.queue.full():
            while not subscription.queue.empty():
                subscription.queue.get_nowait()
            event = SupportEvent(
                event_type="resync_required",
                review_id=event.review_id,
                session_id=event.session_id,
                support_status=event.support_status,
            )
        subscription.queue.put_nowait(event)


support_events = SupportEventBroker()
