"""Production environment verification tests.

Validates security parameters, rate limiting behavior, sanitization logic,
and various error boundary cases for production.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.context_engine import (
    _live_turnstile_overrides,
    _pseudo_multiplier,
    best_gate_live,
    resolve_live,
    update_live_turnstile,
)
from app.core.llm import MockLLM, get_llm
from app.core.security import sanitize_text
from app.main import app


def test_production_features():
    client = TestClient(app)

    # 1. Test SQL and Shell Command Injection Sanitization
    dirty_text = "how is gate B; -- rm -rf /; | cat /etc/passwd"
    clean_text = sanitize_text(dirty_text)
    assert ";" not in clean_text
    assert "--" not in clean_text
    assert "|" not in clean_text
    assert "&" not in clean_text
    assert "$" not in clean_text
    assert "`" not in clean_text
    assert "rm -rf" in clean_text

    # 2. Test Multiplier Caching (lru_cache)
    _pseudo_multiplier.cache_clear()
    val1 = _pseudo_multiplier("A", 12345)
    val2 = _pseudo_multiplier("A", 12345)
    assert val1 == val2
    assert _pseudo_multiplier.cache_info().hits >= 1

    # 3. Test IoT Live Turnstile Override POST Endpoint
    _live_turnstile_overrides.clear()
    update_payload = {
        "gate_id": "B",
        "arrivals_per_min": 10.5,
        "capacity_per_min": 2.5,
        "servers_open": 5,
    }
    update_resp = client.post("/api/ops/gate-update", json=update_payload)
    assert update_resp.status_code == 200
    assert update_resp.json()["status"] == "success"

    snapshot_resp = client.get("/api/ops/snapshot")
    assert snapshot_resp.status_code == 200
    gates = {g["gate_id"]: g for g in snapshot_resp.json()["gates"]}
    assert gates["B"]["utilization"] == 0.84

    # 4. Test Server-Sent Events (SSE) Live Stream Endpoint
    with client.stream("GET", "/api/ops/live") as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        for line in response.iter_lines():
            if isinstance(line, bytes):
                line = line.decode("utf-8")
            if line.startswith("data: "):
                event_data = json.loads(line[6:])
                assert "gates" in event_data
                assert "critical_count" in event_data
                break

    # 5. Test Payload Size Limiter (DoS protection)
    large_payload = {"persona": "fan", "language": "en", "raw_text": "A" * 150000}
    large_resp = client.post("/api/assist", json=large_payload)
    assert large_resp.status_code == 413
    assert "Request entity too large" in large_resp.json()["detail"]

    # Invalid content-length header test
    bad_headers = {"content-length": "notanumber"}
    bad_len_resp = client.post(
        "/api/assist",
        json={"persona": "fan", "language": "en", "raw_text": "hello"},
        headers=bad_headers,
    )
    assert bad_len_resp.status_code == 400
    assert "Invalid content-length" in bad_len_resp.json()["detail"]

    # 6. Test Model Wait Ceiling Clamp
    from app.core.context_engine import predict_wait
    from app.core.schemas import GateStatus

    overloaded_gate = GateStatus(
        gate_id="X",
        name="Overloaded Gate",
        capacity_per_min=1.0,
        arrivals_per_min=100.0,
        servers_open=1,
    )
    est = predict_wait(overloaded_gate)
    assert est.predicted_wait_minutes == 99.0
    assert est.congestion_level == "critical"

    # 7. Test HTML Views Coverage
    home_resp = client.get("/")
    assert home_resp.status_code == 200
    assert "Phoenix Stadium" in home_resp.text

    ops_resp = client.get("/ops")
    assert ops_resp.status_code == 200
    assert "Ops Dashboard" in ops_resp.text

    # 8. Test Schema Field Validator for Negative Arrivals
    from pydantic import ValidationError

    from app.core.schemas import GateStatus as _GateStatus

    with pytest.raises(ValidationError):
        _GateStatus(
            gate_id="ERR",
            name="Error Gate",
            capacity_per_min=2.0,
            arrivals_per_min=-5.0,
            servers_open=2,
        )

    # 9. Test Invalid Gate ID on turnstile update
    with pytest.raises(ValueError, match="Invalid gate ID"):
        update_live_turnstile("INVALID_ID", arrivals_per_min=10.0)

    # 10. Test invalid gate update payload through API
    bad_update = {"gate_id": "INVALID_ID", "arrivals_per_min": 10.0}
    bad_update_resp = client.post("/api/ops/gate-update", json=bad_update)
    assert bad_update_resp.status_code == 400
    assert "Invalid gate ID" in bad_update_resp.json()["detail"]

    # 11. Test Wheelchair Requested Gate fallback inside resolve_live
    from app.core.schemas import AccessibilityNeed, Language, Persona, UserQuery

    wheelchair_q = UserQuery(
        persona=Persona.FAN,
        language=Language.EN,
        raw_text="gate B please",
        accessibility_need=AccessibilityNeed.WHEELCHAIR,
    )
    resolved_ctx = resolve_live(wheelchair_q, "gate B please")
    assert resolved_ctx.recommended_gate.step_free is True
    assert resolved_ctx.recommended_gate.gate_id != "B"

    # 12. Test get_llm Gemini initialization failure fallback
    with patch.dict(
        os.environ,
        {"PHOENIX_STADIUM_LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "dummy_key"},
    ):
        with patch("app.core.llm.GeminiLLM", side_effect=Exception("Initialization failed")):
            llm_instance = get_llm()
            assert isinstance(llm_instance, MockLLM)

    # 13. Test incident closed gate filtering and safety alert phrasing
    _live_turnstile_overrides.clear()
    update_live_turnstile("A", incident="CLOSED due to safety hazard")

    best_gate_obj, _, _ = best_gate_live(AccessibilityNeed.NONE)
    assert best_gate_obj.gate_id != "A"

    update_live_turnstile("B", incident="Suspicious bag near entry")
    q_incident = UserQuery(
        persona=Persona.FAN,
        language=Language.EN,
        raw_text="how is gate B doing",
        accessibility_need=AccessibilityNeed.NONE,
    )
    ctx_incident = resolve_live(q_incident, "how is gate B doing")
    assert ctx_incident.safety_notice == "ALERT for Gate B — East: Suspicious bag near entry"

    reply_incident = MockLLM().phrase(ctx_incident)
    assert reply_incident.text.startswith("[ALERT for Gate B — East: Suspicious bag near entry]")

    # 14. Test Ops Briefing API Endpoint
    briefing_resp = client.post("/api/ops/briefing")
    assert briefing_resp.status_code == 200
    briefing_data = briefing_resp.json()
    assert "briefing" in briefing_data
    assert "generated_at" in briefing_data
    assert "snapshot_summary" in briefing_data
    assert briefing_data["snapshot_summary"]["total_gates"] > 0

    # 15. Test Health & Healthz Check endpoints
    health_resp = client.get("/health")
    assert health_resp.status_code == 200
    assert health_resp.json() == {"status": "ok"}

    healthz_resp = client.get("/healthz")
    assert healthz_resp.status_code == 200
    assert healthz_resp.json() == {"status": "ok"}
