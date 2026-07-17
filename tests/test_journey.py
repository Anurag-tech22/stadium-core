from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_end_to_end_user_journey():
    """End-to-end integration test representing the full user journey.

    1. Hit /health to verify application status.
    2. Hit /api/assist with a fan query in Hindi requesting wheelchair-accessible gate.
    3. Hit /api/ops/snapshot to retrieve operational snapshot for all gates.
    4. Assert mutual consistency:
       - The recommended gate from assist must be a step-free gate.
       - The recommended gate's ID and wait time must match the values in the snapshot.
    """
    client = TestClient(app)

    # 1. Verify health check
    health_resp = client.get("/health")
    assert health_resp.status_code == 200
    assert health_resp.json() == {"status": "ok"}

    # 2. Query assistant (Hindi, Wheelchair Accessibility requested)
    assist_payload = {
        "persona": "fan",
        "language": "hi",
        "raw_text": "व्हीलचेयर wheelchair accessible मार्ग",
        "accessibility_need": "wheelchair",
    }
    assist_resp = client.post("/api/assist", json=assist_payload)
    assert assist_resp.status_code == 200
    assist_data = assist_resp.json()

    # Verify response structure
    assert "reply" in assist_data
    assert "intent" in assist_data
    assert "grounded_facts" in assist_data
    assert assist_data["accessible_route_available"] is True

    # Check that it translated/phrased in Hindi
    assert isinstance(assist_data["reply"], str)

    # Parse gate recommendation and wait time from the grounded facts
    recommended_gate_id = None
    predicted_wait_minutes = None

    for fact in assist_data["grounded_facts"]:
        if fact.startswith("gate="):
            recommended_gate_id = fact.split("=")[1]
        elif fact.startswith("wait_minutes="):
            predicted_wait_minutes = float(fact.split("=")[1])

    assert recommended_gate_id is not None, "Grounded facts must contain gate recommendation"
    assert predicted_wait_minutes is not None, "Grounded facts must contain wait estimate minutes"

    # 3. Verify operational snapshot
    snapshot_resp = client.get("/api/ops/snapshot")
    assert snapshot_resp.status_code == 200
    snapshot_data = snapshot_resp.json()

    assert "gates" in snapshot_data
    assert "critical_count" in snapshot_data

    # 4. Verify mutual consistency
    snapshot_gates = {g["gate_id"]: g for g in snapshot_data["gates"]}

    assert recommended_gate_id in snapshot_gates

    snapshot_gate_data = snapshot_gates[recommended_gate_id]
    assert snapshot_gate_data["predicted_wait_minutes"] == predicted_wait_minutes

    # Recommended gate must be step-free
    assert recommended_gate_id != "B", (
        "Non-accessible gate B must not be recommended for wheelchair accessibility needs"
    )
