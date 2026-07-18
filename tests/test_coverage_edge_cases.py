"""Coverage and edge case tests.

Verifies edge cases like missing configuration fallbacks,
lifespan startup/shutdown errors, and other error boundary scenarios.
"""

from __future__ import annotations

import importlib
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from app.core.context_engine import (
    GATES,
    _live_turnstile_overrides,
    best_gate_live,
    resolve_live,
)
from app.core.schemas import AccessibilityNeed, Language, Persona, UserQuery
from app.main import app, lifespan


@pytest.mark.anyio
async def test_lifespan_startup_and_shutdown():
    """Verify that lifespan log statements are triggered on startup and shutdown."""
    async with lifespan(app):
        pass


def test_best_gate_live_candidates_empty_fallback():
    """Ensure best_gate_live falls back to all gates if all candidates are filtered out."""
    _live_turnstile_overrides.clear()
    try:
        for gate_id in GATES.keys():
            _live_turnstile_overrides[gate_id] = {"incident": "gate closed for maintenance"}
        best, best_wait, alt = best_gate_live(AccessibilityNeed.WHEELCHAIR)
        assert best is not None
        assert best.gate_id in GATES
    finally:
        _live_turnstile_overrides.clear()


def test_limiter_with_redis():
    """Test that app.core.limiter instantiates Limiter with storage_uri when
    REDIS_URL is set."""
    import app.core.limiter as limiter_mod

    with patch.dict(os.environ, {"REDIS_URL": "redis://127.0.0.1:6379/0"}):
        with patch("slowapi.Limiter") as mock_limiter_cls:
            mock_limiter_cls.return_value = MagicMock()
            importlib.reload(limiter_mod)
            mock_limiter_cls.assert_called_once()
            _, kwargs = mock_limiter_cls.call_args
            assert kwargs.get("storage_uri") == "redis://127.0.0.1:6379/0"

    importlib.reload(limiter_mod)


def test_resolve_live_inaccessible_gate_wheelchair():
    """Test resolve_live where a wheelchair user requests a non-step-free gate."""
    q = UserQuery(
        persona=Persona.FAN,
        language=Language.EN,
        raw_text="give me Gate B",
        accessibility_need=AccessibilityNeed.WHEELCHAIR,
    )
    ctx = resolve_live(q, "give me Gate B")
    assert ctx.recommended_gate.step_free is True
    assert ctx.recommended_gate.gate_id != "B"
    assert ctx.accessible_route_available is True


def test_overrides_persistence_round_trip(tmp_path):
    """Verify that _save_overrides + _load_overrides round-trip correctly."""
    import app.core.context_engine as ce
    from app.core.context_engine import _load_overrides, _save_overrides

    original = ce.OVERRIDES_FILE
    temp_file = str(tmp_path / "test_overrides.json")
    ce.OVERRIDES_FILE = temp_file
    try:
        test_data = {"A": {"arrivals_per_min": 5.5, "incident": "test"}}
        _save_overrides(test_data)
        loaded = _load_overrides()
        assert loaded == test_data
    finally:
        ce.OVERRIDES_FILE = original


def test_overrides_load_malformed_json_falls_back(tmp_path):
    """_load_overrides must return {} gracefully when the file is corrupted JSON."""
    import app.core.context_engine as ce

    original = ce.OVERRIDES_FILE
    temp_file = str(tmp_path / "bad_overrides.json")
    ce.OVERRIDES_FILE = temp_file
    try:
        with open(temp_file, "w") as f:
            f.write("{this is not json}")
        result = ce._load_overrides()
        assert result == {}
    finally:
        ce.OVERRIDES_FILE = original


def test_overrides_atomic_write_no_leftover_temp(tmp_path):
    """Atomic write (temp + os.replace) must leave no .tmp file behind."""
    import app.core.context_engine as ce

    original = ce.OVERRIDES_FILE
    temp_file = str(tmp_path / "overrides_db.json")
    ce.OVERRIDES_FILE = temp_file
    try:
        ce._save_overrides({"B": {"servers_open": 3}})
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0
        with open(temp_file) as f:
            data = json.load(f)
        assert data == {"B": {"servers_open": 3}}
    finally:
        ce.OVERRIDES_FILE = original
