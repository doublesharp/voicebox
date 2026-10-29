"""Exercise legacy upgrades against real SQLite databases, including restarts."""

import json

import pytest
from sqlalchemy import create_engine, inspect, text

from backend.database import migrations
from backend.utils.capture_chords import default_push_to_talk_chord, default_toggle_to_talk_chord


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    yield engine
    engine.dispose()


def execute(engine, *statements):
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def rows(engine, sql):
    with engine.connect() as conn:
        return conn.execute(text(sql)).fetchall()


def test_old_position_stories_upgrade_in_order_and_survive_restart(engine):
    execute(engine,
        "CREATE TABLE generations (id TEXT PRIMARY KEY, duration FLOAT)",
        "INSERT INTO generations VALUES ('g1', 1.2), ('g2', 2.0), ('g3', NULL)",
        "CREATE TABLE story_items (id TEXT PRIMARY KEY, story_id TEXT NOT NULL, generation_id TEXT NOT NULL, position INTEGER, created_at DATETIME)",
        "INSERT INTO story_items VALUES ('a', 's1', 'g1', 1, '2024-01-01'), ('b', 's1', 'g2', 2, '2024-01-02'), ('c', 's2', 'g3', 1, '2024-01-03')",
    )
    for _ in range(2):
        migrations._migrate_story_items(engine, inspect(engine), {'story_items'})
    assert rows(engine, 'SELECT id, start_time_ms, track, trim_start_ms, trim_end_ms, volume FROM story_items ORDER BY id') == [
        ('a', 0, 0, 0, 0, 1.0), ('b', 1400, 0, 0, 0, 1.0), ('c', 0, 0, 0, 0, 1.0),
    ]
    assert 'position' not in {col['name'] for col in inspect(engine).get_columns('story_items')}


def test_position_rebuild_preserves_existing_edit_values(engine):
    execute(engine,
        "CREATE TABLE generations (id TEXT PRIMARY KEY, duration FLOAT)",
        "INSERT INTO generations VALUES ('g', 1)",
        "CREATE TABLE story_items (id TEXT PRIMARY KEY, story_id TEXT, generation_id TEXT, position INTEGER, start_time_ms INTEGER, track INTEGER, trim_start_ms INTEGER, trim_end_ms INTEGER, version_id TEXT, volume FLOAT, created_at DATETIME)",
        "INSERT INTO story_items VALUES ('a', 's', 'g', 1, 730, 2, 30, 80, 'v', 0.25, '2024-01-01')",
    )
    migrations._migrate_story_items(engine, inspect(engine), {'story_items'})
    assert rows(engine, 'SELECT start_time_ms, track, trim_start_ms, trim_end_ms, version_id, volume FROM story_items') == [(730, 2, 30, 80, 'v', 0.25)]


def test_existing_modern_story_rows_gain_edit_defaults(engine):
    execute(engine, "CREATE TABLE story_items (id TEXT PRIMARY KEY, start_time_ms INTEGER)", "INSERT INTO story_items VALUES ('a', 500)")
    for _ in range(2):
        migrations._migrate_story_items(engine, inspect(engine), {'story_items'})
    assert rows(engine, 'SELECT start_time_ms, track, trim_start_ms, trim_end_ms, version_id, volume FROM story_items') == [(500, 0, 0, 0, None, 1.0)]


def test_settings_upgrade_adds_defaults_without_overwriting_existing_rows(engine):
    execute(engine, 'CREATE TABLE capture_settings (id INTEGER PRIMARY KEY)', 'INSERT INTO capture_settings VALUES (1)')
    for _ in range(2):
        migrations._migrate_capture_settings(engine, inspect(engine), {'capture_settings'})
    result = rows(engine, 'SELECT allow_auto_paste, hotkey_enabled, chord_push_to_talk_keys, chord_toggle_to_talk_keys, custom_llm_endpoint FROM capture_settings')[0]
    assert result[:2] == (1, 0)
    assert json.loads(result[2]) == default_push_to_talk_chord()
    assert json.loads(result[3]) == default_toggle_to_talk_chord()
    assert result[4] is None


@pytest.mark.parametrize('drop_supported', [True, False])
def test_mcp_binding_upgrade_retains_data_and_handles_old_sqlite(engine, monkeypatch, drop_supported):
    execute(engine, 'CREATE TABLE mcp_client_bindings (client_id TEXT PRIMARY KEY, default_intent TEXT)', "INSERT INTO mcp_client_bindings VALUES ('agent', 'respond')")
    monkeypatch.setattr(migrations, '_supports_drop_column', lambda _: drop_supported)
    for _ in range(2):
        migrations._migrate_mcp_bindings(engine, inspect(engine), {'mcp_client_bindings'})
    assert rows(engine, 'SELECT client_id, default_personality FROM mcp_client_bindings') == [('agent', 0)]
    assert ('default_intent' in {c['name'] for c in inspect(engine).get_columns('mcp_client_bindings')}) is not drop_supported


def test_profile_preset_and_version_migrations_preserve_rows(engine):
    execute(engine, 'CREATE TABLE profiles (id TEXT PRIMARY KEY)', "INSERT INTO profiles VALUES ('p')", 'CREATE TABLE effect_presets (id TEXT PRIMARY KEY)', "INSERT INTO effect_presets VALUES ('e')", 'CREATE TABLE generation_versions (id TEXT PRIMARY KEY)', "INSERT INTO generation_versions VALUES ('v')")
    for _ in range(2):
        inspector = inspect(engine)
        migrations._migrate_profiles(engine, inspector, {'profiles'})
        migrations._migrate_effect_presets(engine, inspector, {'effect_presets'})
        migrations._migrate_generation_versions(engine, inspector, {'generation_versions'})
    assert rows(engine, 'SELECT id, voice_type, avatar_path, effects_chain, default_engine FROM profiles') == [('p', 'cloned', None, None, None)]
    assert rows(engine, 'SELECT id, sort_order FROM effect_presets') == [('e', 100)]
    assert rows(engine, 'SELECT id, source_version_id FROM generation_versions') == [('v', None)]


def test_startup_migrations_normalize_paths_and_are_repeatable(engine, tmp_path, monkeypatch):
    from backend import config
    from backend.database.models import Base

    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    monkeypatch.setattr(config, '_data_dir', data_dir)
    Base.metadata.create_all(engine)
    absolute = str(data_dir / 'generations' / 'voice.wav')
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO generations (id, profile_id, text, audio_path, source) VALUES ('g', 'p', 'hello', :path, 'manual')"), {'path': absolute})
        conn.execute(text("INSERT INTO profiles (id, name, avatar_path) VALUES ('p', 'voice', :path)"), {'path': str(data_dir / 'profiles' / 'avatar.png')})
    for _ in range(2):
        migrations.run_migrations(engine)
    assert rows(engine, "SELECT audio_path FROM generations WHERE id='g'") == [('generations/voice.wav',)]
    assert rows(engine, "SELECT avatar_path FROM profiles WHERE id='p'") == [('profiles/avatar.png',)]
    names = {index['name'] for index in inspect(engine).get_indexes('generations')}
    assert {'ix_generations_cursor', 'ix_generations_profile_cursor'} <= names
