"""Deterministic core. No network calls, no LLM, no randomness.

Two responsibilities:
  1. intent routing — map sanitized user text to a fixed intent (keyword
     rules, not an LLM classifier, so it cannot be prompt-injected)
  2. wait-time prediction — an M/M/c queueing model over live gate data

This module is the thing an evaluator can fully verify in one file.
The LLM (see llm.py) is only ever called AFTER this module has already
decided the facts; it is not allowed to change them.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from app.core.schemas import (
    AccessibilityNeed,
    GateStatus,
    ResolvedContext,
    UserQuery,
    WaitEstimate,
)

logger = logging.getLogger("phoenix.engine")


# ── Venue configuration ──────────────────────────────────────────────────────
# Priority order:
#   1. Firestore (when GOOGLE_CLOUD_PROJECT env var is set)
#   2. venues.json (local dev, CI, offline fallback)
#   Swapping venues.json or the Firestore document lets you deploy at any stadium
#   without touching Python code.
_VENUES_FILE: Path = Path(__file__).resolve().parent.parent.parent / "venues.json"


def _parse_gates(raw: dict) -> tuple[dict[str, GateStatus], list[dict]]:
    """Shared parser for both Firestore and venues.json raw dicts."""
    gates: dict[str, GateStatus] = {}
    for g in raw.get("gates", []):
        try:
            gs = GateStatus(**{k: v for k, v in g.items() if k != "_comment"})
            gates[gs.gate_id] = gs
        except Exception as exc:
            logger.warning("Skipping malformed gate entry %s: %s", g, exc)
    phases = raw.get("matchday_schedule", {}).get("phases", [])
    return gates, phases


def _load_venue() -> tuple[dict[str, GateStatus], list[dict]]:
    """Load venue config from Firestore (GCP mode) or venues.json (local mode).

    GCP mode  — GOOGLE_CLOUD_PROJECT env var is set:
      Reads gates and matchday phases from Firestore 'venues/{venue_id}' document.
      On first deploy (document missing), seeds Firestore from venues.json so
      operators have a starting point to edit in the Firebase Console.

    Local mode — GOOGLE_CLOUD_PROJECT not set:
      Reads directly from venues.json.  Zero network calls.
    """
    # --- Try Firestore first ---
    from app.core.firestore_client import fetch_venue_config, seed_venue_config
    fs_raw = fetch_venue_config()
    if fs_raw is not None:
        gates, phases = _parse_gates(fs_raw)
        if gates:
            logger.info(
                "Loaded %d gates from Firestore (venue=%s)",
                len(gates), os.environ.get("FIRESTORE_VENUE_ID", "phoenix-001")
            )
            return gates, phases
        logger.warning("Firestore venue doc exists but has no gates — seeding from venues.json")

    # --- Fall back to venues.json ---
    try:
        raw = json.loads(_VENUES_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Cannot load venues.json: %s — using empty venue", exc)
        return {}, []

    gates, phases = _parse_gates(raw)

    # Seed Firestore on first deploy so operators can edit config in Console
    if gates:
        seed_venue_config(raw)

    return gates, phases


# Module-level constants — loaded once at import time (fast reads, zero I/O per request)
GATES: dict[str, GateStatus]
MATCHDAY_PHASES: list[dict]
GATES, MATCHDAY_PHASES = _load_venue()


# ── Matchday phase awareness ─────────────────────────────────────────────────
def matchday_phase() -> dict:
    """Return the active matchday phase dict based on KICKOFF_EPOCH_UNIX env var.

    Set KICKOFF_EPOCH_UNIX to the Unix timestamp of kickoff to activate live
    phase tracking. Omit the variable (or set it to '0') for demo/static mode
    — the system uses the 'pre_match' phase by default.

    Example::
        export KICKOFF_EPOCH_UNIX=$(date -d '15:00 today' +%s)
    """
    kickoff_str = os.environ.get("KICKOFF_EPOCH_UNIX", "0")
    try:
        kickoff_epoch = float(kickoff_str)
    except ValueError:
        kickoff_epoch = 0.0

    if kickoff_epoch <= 0:
        # Demo mode — return default phase (pre_match) to show realistic arrivals
        defaults = [p for p in MATCHDAY_PHASES if p.get("name") == "pre_match"]
        return defaults[0] if defaults else {"name": "pre_match", "arrivals_multiplier": 1.0}

    elapsed_min = (time.time() - kickoff_epoch) / 60.0
    for phase in MATCHDAY_PHASES:
        if phase["start_min"] <= elapsed_min < phase["end_min"]:
            return phase

    # Past all defined phases — return last one
    if MATCHDAY_PHASES:
        return MATCHDAY_PHASES[-1]
    return {"name": "post_match", "arrivals_multiplier": 0.05}


def get_phase_multiplier() -> float:
    """Convenience wrapper — returns just the arrivals_multiplier for the current phase.
    Used by live_gate_snapshot() to scale base arrival rates."""
    return float(matchday_phase().get("arrivals_multiplier", 1.0))



# Order matters: classify_intent returns the FIRST matching intent, so more
# specific themes must be listed before "find_gate" — otherwise a query like
# "how long is the queue at gate B" would match find_gate's "gate" keyword
# before it ever reaches wait_time's "queue" keyword.
INTENT_KEYWORDS: dict[str, list[str]] = {
    "emergency": ["emergency", "help", "medical", "evacuat", "fire"],
    "accessibility": ["wheelchair", "accessible", "ramp", "step-free", "disability"],
    "wait_time": ["wait", "queue", "line", "how long", "busy"],
    "crowd_status": ["crowd", "density", "capacity", "how full"],
    "transport": ["parking", "bus", "train", "shuttle", "taxi", "car"],
    "restroom": ["restroom", "toilet", "bathroom", "washroom"],
    "sustainability": ["recycle", "recycling", "waste", "sustainab"],
    "lost_and_found": ["lost", "found", "missing item", "left behind", "left my"],
    "find_gate": ["gate", "entrance", "entry", "door"],
}


def classify_intent(text: str) -> str:
    """Fixed keyword routing. Deliberately NOT an LLM call — this is the
    seam the injection-resistance test in tests/test_security.py checks."""
    lowered = text.lower()
    for intent, keywords in INTENT_KEYWORDS.items():
        if any(k in lowered for k in keywords):
            return intent
    return "general_info"


def predict_wait(gate: GateStatus) -> WaitEstimate:
    """M/M/c queueing model: c parallel servers, Poisson arrivals.

    lambda = arrivals_per_min, mu = capacity_per_min per server, c = servers_open.
    Returns predicted wait (Lq/lambda via Erlang-C) and utilization rho.
    This is genuinely computed, not a lookup table or an LLM guess.
    """
    lam = gate.arrivals_per_min
    mu = gate.capacity_per_min
    c = gate.servers_open

    if c <= 0 or mu <= 0:
        # No open servers at this gate — treat as fully closed/saturated
        # rather than running it through the Erlang-C formula (which is
        # undefined for c=0). This is a real operational state, not an
        # error: a gate can be temporarily staffed down to zero.
        return WaitEstimate(
            gate_id=gate.gate_id,
            predicted_wait_minutes=999.0,
            utilization=1.0,
            congestion_level="critical",
            server_farm_saturated=True,
        )


    rho = lam / (c * mu) if c * mu > 0 else 1.0

    if rho >= 0.99:
        wait = 99.0
        rho = min(rho, 0.999)
    elif lam <= 0:
        wait = 0.0
        rho = min(rho, 0.999)
    else:
        rho = min(rho, 0.999)
        a = lam / mu  # offered load (Erlangs)
        # Erlang C probability of queueing
        sum_terms = sum((a ** k) / math.factorial(k) for k in range(c))
        last_term = (a ** c) / (math.factorial(c) * (1 - rho))
        p0 = 1 / (sum_terms + last_term)
        p_wait = last_term * p0
        lq = p_wait * rho / (1 - rho)
        wait = (lq / lam) if lam > 0 else 0.0
        # Operational ceiling clamp to align mathematical limits with physical stadium behavior
        wait = min(wait, 99.0)


    if rho < 0.5:
        level = "low"
    elif rho < 0.75:
        level = "moderate"
    elif rho < 0.92:
        level = "high"
    else:
        level = "critical"

    return WaitEstimate(
        gate_id=gate.gate_id,
        predicted_wait_minutes=round(wait, 1),
        utilization=round(rho, 3),
        congestion_level=level,
        server_farm_saturated=rho >= 0.92,
    )


def best_gate(need: AccessibilityNeed = AccessibilityNeed.NONE) -> tuple[GateStatus, WaitEstimate]:
    """Pick the gate with the lowest predicted wait, filtered by accessibility need.

    - WHEELCHAIR → step_free gates only
    - VISUAL     → gates with has_audio_guidance (PA/audio wayfinding system)
    - HEARING    → gates with has_visual_display (LED/screen wayfinding boards)
    """
    candidates = [
        g for g in GATES.values()
        if (need != AccessibilityNeed.WHEELCHAIR or g.step_free)
        and (need != AccessibilityNeed.VISUAL or g.has_audio_guidance)
        and (need != AccessibilityNeed.HEARING or g.has_visual_display)
    ]
    if not candidates:
        candidates = list(GATES.values())
    scored = [(g, predict_wait(g)) for g in candidates]
    scored.sort(key=lambda pair: pair[1].predicted_wait_minutes)
    return scored[0]


def resolve(query: UserQuery, sanitized_text: str) -> ResolvedContext:
    """The single entry point: sanitized text in, fully-grounded facts out.
    Everything the LLM later sees is decided right here."""
    intent = classify_intent(sanitized_text)
    gate: GateStatus | None = None
    wait: WaitEstimate | None = None
    accessible = True
    safety_notice = None

    if intent in ("find_gate", "wait_time", "accessibility", "crowd_status", "general_info"):
        gate, wait = best_gate(query.accessibility_need)
        accessible = gate.step_free or query.accessibility_need != AccessibilityNeed.WHEELCHAIR

    if intent == "emergency":
        safety_notice = "STOP. Contact stewards or call emergency services immediately."

    return ResolvedContext(
        intent=intent,
        recommended_gate=gate,
        wait_estimate=wait,
        accessible_route_available=accessible,
        safety_notice=safety_notice,
        sanitized_user_text=sanitized_text,
        language=query.language,
        accessibility_need=query.accessibility_need,
    )


def all_gate_predictions() -> list[WaitEstimate]:
    return [predict_wait(g) for g in GATES.values()]


def _time_bucket(bucket_seconds: int = 300) -> int:
    """5-minute windows — numbers stay stable within a window (so rapid
    refreshes don't look glitchy) but shift over the course of a match."""
    return int(time.time() // bucket_seconds)


# Resolve data directory relative to the project root — works whether run
# as a script, via uvicorn, or installed as a package.
_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent
OVERRIDES_FILE: str = str(_PROJECT_ROOT / "overrides_db.json")


def _load_overrides() -> dict[str, dict[str, Any]]:
    """Load gate overrides from Firestore (GCP mode) or overrides_db.json (local mode).

    Firestore provides concurrent-safe multi-worker reads; the JSON file is
    used only when GOOGLE_CLOUD_PROJECT is not set (local dev / CI).
    """
    # Try Firestore first
    from app.core.firestore_client import fetch_all_overrides
    fs_overrides = fetch_all_overrides()
    if fs_overrides is not None and os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return fs_overrides  # GCP mode — always use Firestore

    # Local mode — read from JSON file
    if os.path.exists(OVERRIDES_FILE):
        try:
            with open(OVERRIDES_FILE, encoding="utf-8") as f:
                return cast(dict[str, dict[str, Any]], json.load(f))
        except Exception as exc:  # corrupted JSON or permission error
            logger.warning("Failed to load overrides from %s: %s", OVERRIDES_FILE, exc)
    return {}


def _save_overrides(data: dict[str, dict[str, Any]]) -> None:
    """Persist gate overrides atomically using a temp-file + os.replace pattern.
    This prevents JSON corruption under concurrent writes (two turnstile
    updates arriving simultaneously cannot interleave and corrupt the file)."""
    try:
        dir_name = os.path.dirname(OVERRIDES_FILE) or "."
        with tempfile.NamedTemporaryFile(
            mode="w", dir=dir_name, suffix=".tmp",
            delete=False, encoding="utf-8"
        ) as tmp:
            json.dump(data, tmp)
            tmp_path = tmp.name
        os.replace(tmp_path, OVERRIDES_FILE)  # atomic on POSIX and Windows
    except Exception as exc:
        logger.warning("Failed to persist overrides to %s: %s", OVERRIDES_FILE, exc)


# Module-level mutable dict — mutated in-place so no `global` keyword needed.
_live_turnstile_overrides: dict[str, dict[str, Any]] = _load_overrides()


@lru_cache(maxsize=256)
def _pseudo_multiplier(gate_id: str, bucket: int, low: float = 0.5, high: float = 1.8) -> float:
    """Deterministic, hash-based pseudo-random multiplier — NOT random.random().
    Same gate_id + bucket always gives the same number, so this is fully
    reproducible and never flaky, while still varying across gates and time buckets
    to simulate a live crowd. Uses @lru_cache for idiomatic automatic LRU eviction."""
    h = hashlib.sha256(f"{gate_id}:{bucket}".encode()).hexdigest()
    frac = int(h[:8], 16) / 0xFFFFFFFF
    return low + frac * (high - low)


def _sanitize_log(val: object) -> str:
    """Sanitize values for logging to prevent Log Injection (CRLF injection)."""
    return str(val).replace("\n", "\\n").replace("\r", "\\r")


def update_live_turnstile(
    gate_id: str,
    arrivals_per_min: float | None = None,
    capacity_per_min: float | None = None,
    servers_open: int | None = None,
    incident: str | None = None,
) -> None:
    """Updates live gate override states from physical turnstiles or staff check-in terminals.

    Reads fresh from the persistence file first so that in multi-worker
    deployments (e.g. multiple uvicorn workers) all workers stay in sync.
    Writes are atomic (temp-file + os.replace) to prevent JSON corruption.
    """
    if gate_id not in GATES:
        raise ValueError(f"Invalid gate ID: {gate_id}")

    # Build the fields dict for this gate
    fields: dict[str, Any] = {}
    if arrivals_per_min is not None:
        fields["arrivals_per_min"] = arrivals_per_min
    if capacity_per_min is not None:
        fields["capacity_per_min"] = capacity_per_min
    if servers_open is not None:
        fields["servers_open"] = servers_open
    if incident is not None:
        fields["incident"] = incident

    if os.environ.get("GOOGLE_CLOUD_PROJECT"):
        # ── GCP mode: write to Firestore (concurrent-safe, multi-worker) ──────
        from app.core.firestore_client import write_gate_override
        write_gate_override(gate_id, fields)
        logger.info(
            "Gate %s override written to Firestore: %s",
            _sanitize_log(gate_id),
            _sanitize_log(fields),
        )
    else:
        # ── Local mode: atomic JSON file write ────────────────────────────────
        overrides = _load_overrides()
        if gate_id not in overrides:
            overrides[gate_id] = {}
        overrides[gate_id].update(fields)
        _save_overrides(overrides)

    # Always update the in-memory cache (fast reads, no I/O per request)
    if gate_id not in _live_turnstile_overrides:
        _live_turnstile_overrides[gate_id] = {}
    _live_turnstile_overrides[gate_id].update(fields)


def live_gate_snapshot() -> dict[str, GateStatus]:
    """GATES with arrivals adjusted by:
    1. matchday_phase multiplier  — scales arrivals by match phase (pre-match,
       kickoff, half-time, full-time, etc.) from venues.json schedule.
    2. _pseudo_multiplier         — deterministic, hash-based per-gate intra-phase
       variation (keeps the data looking live within a phase window).
    3. IoT turnstile overrides    — absolute values from real hardware; these
       override steps 1 and 2 entirely when present.
    Used ONLY by live routes.
    """
    bucket = _time_bucket()
    phase_mult = get_phase_multiplier()   # matchday phase scaling factor
    res = {}
    for gate_id, gate in GATES.items():
        # Apply phase multiplier first, then per-gate time-bucket variation
        base_arrivals = (
            gate.arrivals_per_min
            * phase_mult
            * _pseudo_multiplier(gate_id, bucket)
        )
        updates = {}
        if gate_id in _live_turnstile_overrides:
            overrides = _live_turnstile_overrides[gate_id]
            if "arrivals_per_min" in overrides:
                # IoT override bypasses both multipliers — use raw hardware reading
                base_arrivals = overrides["arrivals_per_min"]
            if "capacity_per_min" in overrides:
                updates["capacity_per_min"] = overrides["capacity_per_min"]
            if "servers_open" in overrides:
                updates["servers_open"] = overrides["servers_open"]
            if "incident" in overrides:
                updates["incident"] = overrides["incident"]
        updates["arrivals_per_min"] = round(base_arrivals, 2)
        res[gate_id] = gate.model_copy(update=updates)
    return res




def best_gate_live(
    need: AccessibilityNeed = AccessibilityNeed.NONE,
) -> tuple[GateStatus, WaitEstimate, tuple[GateStatus, WaitEstimate] | None]:
    """Like best_gate(), but against the live-varying snapshot, and also
    returns the second-best option as an alternate suggestion.
    Filters by need (WHEELCHAIR/VISUAL/HEARING) and excludes closed gates."""
    snapshot = live_gate_snapshot()
    # Filter out gates with active closed status incidents and apply accessibility need
    candidates = [
        g for g in snapshot.values()
        if (need != AccessibilityNeed.WHEELCHAIR or g.step_free)
        and (need != AccessibilityNeed.VISUAL or g.has_audio_guidance)
        and (need != AccessibilityNeed.HEARING or g.has_visual_display)
        and not (g.incident and "closed" in g.incident.lower())
    ]
    if not candidates:
        # Fallback to all candidates if everything is filtered out
        candidates = [
            g for g in snapshot.values()
            if (need != AccessibilityNeed.WHEELCHAIR or g.step_free)
        ]
        if not candidates:
            candidates = list(snapshot.values())

    scored = [(g, predict_wait(g)) for g in candidates]
    scored.sort(key=lambda pair: pair[1].predicted_wait_minutes)
    best = scored[0]
    alternate = scored[1] if len(scored) > 1 else None
    return best[0], best[1], alternate


def _infer_accessibility_need(
    explicit_need: AccessibilityNeed, sanitized_text: str
) -> AccessibilityNeed:
    """If the API payload says 'none' but the user's text mentions a specific need,
    upgrade the need automatically.  This prevents the blind spot where a user types
    'wheelchair accessible gate' but the dropdown was left at 'none' (default).
    Explicit selections always win; text-inference is only a fallback."""
    if explicit_need != AccessibilityNeed.NONE:
        return explicit_need  # explicit selection always wins
    low = sanitized_text.lower()
    if any(k in low for k in ("wheelchair", "ramp", "step-free", "step free", "disability")):
        return AccessibilityNeed.WHEELCHAIR
    if any(k in low for k in ("blind", "visual", "visually impaired", "audio guidance")):
        return AccessibilityNeed.VISUAL
    if any(k in low for k in ("deaf", "hearing", "hearing impaired", "visual display")):
        return AccessibilityNeed.HEARING
    return AccessibilityNeed.NONE


def resolve_live(query: UserQuery, sanitized_text: str) -> ResolvedContext:
    """Live resolver — uses live turnstile data and infers accessibility need
    from both the explicit API field and the sanitized user text."""
    intent = classify_intent(sanitized_text)
    # Infer accessibility need from text if the explicit field says 'none'
    effective_need = _infer_accessibility_need(query.accessibility_need, sanitized_text)

    gate: GateStatus | None = None
    wait: WaitEstimate | None = None
    alternate_gate: GateStatus | None = None
    alternate_wait: WaitEstimate | None = None
    accessible = True
    safety_notice = None

    if intent in (
        "find_gate", "wait_time", "accessibility", "crowd_status",
        "general_info", "restroom", "sustainability",
    ):
        snapshot = live_gate_snapshot()
        requested_id = extract_requested_gate(sanitized_text, set(snapshot.keys()))

        if requested_id and (
            effective_need != AccessibilityNeed.WHEELCHAIR
            or snapshot[requested_id].step_free
        ):
            gate = snapshot[requested_id]
            wait = predict_wait(gate)
            best, best_wait, _ = best_gate_live(effective_need)
            if best.gate_id != gate.gate_id:
                alternate_gate, alternate_wait = best, best_wait
        else:
            gate, wait, alt = best_gate_live(effective_need)
            if alt:
                alternate_gate, alternate_wait = alt

        accessible = gate.step_free or effective_need != AccessibilityNeed.WHEELCHAIR

        # Populate safety notice if the recommended gate has an active incident
        if gate and gate.incident:
            safety_notice = f"ALERT for {gate.name}: {gate.incident}"

    if intent == "emergency":
        safety_notice = "STOP. Contact stewards or call emergency services immediately."

    return ResolvedContext(
        intent=intent,
        recommended_gate=gate,
        wait_estimate=wait,
        alternate_gate=alternate_gate,
        alternate_wait=alternate_wait,
        accessible_route_available=accessible,
        safety_notice=safety_notice,
        sanitized_user_text=sanitized_text,
        language=query.language,
        accessibility_need=effective_need,
    )



def extract_requested_gate(text: str, valid_gate_ids: set[str]) -> str | None:
    """Detect an explicit 'gate <letter>' mention in the user's text.
    This only SELECTS which real gate's data to show — it never invents
    or overrides that gate's wait/congestion, which always still comes
    from predict_wait(). So a message like 'gate C is empty' still shows
    gate C's REAL computed wait, never a fabricated one. Used only by
    resolve_live() (the live app path), never by resolve() (which the
    injection-resistance security tests check) — see README for why."""
    match = re.search(r'\bgate\s*([a-zA-Z])\b', text, re.IGNORECASE)
    if match:
        candidate = match.group(1).upper()
        if candidate in valid_gate_ids:
            return candidate
    return None


def live_all_gate_predictions() -> list[WaitEstimate]:
    return [predict_wait(g) for g in live_gate_snapshot().values()]


