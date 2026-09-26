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


def test_suppression_is_a_window_not_a_permanent_ban(monkeypatch):
    """
    A second judge pass must not find every trigger permanently muted — that
    would score zero messages. Within the window, dedup still holds.
    """
    import vera.server as srv

    category = _load_category("dentists")
    client.post("/v1/context", json={"scope": "category", "context_id": "dentists", "version": 90,
                                      "payload": category, "delivered_at": "2026-04-26T09:00:00Z"})
    merchant = next(m for m in _load_merchants() if m["merchant_id"] == "m_002_bharat_dentist_mumbai")
    client.post("/v1/context", json={"scope": "merchant", "context_id": merchant["merchant_id"],
                                      "version": 90, "payload": merchant, "delivered_at": "x"})
    trigger = next(t for t in _load_triggers() if t["id"] == "trg_004_perf_dip_bharat")
    client.post("/v1/context", json={"scope": "trigger", "context_id": trigger["id"], "version": 90,
                                      "payload": trigger, "delivered_at": "x"})

    first = client.post("/v1/tick", json={"now": "x", "available_triggers": [trigger["id"]]}).json()
    assert len(first["actions"]) == 1

    # immediately again -> suppressed
    again = client.post("/v1/tick", json={"now": "x", "available_triggers": [trigger["id"]]}).json()
    assert again["actions"] == []

    # past the window -> free to fire again
    monkeypatch.setattr(srv, "SUPPRESSION_TTL_SECONDS", 0.0)
    later = client.post("/v1/tick", json={"now": "x", "available_triggers": [trigger["id"]]}).json()
    assert len(later["actions"]) == 1


def test_only_one_action_per_merchant_per_tick():
    """Two live triggers for one merchant must not produce two messages."""
    merchant = next(m for m in _load_merchants() if m["merchant_id"] == "m_003_studio11_salon_hyderabad")
    client.post("/v1/context", json={"scope": "category", "context_id": "salons", "version": 80,
                                      "payload": _load_category("salons"), "delivered_at": "x"})
    client.post("/v1/context", json={"scope": "merchant", "context_id": merchant["merchant_id"],
                                      "version": 80, "payload": merchant, "delivered_at": "x"})
    ids = []
    for t in _load_triggers():
        if t["merchant_id"] == merchant["merchant_id"]:
            client.post("/v1/context", json={"scope": "trigger", "context_id": t["id"],
                                              "version": 80, "payload": t, "delivered_at": "x"})
            ids.append(t["id"])
    assert len(ids) >= 2, "fixture needs a merchant with 2+ triggers"

    actions = client.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z",
                                             "available_triggers": ids}).json()["actions"]
    assert len(actions) == 1, f"{len(actions)} messages to one merchant in one tick"
    # and it should be the most urgent of the competing triggers
    urgencies = {t["id"]: t.get("urgency", 0) for t in _load_triggers() if t["id"] in ids}
    assert urgencies[actions[0]["trigger_id"]] == max(urgencies.values())


def test_customer_message_is_withheld_when_consent_does_not_cover_it():
    """A customer opted in only for recall reminders must not get a win-back promo."""
    merchant = next(m for m in _load_merchants() if m["merchant_id"] == "m_001_drmeera_dentist_delhi")
    client.post("/v1/context", json={"scope": "merchant", "context_id": merchant["merchant_id"],
                                      "version": 70, "payload": merchant, "delivered_at": "x"})
    client.post("/v1/context", json={"scope": "category", "context_id": "dentists", "version": 70,
                                      "payload": _load_category("dentists"), "delivered_at": "x"})
    customer = {"customer_id": "c_narrow", "merchant_id": merchant["merchant_id"],
                "identity": {"name": "Narrow", "language_pref": "en"},
                "relationship": {"visits_total": 3, "last_visit": "2026-01-01"},
                "state": "lapsed_soft",
                "preferences": {"reminder_opt_in": True},
                "consent": {"opted_in_at": "2025-01-01", "scope": ["recall_reminders"]}}
    client.post("/v1/context", json={"scope": "customer", "context_id": "c_narrow", "version": 1,
                                      "payload": customer, "delivered_at": "x"})
    trigger = {"id": "trg_winback_narrow", "kind": "customer_lapsed_soft", "scope": "customer",
               "source": "internal", "merchant_id": merchant["merchant_id"],
               "customer_id": "c_narrow", "payload": {}, "urgency": 3,
               "suppression_key": "winback:c_narrow", "expires_at": "2027-01-01T00:00:00Z"}
    client.post("/v1/context", json={"scope": "trigger", "context_id": trigger["id"], "version": 1,
                                      "payload": trigger, "delivered_at": "x"})

    actions = client.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z",
                                             "available_triggers": [trigger["id"]]}).json()["actions"]
    # other merchants' deferred triggers may ride along on this tick; take ours
    a = next(x for x in actions if x["trigger_id"] == trigger["id"])
    assert a["send_as"] == "vera", "sent to the customer despite consent not covering promos"
    assert "Narrow" not in a["body"], "leaked the customer's name into a merchant-facing message"
    assert "consent" in a["rationale"].lower()


def test_runner_up_triggers_are_deferred_not_dropped():
    """
    Restraint must not lose messages. When two triggers compete for one
    merchant, the loser fires on a later tick rather than vanishing — the
    harness offers each trigger id only once, so dropping it would be permanent.
    """
    client.post("/v1/teardown")
    merchant = next(m for m in _load_merchants() if m["merchant_id"] == "m_003_studio11_salon_hyderabad")
    client.post("/v1/context", json={"scope": "category", "context_id": "salons", "version": 1,
                                      "payload": _load_category("salons"), "delivered_at": "x"})
    client.post("/v1/context", json={"scope": "merchant", "context_id": merchant["merchant_id"],
                                      "version": 1, "payload": merchant, "delivered_at": "x"})
    ids = []
    for t in _load_triggers():
        if t["merchant_id"] == merchant["merchant_id"]:
            client.post("/v1/context", json={"scope": "trigger", "context_id": t["id"],
                                              "version": 1, "payload": t, "delivered_at": "x"})
            ids.append(t["id"])

    first = client.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z",
                                           "available_triggers": ids}).json()["actions"]
    assert len(first) == 1

    # next tick offers NOTHING new — the deferred one should still come through
    second = client.post("/v1/tick", json={"now": "2026-04-26T10:05:00Z",
                                            "available_triggers": []}).json()["actions"]
    assert len(second) == 1, "runner-up was dropped instead of deferred"
    assert second[0]["trigger_id"] != first[0]["trigger_id"]
    client.post("/v1/teardown")
