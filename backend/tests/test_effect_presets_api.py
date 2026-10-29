"""Effect preset operations through HTTP and real SQLite sessions."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.database import Base, EffectPreset, get_db
from backend.models import EffectConfig, EffectPresetUpdate
from backend.routes import effects as routes
from backend.services import effects as service
from backend.utils.effects import validate_effects_chain


@pytest.fixture
def database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def client(database):
    def session():
        with Session(database) as db:
            yield db

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = session
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_preset_lifecycle_and_duplicate_rename(client):
    first = client.post("/effects/presets", json={"name": "Voice", "effects_chain": []}).json()
    second = client.post("/effects/presets", json={"name": "Narration", "effects_chain": []}).json()
    duplicate = client.put(f"/effects/presets/{second['id']}", json={"name": "Voice", "description": "changed"})
    assert duplicate.status_code == 400
    assert client.get(f"/effects/presets/{second['id']}").json()["name"] == "Narration"
    assert client.get(f"/effects/presets/{second['id']}").json()["description"] is None
    assert client.put(f"/effects/presets/{second['id']}", json={"name": "New name"}).status_code == 200
    assert {preset["name"] for preset in client.get("/effects/presets").json()} == {"Voice", "New name"}
    assert client.delete(f"/effects/presets/{first['id']}").status_code == 200
    assert client.get(f"/effects/presets/{first['id']}").status_code == 404


def test_invalid_chain_does_not_leave_pending_preset_mutations(database):
    with Session(database) as db:
        db.add(EffectPreset(id="one", name="Original", effects_chain="[]", is_builtin=False))
        db.commit()
        with pytest.raises(ValueError, match="Unknown effect"):
            service.update_preset(
                "one", EffectPresetUpdate(name="Changed", effects_chain=[EffectConfig(type="missing")]), db
            )
        # Reusing a session for later unrelated work must not commit rejected edits.
        db.commit()
    with Session(database) as db:
        assert db.get(EffectPreset, "one").name == "Original"


def test_builtin_and_missing_presets_are_protected(client, database):
    with Session(database) as db:
        db.add(EffectPreset(id="builtin", name="Built in", effects_chain="[]", is_builtin=True))
        db.commit()
    assert client.put("/effects/presets/builtin", json={"name": "Edited"}).status_code == 400
    assert client.delete("/effects/presets/builtin").status_code == 400
    assert client.put("/effects/presets/missing", json={"name": "Edited"}).status_code == 404
    assert client.delete("/effects/presets/missing").status_code == 404
    assert client.get("/effects/presets/builtin").json()["name"] == "Built in"


def test_duplicate_rename_race_rolls_back_and_reports_conflict(database, monkeypatch):
    with Session(database) as db:
        db.add(EffectPreset(id="one", name="Original", effects_chain="[]", is_builtin=False))
        db.commit()
        original_commit = db.commit

        def racing_commit():
            with Session(database) as other:
                other.add(EffectPreset(id="two", name="Taken", effects_chain="[]", is_builtin=False))
                other.commit()
            original_commit()

        monkeypatch.setattr(db, "commit", racing_commit)
        with pytest.raises(ValueError, match="already exists"):
            service.update_preset("one", EffectPresetUpdate(name="Taken"), db)
        assert db.get(EffectPreset, "one").name == "Original"
        assert db.get(EffectPreset, "two").name == "Taken"


@pytest.mark.parametrize("value", ["NaN", "true"])
def test_invalid_numeric_parameters_do_not_create_preset(client, value):
    response = client.post(
        "/effects/presets",
        content='{"name":"Bad","effects_chain":[{"type":"gain","params":{"gain_db":' + value + "}}]}",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert client.get("/effects/presets").json() == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True, False, 10**400])
def test_invalid_effect_numbers_are_rejected_at_shared_validation_boundary(value):
    assert validate_effects_chain([{"type": "gain", "params": {"gain_db": value}}]) is not None


@pytest.mark.parametrize("value", [-40, 0, 40.0])
def test_finite_effect_numbers_within_bounds_are_allowed(value):
    assert validate_effects_chain([{"type": "gain", "params": {"gain_db": value}}]) is None


def test_invalid_number_does_not_replace_existing_preset(client):
    created = client.post("/effects/presets", json={"name": "Original", "effects_chain": []}).json()
    response = client.put(
        f"/effects/presets/{created['id']}",
        content='{"name":"Changed","effects_chain":[{"type":"gain","params":{"gain_db":NaN}}]}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    saved = client.get(f"/effects/presets/{created['id']}").json()
    assert saved["name"] == "Original"
    assert saved["effects_chain"] == []
