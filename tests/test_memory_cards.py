"""Tests for issue 04: Memory cards + no-memory session.

Covers:
  - MemoryCardService CRUD (create, list, update, delete)
  - Suggestion + confirmation flow (unconfirmed not in context)
  - User isolation
  - API endpoints (GET/POST/PUT/DELETE/confirm)
  - Confirmed context for agent runtime

Run:  python tests/test_memory_cards.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.api.routes import router
from app.core.database import Base, get_db
from app.core.security import hash_password
from app.models.entities import UserAccount, MemoryCard
from app.services.memory_cards import MemoryCardService


_test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
_TestSession = sessionmaker(bind=_test_engine, autoflush=False, autocommit=False)

def _test_get_db():
    db = _TestSession()
    try:
        yield db
    finally:
        db.close()

app = FastAPI()
app.include_router(router)
app.dependency_overrides[get_db] = _test_get_db
Base.metadata.create_all(bind=_test_engine)

def _seed():
    db = _TestSession()
    try:
        s1 = UserAccount(username="student", display_name="S1", password_hash=hash_password("student123"))
        s1.roles = {"ROLE_USER"}
        s2 = UserAccount(username="student2", display_name="S2", password_hash=hash_password("pass2"))
        s2.roles = {"ROLE_USER"}
        db.add_all([s1, s2])
        db.commit()
    finally:
        db.close()

_seed()
client = TestClient(app)

def _token(u="student", p="student123"):
    r = client.post("/api/auth/login", json={"username": u, "password": p})
    return r.json()["accessToken"]

def _auth(t):
    return {"Authorization": f"Bearer {t}"}

def _clean():
    db = _TestSession()
    try:
        db.query(MemoryCard).delete()
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Service: CRUD
# ---------------------------------------------------------------------------

def test_create_card():
    _clean()
    db = _TestSession()
    try:
        svc = MemoryCardService(db)
        card = svc.create_card(1, "和室友关系紧张影响复习")
        assert card.id is not None
        assert card.confirmed is True
        assert card.content == "和室友关系紧张影响复习"
    finally:
        db.close()

def test_list_cards():
    _clean()
    db = _TestSession()
    try:
        svc = MemoryCardService(db)
        svc.create_card(1, "card 1")
        svc.create_card(1, "card 2")
        cards = svc.list_cards(1)
        assert len(cards) == 2
    finally:
        db.close()

def test_update_card():
    _clean()
    db = _TestSession()
    try:
        svc = MemoryCardService(db)
        card = svc.create_card(1, "original")
        updated = svc.update_card(1, card.id, "updated content")
        assert updated.content == "updated content"
    finally:
        db.close()

def test_delete_card():
    _clean()
    db = _TestSession()
    try:
        svc = MemoryCardService(db)
        card = svc.create_card(1, "to delete")
        svc.delete_card(1, card.id)
        assert len(svc.list_cards(1)) == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Suggestion + confirmation
# ---------------------------------------------------------------------------

def test_suggested_card_not_in_context():
    _clean()
    db = _TestSession()
    try:
        svc = MemoryCardService(db)
        svc.suggest_card(1, "system suggestion")
        # Unconfirmed cards should not appear in context
        assert svc.get_confirmed_context(1) == ""
        # But should appear in list with include_pending
        all_cards = svc.list_cards(1)
        assert len(all_cards) == 1
        assert all_cards[0].confirmed is False
    finally:
        db.close()

def test_confirm_card_adds_to_context():
    _clean()
    db = _TestSession()
    try:
        svc = MemoryCardService(db)
        card = svc.suggest_card(1, "important context")
        assert svc.get_confirmed_context(1) == ""
        svc.confirm_card(1, card.id)
        context = svc.get_confirmed_context(1)
        assert "important context" in context
    finally:
        db.close()


# ---------------------------------------------------------------------------
# User isolation
# ---------------------------------------------------------------------------

def test_user_isolation():
    _clean()
    db = _TestSession()
    try:
        svc = MemoryCardService(db)
        svc.create_card(1, "user 1 card")
        svc.create_card(2, "user 2 card")
        assert len(svc.list_cards(1)) == 1
        assert len(svc.list_cards(2)) == 1
        # User 1 can't access user 2's card
        try:
            svc.update_card(1, svc.list_cards(2)[0].id, "hacked")
            assert False, "Should have raised"
        except ValueError:
            pass
    finally:
        db.close()


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

def test_api_create_and_list():
    _clean()
    t = _token()
    r = client.post("/api/memory-cards", json={"content": "API card"}, headers=_auth(t))
    assert r.status_code == 200
    r = client.get("/api/memory-cards", headers=_auth(t))
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["content"] == "API card"

def test_api_update():
    _clean()
    t = _token()
    r = client.post("/api/memory-cards", json={"content": "original"}, headers=_auth(t))
    card_id = r.json()["id"]
    r = client.put(f"/api/memory-cards/{card_id}", json={"content": "updated"}, headers=_auth(t))
    assert r.status_code == 200
    assert r.json()["content"] == "updated"

def test_api_delete():
    _clean()
    t = _token()
    r = client.post("/api/memory-cards", json={"content": "to delete"}, headers=_auth(t))
    card_id = r.json()["id"]
    r = client.delete(f"/api/memory-cards/{card_id}", headers=_auth(t))
    assert r.status_code == 200
    r = client.get("/api/memory-cards", headers=_auth(t))
    assert len(r.json()) == 0

def test_api_confirm():
    _clean()
    db = _TestSession()
    try:
        svc = MemoryCardService(db)
        card = svc.suggest_card(1, "needs confirmation")
        card_id = card.id
    finally:
        db.close()
    t = _token()
    r = client.post(f"/api/memory-cards/{card_id}/confirm", headers=_auth(t))
    assert r.status_code == 200
    assert r.json()["confirmed"] is True

def test_api_user_isolation():
    _clean()
    t1 = _token("student", "student123")
    t2 = _token("student2", "pass2")
    client.post("/api/memory-cards", json={"content": "user1"}, headers=_auth(t1))
    r = client.get("/api/memory-cards", headers=_auth(t2))
    assert len(r.json()) == 0

def test_api_requires_auth():
    r = client.get("/api/memory-cards")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    passed = 0
    failed = 0
    for test in _TESTS:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL  {test.__name__}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR {test.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(_TESTS)} total")
    sys.exit(1 if failed else 0)
