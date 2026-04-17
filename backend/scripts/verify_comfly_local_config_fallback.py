from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api import comfly_veo
from backend.app.db import Base, get_db
from backend.app.models import UserComflyConfig


engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


async def reject_auth(*args, **kwargs):
    raise HTTPException(status_code=401, detail="expired")


def make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(comfly_veo.router)
    app.dependency_overrides[get_db] = override_get_db
    comfly_veo.get_current_user_for_local = reject_auth
    return app


def main() -> None:
    app = make_app()

    local = TestClient(app, base_url="http://127.0.0.1:8000")
    r = local.post(
        "/api/comfly/config",
        json={"api_key": "sk-local-test"},
        headers={"Authorization": "Bearer stale-token"},
    )
    assert r.status_code == 200, r.text

    db = TestingSessionLocal()
    try:
        row = (
            db.query(UserComflyConfig)
            .filter(UserComflyConfig.user_id == comfly_veo.LOCAL_COMFLY_CONFIG_USER_ID)
            .first()
        )
        assert row is not None
        assert row.api_key == "sk-local-test"
    finally:
        db.close()

    r = local.get("/api/comfly/config")
    assert r.status_code == 200, r.text
    assert r.json()["effective_ready"] is True

    remote = TestClient(app, base_url="http://example.com")
    r = remote.post(
        "/api/comfly/config",
        json={"api_key": "sk-should-not-save"},
        headers={"Authorization": "Bearer stale-token"},
    )
    assert r.status_code == 401, r.text

    print("OK: Comfly config local fallback works and remains local-only.")


if __name__ == "__main__":
    main()
