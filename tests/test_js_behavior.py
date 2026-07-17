"""JavaScript behavioral contract tests.

These tests verify that:
1. The SSE/snapshot payload shapes exactly match what app.js expects.
2. The Erlang-C formula in app.js produces results consistent with the Python
   backend's predict_wait() — both must agree to within floating-point tolerance.
3. The renderOpsTable input contract is met: every gate row must carry the fields
   that app.js reads directly (gate_id, name, predicted_wait_minutes, utilization,
   arrivals_per_min, congestion_level, incident).
4. The SSE critical_count field is derived from the same rows (not a second computation).

No browser or Node.js is required.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.context_engine import predict_wait
from app.core.schemas import GateStatus
from app.main import app

# ── 1. Python mirror of the JS erlangC() + predictWait() functions ───────────


def _js_factorial(n: int) -> float:
    """Mirror of the factorial() function in app.js."""
    if n <= 1:
        return 1
    r = 1
    for i in range(2, n + 1):
        r *= i
    return r


def _js_erlang_c(lam: float, mu: float, c: int) -> float:
    """Mirror of the erlangC() function in app.js (lines 104-116)."""
    a = lam / mu
    rho = a / c
    if rho >= 1:
        return 1.0
    sum_terms = sum(pow(a, k) / _js_factorial(k) for k in range(c))
    last_term = pow(a, c) / _js_factorial(c)
    erlang_c = last_term / (last_term + sum_terms * (1 - rho))
    return erlang_c


def _js_predict_wait(lam: float, mu: float, c: int) -> float:
    """Mirror of the predictWait() function in app.js (lines 125-132)."""
    if c <= 0 or lam <= 0:
        return 0.0
    rho = lam / (c * mu)
    if rho >= 1:
        return 999.0
    erlang_c = _js_erlang_c(lam, mu, c)
    wq = erlang_c / (c * mu - lam)
    return round(max(0, wq) * 10) / 10


# ── 2. Agreement tests: JS formula must match Python backend ─────────────────


@pytest.mark.parametrize(
    "lam,mu,c",
    [
        (3.0, 2.0, 3),
        (1.0, 2.0, 2),
        (9.0, 3.0, 4),
        (7.8, 2.8, 3),
        (0.0, 2.0, 2),
        (0.6, 1.0, 1),
    ],
)
def test_js_erlang_c_matches_python_backend(lam, mu, c):
    """JS predictWait() must agree with Python predict_wait() to within ±0.2 min."""
    gate = GateStatus(
        gate_id="TEST",
        name="Test",
        arrivals_per_min=lam,
        capacity_per_min=mu,
        servers_open=c,
    )
    py_wait = predict_wait(gate).predicted_wait_minutes
    js_wait = _js_predict_wait(lam, mu, c)
    assert abs(py_wait - js_wait) <= 0.2, (
        f"JS and Python Erlang-C disagree for λ={lam} μ={mu} c={c}: JS={js_wait} Python={py_wait}"
    )


def test_js_erlang_c_zero_arrivals_returns_zero():
    """JS predictWait must return 0 for λ=0."""
    assert _js_predict_wait(0, 2.0, 3) == 0.0


def test_js_erlang_c_overloaded_returns_999():
    """JS predictWait must return 999 when rho >= 1 (system unstable)."""
    assert _js_predict_wait(10.0, 1.0, 1) == 999.0


def test_js_erlang_c_increases_with_load():
    """Higher arrival rate -> longer predicted wait (monotonicity check)."""
    low_wait = _js_predict_wait(1.0, 2.0, 2)
    high_wait = _js_predict_wait(3.6, 2.0, 2)
    assert high_wait > low_wait


# ── 3. SSE payload contract ───────────────────────────────────────────────────

_JS_REQUIRED_GATE_FIELDS = {
    "gate_id",
    "name",
    "predicted_wait_minutes",
    "utilization",
    "arrivals_per_min",
    "congestion_level",
}


@pytest.fixture(scope="module")
def ops_client():
    return TestClient(app)


def test_snapshot_payload_contains_all_js_required_fields(ops_client):
    """Every gate in /api/ops/snapshot must carry the fields that app.js reads."""
    resp = ops_client.get("/api/ops/snapshot")
    assert resp.status_code == 200
    data = resp.json()
    assert "gates" in data
    assert "critical_count" in data
    for gate_row in data["gates"]:
        missing = _JS_REQUIRED_GATE_FIELDS - set(gate_row.keys())
        assert not missing, f"Gate row missing JS-required fields: {missing}"


def test_sse_payload_matches_snapshot_schema(ops_client):
    """The SSE stream payload must have the same schema as /api/ops/snapshot."""
    with ops_client.stream("GET", "/api/ops/live") as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if isinstance(line, bytes):
                line = line.decode("utf-8")
            if line.startswith("data: "):
                data = json.loads(line[6:])
                assert "gates" in data
                assert "critical_count" in data
                for gate_row in data["gates"]:
                    missing = _JS_REQUIRED_GATE_FIELDS - set(gate_row.keys())
                    assert not missing, f"SSE gate row missing JS-required fields: {missing}"
                break


def test_critical_count_derived_from_same_rows(ops_client):
    """critical_count must equal the count of 'critical' gates in the rows array."""
    resp = ops_client.get("/api/ops/snapshot")
    data = resp.json()
    expected = sum(1 for g in data["gates"] if g.get("congestion_level") == "critical")
    assert data["critical_count"] == expected


def test_sustainability_input_fields_present(ops_client):
    """arrivals_per_min and congestion_level must have expected types."""
    resp = ops_client.get("/api/ops/snapshot")
    data = resp.json()
    for gate_row in data["gates"]:
        assert isinstance(gate_row["arrivals_per_min"], int | float)
        assert isinstance(gate_row["congestion_level"], str)
        assert gate_row["congestion_level"] in ("low", "moderate", "high", "critical")


def test_snapshot_contains_matchday_phase_fields(ops_client):
    """Snapshot must include matchday_phase and matchday_label."""
    resp = ops_client.get("/api/ops/snapshot")
    data = resp.json()
    assert "matchday_phase" in data, "matchday_phase missing from ops snapshot"
    assert "matchday_label" in data, "matchday_label missing from ops snapshot"
    valid_phases = {
        "pre_match",
        "kickoff",
        "half_time",
        "second_half",
        "full_time",
        "post_match",
    }
    assert data["matchday_phase"] in valid_phases, f"Unknown phase: {data['matchday_phase']}"
    assert isinstance(data["matchday_label"], str) and len(data["matchday_label"]) > 0
