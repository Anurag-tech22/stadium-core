"""Firestore client with graceful local fallback.

Design contract
---------------
- If GOOGLE_CLOUD_PROJECT is set and google-cloud-firestore is installed,
  ALL reads/writes go to Firestore.  No local files are touched.
- If GOOGLE_CLOUD_PROJECT is NOT set (local dev, CI, tests), the module
  returns None from get_client() and the caller falls back to the existing
  JSON-file behaviour.  No import error is raised.
- The module is imported at startup but the Firestore client is lazily
  initialised on the first call to get_client() so the server starts fast.

Firestore document layout
--------------------------
  Collection: venues
    Document:  {VENUE_ID}          (default: "phoenix-001")
      Field:   gates        list[map]   — same shape as venues.json
      Field:   phases       list[map]   — matchday_schedule.phases

  Collection: gate_overrides
    Document:  {gate_id}    (e.g. "A", "B", …)
      Fields:  arrivals_per_min, capacity_per_min, servers_open, incident

Environment variables
---------------------
  GOOGLE_CLOUD_PROJECT   GCP project ID  (required to enable Firestore mode)
  FIRESTORE_VENUE_ID     Venue document ID in the 'venues' collection
                         (default: "phoenix-001")
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("phoenix.firestore")

# Firestore collection / document names
_VENUES_COLLECTION = "venues"
_OVERRIDES_COLLECTION = "gate_overrides"
_DEFAULT_VENUE_ID = "phoenix-001"

# Lazily initialised; None means "local mode"
_client: Any = None
_initialised = False


def get_client() -> Any | None:  # noqa: ANN401
    """Return a Firestore client, or None if GCP is not configured.

    Lazy singleton: created on first call, cached forever.
    Thread-safe in CPython due to the GIL; acceptable for our use case.
    """
    global _client, _initialised
    if _initialised:
        return _client

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    if not project:
        logger.info(
            "GOOGLE_CLOUD_PROJECT not set — running in local mode (venues.json + overrides_db.json)"
        )
        _initialised = True
        return None

    try:
        from google.cloud import firestore

        _client = firestore.Client(project=project)
        logger.info(
            "Firestore client initialised for project=%s venue=%s",
            project,
            venue_id(),
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "Failed to initialise Firestore client (%s) — falling back to local mode",
            exc,
        )
        _client = None

    _initialised = True
    return _client


def venue_id() -> str:
    """Firestore document ID for this venue."""
    return os.environ.get("FIRESTORE_VENUE_ID", _DEFAULT_VENUE_ID)


# ── Venue / gate reads ────────────────────────────────────────────────────────


def fetch_venue_config() -> dict | None:
    """Fetch raw venue config dict from Firestore.

    Returns None if Firestore is unavailable (caller falls back to venues.json).
    """
    client = get_client()
    if client is None:
        return None
    try:
        doc = client.collection(_VENUES_COLLECTION).document(venue_id()).get()
        if doc.exists:
            val = doc.to_dict()
            if isinstance(val, dict):
                return val
        logger.warning(
            "Firestore venue document '%s' not found in collection '%s' — "
            "will seed from venues.json and fall back",
            venue_id(),
            _VENUES_COLLECTION,
        )
        return None
    except Exception as exc:  # pragma: no cover
        logger.warning("Firestore fetch_venue_config failed: %s", exc)
        return None


def seed_venue_config(raw: dict) -> None:
    """Write venues.json content to Firestore on first deploy.

    Safe to call multiple times — uses set(merge=True) so existing fields
    are preserved.  Only runs when GOOGLE_CLOUD_PROJECT is set.
    """
    client = get_client()
    if client is None:
        return
    try:
        client.collection(_VENUES_COLLECTION).document(venue_id()).set(raw, merge=True)
        logger.info("Seeded Firestore venue document '%s' from venues.json", venue_id())
    except Exception as exc:  # pragma: no cover
        logger.warning("Firestore seed_venue_config failed: %s", exc)


# ── Gate override reads / writes ──────────────────────────────────────────────


def fetch_all_overrides() -> dict[str, dict[str, Any]]:
    """Fetch all gate overrides from Firestore.

    Returns {} if Firestore is unavailable (caller uses overrides_db.json).
    """
    client = get_client()
    if client is None:
        return {}
    try:
        docs = client.collection(_OVERRIDES_COLLECTION).stream()
        result: dict[str, dict[str, Any]] = {}
        for doc in docs:
            result[doc.id] = doc.to_dict() or {}
        return result
    except Exception as exc:  # pragma: no cover
        logger.warning("Firestore fetch_all_overrides failed: %s", exc)
        return {}


def _sanitize_log(val: object) -> str:
    """Sanitize values for logging to prevent Log Injection (CRLF injection)."""
    return str(val).replace("\n", "\\n").replace("\r", "\\r")


def write_gate_override(gate_id: str, fields: dict[str, Any]) -> None:
    """Write (merge) override fields for a single gate to Firestore.

    Uses set(merge=True) so only the provided fields are updated; other
    fields on the document are left intact.
    """
    client = get_client()
    if client is None:
        return
    try:
        client.collection(_OVERRIDES_COLLECTION).document(gate_id).set(fields, merge=True)
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "Firestore write_gate_override(%s) failed: %s",
            _sanitize_log(gate_id),
            exc,
        )
