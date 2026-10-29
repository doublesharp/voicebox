"""History pagination keeps a stable boundary when newer generations arrive."""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database.models import Base, Generation, VoiceProfile
from backend.models import HistoryQuery
from backend.services.history import list_generations


@pytest.fixture
def database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'history.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(VoiceProfile(id="voice", name="Voice", language="en"))
        session.add_all(
            [
                Generation(
                    id=f"g{index}", profile_id="voice", text=f"Speech {index}", created_at=datetime(2026, 10, 29)
                )
                for index in range(6)
            ]
        )
        session.commit()
        yield session
    engine.dispose()


@pytest.mark.asyncio
async def test_cursor_pages_keep_ties_stable_and_ignore_new_head_rows(database):
    first = await list_generations(HistoryQuery(limit=2), database)
    assert [item.id for item in first.items] == ["g5", "g4"]
    assert first.next_cursor
    database.add(Generation(id="new", profile_id="voice", text="New", created_at=datetime(2026, 10, 30)))
    database.commit()
    second = await list_generations(HistoryQuery(limit=2, cursor=first.next_cursor, include_total=False), database)
    assert [item.id for item in second.items] == ["g3", "g2"]
    assert second.total is None
    third = await list_generations(HistoryQuery(limit=2, cursor=second.next_cursor), database)
    assert [item.id for item in third.items] == ["g1", "g0"]
    assert third.next_cursor is None
    assert third.total == 7


@pytest.mark.asyncio
async def test_cursor_rejects_invalid_and_mixed_offset_boundaries(database):
    with pytest.raises(ValueError, match="cursor"):
        await list_generations(HistoryQuery(cursor="broken"), database)
    first = await list_generations(HistoryQuery(limit=2), database)
    with pytest.raises(ValueError, match="offset"):
        await list_generations(HistoryQuery(cursor=first.next_cursor, offset=2), database)


def test_existing_database_receives_compound_cursor_indexes(tmp_path):
    from sqlalchemy import inspect, text

    from backend.database.migrations import _migrate_add_indexes

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite'}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE generations (id TEXT PRIMARY KEY, profile_id TEXT, created_at DATETIME, status TEXT)"
                )
            )
        _migrate_add_indexes(engine, {"generations"})
        indexes = {index["name"]: index["column_names"] for index in inspect(engine).get_indexes("generations")}
        assert indexes["ix_generations_cursor"] == ["created_at", "id"]
        assert indexes["ix_generations_profile_cursor"] == ["profile_id", "created_at", "id"]
        with engine.connect() as connection:
            plan = connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT id FROM generations WHERE (created_at,id) < (?,?) ORDER BY created_at DESC,id DESC LIMIT 20",
                ("2026-10-29", "last"),
            ).fetchall()
        assert any("SEARCH" in row[3] and "ix_generations_cursor" in row[3] for row in plan)
    finally:
        engine.dispose()


def test_history_http_cursor_contract(database):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.database import get_db
    from backend.routes.history import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: database
    with TestClient(app) as client:
        first = client.get("/history", params={"limit": 2}).json()
        response = client.get("/history", params={"limit": 2, "cursor": first["next_cursor"], "include_total": "false"})
        assert response.status_code == 200
        assert response.json()["total"] is None
        assert [item["id"] for item in response.json()["items"]] == ["g3", "g2"]
        assert client.get("/history", params={"cursor": "broken"}).status_code == 400
        assert client.get("/history", params={"cursor": first["next_cursor"], "offset": 2}).status_code == 400
        assert client.get("/history", params={"limit": 0}).status_code == 422
