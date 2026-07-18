"""Unit tests for the context and prediction engine.

Covers Erlang-C wait time predictions, intent routing rules,
accessibility need parsing, and venue configuration loaders.
"""

from __future__ import annotations

import json
import math
import os
import time as t
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.context_engine import (
    GATES,
    INTENT_KEYWORDS,
    _infer_accessibility_need,
    all_gate_predictions,
    best_gate,
    classify_intent,
    get_phase_multiplier,
    live_gate_snapshot,
    matchday_phase,
    predict_wait,
    resolve,
    resolve_live,
)
from app.core.llm import MockLLM
from app.core.schemas import (
    AccessibilityNeed,
    GateStatus,
    Language,
    Persona,
    ResolvedContext,
    UserQuery,
    WaitEstimate,
)
from app.main import app


def test_classify_intent_wait():
    assert classify_intent("how long is the queue at gate B") == "wait_time"


def test_classify_intent_accessibility():
    assert classify_intent("I need a wheelchair accessible entrance") == "accessibility"


def test_classify_intent_emergency():
    assert classify_intent("medical emergency near section 5") == "emergency"


def test_classify_intent_unmatched_falls_back():
    assert classify_intent("what a great evening for football") == "general_info"


def test_predict_wait_zero_arrivals_zero_wait():
    gate = GateStatus(
        gate_id="Z",
        name="Test",
        capacity_per_min=2.0,
        arrivals_per_min=0.0,
        servers_open=2,
    )
    est = predict_wait(gate)
    assert est.predicted_wait_minutes == 0.0
    assert est.congestion_level == "low"


def test_predict_wait_increases_with_load():
    low = GateStatus(
        gate_id="Z",
        name="Test",
        capacity_per_min=2.0,
        arrivals_per_min=1.0,
        servers_open=2,
    )
    high = GateStatus(
        gate_id="Z",
        name="Test",
        capacity_per_min=2.0,
        arrivals_per_min=3.6,
        servers_open=2,
    )
    assert predict_wait(high).predicted_wait_minutes > predict_wait(low).predicted_wait_minutes


def test_predict_wait_never_negative_or_nan():
    for gate in GATES.values():
        est = predict_wait(gate)
        assert est.predicted_wait_minutes >= 0
        assert not math.isnan(est.predicted_wait_minutes)


def test_best_gate_respects_wheelchair_filter():
    gate, _ = best_gate(AccessibilityNeed.WHEELCHAIR)
    assert gate.step_free is True


def test_best_gate_picks_lowest_wait_among_candidates():
    gate, est = best_gate(AccessibilityNeed.NONE)
    all_waits = [predict_wait(g).predicted_wait_minutes for g in GATES.values()]
    assert est.predicted_wait_minutes == min(all_waits)


def test_resolve_emergency_sets_safety_notice():
    q = UserQuery(persona=Persona.FAN, language=Language.EN, raw_text="medical emergency")
    ctx = resolve(q, "medical emergency")
    assert ctx.safety_notice is not None
    assert ctx.intent == "emergency"


def test_resolve_normal_query_has_grounded_gate_and_wait():
    q = UserQuery(persona=Persona.FAN, language=Language.EN, raw_text="which gate is fastest")
    ctx = resolve(q, "which gate is fastest")
    assert ctx.recommended_gate is not None
    assert ctx.wait_estimate is not None


def test_all_gate_predictions_covers_every_gate():
    preds = all_gate_predictions()
    assert {p.gate_id for p in preds} == set(GATES.keys())


def test_classify_intent_all_keywords():
    for intent, keywords in INTENT_KEYWORDS.items():
        for keyword in keywords:
            assert classify_intent(f"where is the {keyword}") == intent
            assert classify_intent(keyword) == intent


def test_predict_wait_zero_capacity_servers_open():
    # Test c * mu == 0 when servers_open = 0
    gate_c0 = GateStatus(
        gate_id="C0",
        name="Zero Servers",
        capacity_per_min=2.0,
        arrivals_per_min=1.0,
        servers_open=1,
    )
    gate_c0.servers_open = 0
    est_c0 = predict_wait(gate_c0)
    assert est_c0.utilization == 1.0

    # Test c * mu == 0 when capacity_per_min = 0.0
    gate_mu0 = GateStatus(
        gate_id="MU0",
        name="Zero Capacity",
        capacity_per_min=2.0,
        arrivals_per_min=1.0,
        servers_open=1,
    )
    gate_mu0.capacity_per_min = 0.0
    est_mu0 = predict_wait(gate_mu0)
    assert est_mu0.utilization == 1.0


def test_predict_wait_rho_clamp():
    # Set arrivals high so rho > 1.0
    gate = GateStatus(
        gate_id="CLAMP",
        name="Clamp Test",
        capacity_per_min=1.0,
        arrivals_per_min=10.0,
        servers_open=1,
    )
    est = predict_wait(gate)
    assert est.utilization == 0.999


def test_predict_wait_congestion_levels():
    gate_low = GateStatus(
        gate_id="LOW",
        name="Low Test",
        capacity_per_min=1.0,
        arrivals_per_min=0.4,
        servers_open=1,
    )
    assert predict_wait(gate_low).congestion_level == "low"

    gate_mod = GateStatus(
        gate_id="MOD",
        name="Moderate Test",
        capacity_per_min=1.0,
        arrivals_per_min=0.6,
        servers_open=1,
    )
    assert predict_wait(gate_mod).congestion_level == "moderate"

    gate_high = GateStatus(
        gate_id="HIGH",
        name="High Test",
        capacity_per_min=1.0,
        arrivals_per_min=0.8,
        servers_open=1,
    )
    assert predict_wait(gate_high).congestion_level == "high"

    gate_crit = GateStatus(
        gate_id="CRIT",
        name="Critical Test",
        capacity_per_min=1.0,
        arrivals_per_min=0.95,
        servers_open=1,
    )
    assert predict_wait(gate_crit).congestion_level == "critical"


def test_best_gate_empty_candidates():
    import app.core.context_engine as ce

    original_step_free = {g.gate_id: g.step_free for g in ce.GATES.values()}
    try:
        for g in ce.GATES.values():
            g.step_free = False
        gate, est = ce.best_gate(AccessibilityNeed.WHEELCHAIR)
        assert gate is not None
        assert gate.step_free is False
    finally:
        for gid, val in original_step_free.items():
            ce.GATES[gid].step_free = val


def test_mock_llm_various_intents_languages_and_empty_contexts():
    # Test unknown intent
    ctx_unknown = ResolvedContext(
        intent="unknown_intent",
        recommended_gate=None,
        wait_estimate=None,
        accessible_route_available=True,
        safety_notice=None,
        sanitized_user_text="hello",
        language=Language.EN,
    )
    reply = MockLLM().phrase(ctx_unknown)
    assert reply.intent == "unknown_intent"
    assert "the main concourse" in reply.text

    # Test other languages
    for lang in [Language.HI, Language.ES]:
        ctx_lang = ResolvedContext(
            intent="find_gate",
            recommended_gate=GateStatus(
                gate_id="A",
                name="Gate A",
                capacity_per_min=2.0,
                arrivals_per_min=1.0,
                servers_open=1,
            ),
            wait_estimate=WaitEstimate(
                gate_id="A",
                predicted_wait_minutes=5.0,
                utilization=0.5,
                congestion_level="low",
                server_farm_saturated=False,
            ),
            accessible_route_available=True,
            safety_notice=None,
            sanitized_user_text="find gate",
            language=lang,
        )
        reply_lang = MockLLM().phrase(ctx_lang)
        assert reply_lang.language == lang
        assert "Gate A" in reply_lang.text


def test_gemini_llm_mocked():
    from app.core.llm import MockLLM as _MockLLM
    from app.core.llm import get_llm

    with patch.dict("os.environ", {"PHOENIX_STADIUM_LLM_PROVIDER": "mock"}):
        assert isinstance(get_llm(), _MockLLM)

    with patch.dict(
        "os.environ",
        {
            "PHOENIX_STADIUM_LLM_PROVIDER": "gemini",
            "GEMINI_API_KEY": "",
        },
    ):
        assert isinstance(get_llm(), _MockLLM)

    with patch.dict(
        "os.environ",
        {
            "PHOENIX_STADIUM_LLM_PROVIDER": "gemini",
            "GEMINI_API_KEY": "test_api_key",
        },
    ):
        mock_gemini_instance = MagicMock()
        with patch("app.core.llm.GeminiLLM", return_value=mock_gemini_instance) as mock_cls:
            llm = get_llm()
            mock_cls.assert_called_once()
            assert llm is mock_gemini_instance


def test_gemini_llm_class_directly():
    mock_genai = MagicMock()
    mock_google = MagicMock()
    mock_google.generativeai = mock_genai
    with patch.dict(
        "sys.modules",
        {
            "google": mock_google,
            "google.generativeai": mock_genai,
        },
    ):
        from app.core.llm import GeminiLLM

        with patch.dict("os.environ", {"GEMINI_API_KEY": "test_api_key"}):
            llm = GeminiLLM()

            gate = GateStatus(
                gate_id="A",
                name="Gate A",
                capacity_per_min=2.0,
                arrivals_per_min=1.0,
                servers_open=1,
            )
            wait = WaitEstimate(
                gate_id="A",
                predicted_wait_minutes=5.5,
                utilization=0.5,
                congestion_level="low",
                server_farm_saturated=False,
            )
            ctx = ResolvedContext(
                intent="general_info",
                recommended_gate=gate,
                wait_estimate=wait,
                accessible_route_available=True,
                safety_notice=None,
                sanitized_user_text="hello",
                language=Language.EN,
            )

            mock_model = mock_genai.GenerativeModel.return_value
            mock_model.generate_content.return_value.text = (
                "This is a rephrased message for Gate A with 5.5 minutes."
            )

            reply = llm.phrase(ctx)
            assert reply.text == "This is a rephrased message for Gate A with 5.5 minutes."

            mock_model.generate_content.return_value.text = "Invalid text without wait info."
            reply_fallback = llm.phrase(ctx)
            assert "Gate A" in reply_fallback.text
            assert reply_fallback.text != "Invalid text without wait info."

            mock_model.generate_content.side_effect = Exception("API error")
            reply_err = llm.phrase(ctx)
            assert "Gate A" in reply_err.text


def test_lost_and_found_intent():
    # 1. Test intent classification
    assert classify_intent("I lost my wallet near section 12") == "lost_and_found"
    assert classify_intent("Has anyone found a set of keys?") == "lost_and_found"
    assert classify_intent("I left my backpack behind") == "lost_and_found"
    assert classify_intent("missing item report") == "lost_and_found"

    # 2. Test resolve and phrase output
    q = UserQuery(persona=Persona.FAN, language=Language.EN, raw_text="I lost my phone")
    ctx = resolve(q, "I lost my phone")
    assert ctx.intent == "lost_and_found"

    reply_en = MockLLM().phrase(ctx)
    assert "guest services desk" in reply_en.text

    # Hindi phrasing
    ctx.language = Language.HI
    reply_hi = MockLLM().phrase(ctx)
    assert "अतिथि सेवा डेस्क" in reply_hi.text

    # Spanish phrasing
    ctx.language = Language.ES
    reply_es = MockLLM().phrase(ctx)
    assert "atención al público" in reply_es.text


# ---------------------------------------------------------------------------
# New tests: VISUAL / HEARING accessibility, security.txt, snapshot
# ---------------------------------------------------------------------------


def test_best_gate_visual_accessibility_filters_audio_guidance():
    """Gates without audio guidance (Gate H) must be excluded for VISUAL need."""
    gate, _ = best_gate(AccessibilityNeed.VISUAL)
    assert gate.has_audio_guidance is True
    assert gate.gate_id != "H"


def test_best_gate_hearing_accessibility_filters_visual_display():
    """Gates without visual displays (Gate G) must be excluded for HEARING need."""
    gate, _ = best_gate(AccessibilityNeed.HEARING)
    assert gate.has_visual_display is True
    assert gate.gate_id != "G"


def test_resolve_propagates_accessibility_need_to_context():
    """accessibility_need from UserQuery must appear on the ResolvedContext."""
    q = UserQuery(
        persona=Persona.FAN,
        language=Language.EN,
        raw_text="I need an accessible entrance",
        accessibility_need=AccessibilityNeed.VISUAL,
    )
    ctx = resolve(q, "I need an accessible entrance")
    assert ctx.accessibility_need == AccessibilityNeed.VISUAL


def test_mock_llm_visual_accessibility_phrasing():
    """VISUAL accessibility intent must mention audio guidance."""
    gate = GateStatus(
        gate_id="A",
        name="Gate A",
        capacity_per_min=3.0,
        arrivals_per_min=1.0,
        servers_open=3,
        has_audio_guidance=True,
    )
    wait = WaitEstimate(
        gate_id="A",
        predicted_wait_minutes=2.0,
        utilization=0.3,
        congestion_level="low",
        server_farm_saturated=False,
    )
    ctx = ResolvedContext(
        intent="accessibility",
        recommended_gate=gate,
        wait_estimate=wait,
        accessible_route_available=True,
        safety_notice=None,
        sanitized_user_text="need audio guide",
        language=Language.EN,
        accessibility_need=AccessibilityNeed.VISUAL,
    )
    reply = MockLLM().phrase(ctx)
    assert "audio" in reply.text.lower()
    assert "Gate A" in reply.text


def test_mock_llm_hearing_accessibility_phrasing():
    """HEARING accessibility intent must mention visual display / LED."""
    gate = GateStatus(
        gate_id="C",
        name="Gate C",
        capacity_per_min=3.0,
        arrivals_per_min=1.0,
        servers_open=3,
        has_visual_display=True,
    )
    wait = WaitEstimate(
        gate_id="C",
        predicted_wait_minutes=3.0,
        utilization=0.3,
        congestion_level="low",
        server_farm_saturated=False,
    )
    ctx = ResolvedContext(
        intent="accessibility",
        recommended_gate=gate,
        wait_estimate=wait,
        accessible_route_available=True,
        safety_notice=None,
        sanitized_user_text="need visual display",
        language=Language.EN,
        accessibility_need=AccessibilityNeed.HEARING,
    )
    reply = MockLLM().phrase(ctx)
    assert "visual" in reply.text.lower() or "LED" in reply.text
    assert "Gate C" in reply.text


def test_spanish_language_response():
    """Spanish (ES) template must return a non-empty string containing the gate name."""
    gate = GateStatus(
        gate_id="A",
        name="Gate A \u2014 North",
        capacity_per_min=3.2,
        arrivals_per_min=1.0,
        servers_open=3,
    )
    wait = WaitEstimate(
        gate_id="A",
        predicted_wait_minutes=4.0,
        utilization=0.3,
        congestion_level="low",
        server_farm_saturated=False,
    )
    ctx = ResolvedContext(
        intent="find_gate",
        recommended_gate=gate,
        wait_estimate=wait,
        accessible_route_available=True,
        safety_notice=None,
        sanitized_user_text="which gate",
        language=Language.ES,
    )
    reply = MockLLM().phrase(ctx)
    assert reply.language == Language.ES
    assert "Gate A" in reply.text
    assert len(reply.text) > 10


def test_security_txt_endpoint():
    """/.well-known/security.txt must exist and contain a Contact field (RFC 9116)."""
    client = TestClient(app)
    resp = client.get("/.well-known/security.txt")
    assert resp.status_code == 200
    assert "Contact:" in resp.text
    assert "Expires:" in resp.text


def test_ops_snapshot_includes_arrivals_per_min():
    """ops snapshot must include arrivals_per_min from the live gate status."""
    client = TestClient(app)
    resp = client.get("/api/ops/snapshot")
    assert resp.status_code == 200
    gates = {g["gate_id"]: g for g in resp.json()["gates"]}
    for gate in gates.values():
        assert "arrivals_per_min" in gate
        assert "capacity_per_min" in gate
        assert "predicted_wait_minutes" in gate


def test_ops_live_sse_content_type():
    """GET /api/ops/live must return text/event-stream content-type."""
    client = TestClient(app)
    with client.stream("GET", "/api/ops/live") as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        for line in response.iter_lines():
            if isinstance(line, bytes):
                line = line.decode("utf-8")
            if line.startswith("data: "):
                data = json.loads(line[6:])
                assert "gates" in data
                assert "critical_count" in data
                break


def test_infer_accessibility_need_from_text_wheelchair():
    """_infer_accessibility_need must upgrade NONE -> WHEELCHAIR when 'wheelchair' is in text."""
    result = _infer_accessibility_need(
        AccessibilityNeed.NONE,
        "I need a wheelchair accessible gate",
    )
    assert result == AccessibilityNeed.WHEELCHAIR


def test_infer_accessibility_need_explicit_wins():
    """Explicit selection always beats text inference."""
    result = _infer_accessibility_need(AccessibilityNeed.HEARING, "wheelchair accessible gate")
    assert result == AccessibilityNeed.HEARING


def test_resolve_live_infers_wheelchair_from_text():
    """resolve_live must route to a step-free gate when text says 'wheelchair'
    even if accessibility_need field is NONE (the dropdown default)."""
    q = UserQuery(
        persona=Persona.FAN,
        language=Language.EN,
        raw_text="is there a wheelchair accessible entrance",
        accessibility_need=AccessibilityNeed.NONE,
    )
    ctx = resolve_live(q, "is there a wheelchair accessible entrance")
    assert ctx.recommended_gate is not None
    assert ctx.recommended_gate.step_free is True
    assert ctx.accessibility_need == AccessibilityNeed.WHEELCHAIR


# ── venues.json loader tests ─────────────────────────────────────────────────


def test_gates_loaded_from_venues_json():
    """GATES must be populated at import time from venues.json (not hardcoded)."""
    assert len(GATES) == 8, "Expected 8 gates as defined in venues.json"
    assert "A" in GATES and "H" in GATES


def test_gates_have_correct_schema():
    """Each loaded gate must satisfy the GateStatus schema exactly."""
    from app.core.schemas import GateStatus as _GateStatus

    for gate_id, gate in GATES.items():
        assert isinstance(gate, _GateStatus)
        assert gate.gate_id == gate_id
        assert gate.capacity_per_min > 0
        assert gate.servers_open >= 1


def test_venue_load_malformed_file_returns_empty(tmp_path, monkeypatch):
    """_load_venue() must return ({}, []) gracefully when venues.json is malformed."""
    import app.core.context_engine as ce

    bad_file = tmp_path / "venues.json"
    bad_file.write_text("{not valid json}", encoding="utf-8")
    monkeypatch.setattr(ce, "_VENUES_FILE", bad_file)
    gates, phases = ce._load_venue()
    assert gates == {}
    assert phases == []


# ── matchday phase tests ─────────────────────────────────────────────────────


def test_matchday_phase_demo_mode_returns_pre_match():
    """Without KICKOFF_EPOCH_UNIX set, must return pre_match phase (demo mode)."""
    os.environ.pop("KICKOFF_EPOCH_UNIX", None)
    phase = matchday_phase()
    assert phase["name"] == "pre_match"
    assert phase["arrivals_multiplier"] > 1.0


def test_matchday_phase_kickoff_returns_correct_phase(monkeypatch):
    """With kickoff 10 minutes ago, must return 'kickoff' phase."""
    kickoff = t.time() - 10 * 60
    monkeypatch.setenv("KICKOFF_EPOCH_UNIX", str(int(kickoff)))
    phase = matchday_phase()
    assert phase["name"] == "kickoff"
    assert phase["arrivals_multiplier"] < 0.5


def test_matchday_phase_half_time(monkeypatch):
    """With kickoff 50 minutes ago, must return 'half_time' phase."""
    kickoff = t.time() - 50 * 60
    monkeypatch.setenv("KICKOFF_EPOCH_UNIX", str(int(kickoff)))
    phase = matchday_phase()
    assert phase["name"] == "half_time"


def test_get_phase_multiplier_is_positive():
    """get_phase_multiplier must always return a non-negative float."""
    os.environ.pop("KICKOFF_EPOCH_UNIX", None)
    mult = get_phase_multiplier()
    assert isinstance(mult, float)
    assert mult >= 0.0


def test_live_snapshot_applies_phase_multiplier(monkeypatch):
    """live_gate_snapshot arrivals must reflect the matchday phase multiplier."""
    kickoff = t.time() - 10 * 60
    monkeypatch.setenv("KICKOFF_EPOCH_UNIX", str(int(kickoff)))
    kickoff_snap = live_gate_snapshot()

    monkeypatch.delenv("KICKOFF_EPOCH_UNIX", raising=False)
    pre_match_snap = live_gate_snapshot()

    kickoff_total = sum(g.arrivals_per_min for g in kickoff_snap.values())
    pre_match_total = sum(g.arrivals_per_min for g in pre_match_snap.values())
    assert pre_match_total > kickoff_total * 5, (
        f"Expected pre_match arrivals ({pre_match_total:.1f}) >> "
        f"kickoff arrivals ({kickoff_total:.1f})"
    )


def test_ops_briefing_endpoint():
    """Verify that POST /api/ops/briefing returns valid structure and data."""
    client = TestClient(app)
    response = client.post("/api/ops/briefing")
    assert response.status_code == 200
    data = response.json()
    assert "briefing" in data
    assert "generated_at" in data
    assert "snapshot_summary" in data

    summary = data["snapshot_summary"]
    assert "total_gates" in summary
    assert "critical_count" in summary
    assert "high_count" in summary
    assert "avg_wait_minutes" in summary
    assert summary["total_gates"] == 8
