import json
from pathlib import Path

from fastapi.testclient import TestClient

from vera.server import app

DATASET = Path(__file__).parent.parent / "dataset"


def make_client():
    # fresh app state per test module run is fine — TestClient shares the
    # imported app's module-level dicts, so tests below reuse/extend state
    # deliberately (mirrors how one long-lived bot process is actually tested).
    return TestClient(app)


client = make_client()


def _load_category(slug):
    return json.load(open(DATASET / "categories" / f"{slug}.json", encoding="utf-8"))


def _load_merchants():
    return json.load(open(DATASET / "merchants_seed.json", encoding="utf-8"))["merchants"]


def _load_triggers():
    return json.load(open(DATASET / "triggers_seed.json", encoding="utf-8"))["triggers"]


def test_healthz_starts_at_zero_ish_and_reports_counts():
    r = client.get("/v1/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "contexts_loaded" in body


def test_metadata_has_required_fields():
    r = client.get("/v1/metadata")
    assert r.status_code == 200
    body = r.json()
    for key in ("team_name", "model", "approach", "contact_email", "version"):
        assert key in body


def test_context_push_idempotent_and_version_bump():
    payload = {"slug": "dentists", "voice": {}, "offer_catalog": [], "peer_stats": {}, "digest": []}
    r1 = client.post("/v1/context", json={
        "scope": "category", "context_id": "dentists", "version": 1,
        "payload": payload, "delivered_at": "2026-04-26T09:45:00Z",
    })
    assert r1.status_code == 200
    assert r1.json()["accepted"] is True

    # re-post same version -> 409 stale
    r2 = client.post("/v1/context", json={
        "scope": "category", "context_id": "dentists", "version": 1,
        "payload": payload, "delivered_at": "2026-04-26T09:45:00Z",
    })
    assert r2.status_code == 409
    assert r2.json()["reason"] == "stale_version"

    # version bump -> accepted
    r3 = client.post("/v1/context", json={
        "scope": "category", "context_id": "dentists", "version": 2,
        "payload": payload, "delivered_at": "2026-04-26T10:00:00Z",
    })
    assert r3.status_code == 200


def test_invalid_scope_is_400():
    r = client.post("/v1/context", json={
        "scope": "not_a_scope", "context_id": "x", "version": 1, "payload": {},
    })
    assert r.status_code == 400
    assert r.json()["accepted"] is False


def test_full_tick_and_reply_flow():
    category = _load_category("dentists")
    client.post("/v1/context", json={"scope": "category", "context_id": "dentists", "version": 10,
                                      "payload": category, "delivered_at": "2026-04-26T09:00:00Z"})

    merchant = next(m for m in _load_merchants() if m["merchant_id"] == "m_001_drmeera_dentist_delhi")
    client.post("/v1/context", json={"scope": "merchant", "context_id": merchant["merchant_id"], "version": 1,
                                      "payload": merchant, "delivered_at": "2026-04-26T09:00:00Z"})

    trigger = next(t for t in _load_triggers() if t["id"] == "trg_001_research_digest_dentists")
    client.post("/v1/context", json={"scope": "trigger", "context_id": trigger["id"], "version": 1,
                                      "payload": trigger, "delivered_at": "2026-04-26T09:00:00Z"})

    r = client.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z",
                                       "available_triggers": [trigger["id"]]})
    assert r.status_code == 200
    actions = r.json()["actions"]
    assert len(actions) == 1
    action = actions[0]
    assert action["merchant_id"] == merchant["merchant_id"]
    assert "JIDA" in action["body"]
    conv_id = action["conversation_id"]

    # a second tick on the same trigger must NOT double-send (suppression)
    r2 = client.post("/v1/tick", json={"now": "2026-04-26T10:05:00Z",
                                        "available_triggers": [trigger["id"]]})
    assert r2.json()["actions"] == []

    # reply flow
    r3 = client.post("/v1/reply", json={
        "conversation_id": conv_id, "merchant_id": merchant["merchant_id"], "customer_id": None,
        "from_role": "merchant", "message": "Yes please send the abstract",
        "received_at": "2026-04-26T10:10:00Z", "turn_number": 2,
    })
    assert r3.status_code == 200
    assert r3.json()["action"] == "send"


def test_tick_returns_empty_actions_when_no_triggers_known():
    r = client.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z", "available_triggers": ["trg_never_pushed"]})
    assert r.status_code == 200
    assert r.json()["actions"] == []


def test_reply_on_unknown_conversation_id_does_not_crash():
    r = client.post("/v1/reply", json={
        "conversation_id": "conv_never_seen", "merchant_id": None, "customer_id": None,
        "from_role": "merchant", "message": "hello?", "received_at": "2026-04-26T10:00:00Z", "turn_number": 1,
    })
    assert r.status_code == 200
    assert r.json()["action"] in ("send", "wait", "end")
