import json
from pathlib import Path

from vera.gating import (customer_consent_ok, is_expired, parse_iso, select_best,
                         template_params)

DS = Path(__file__).parent.parent / "dataset"
CUSTOMERS = {c["customer_id"]: c for c in json.load(open(DS / "customers_seed.json", encoding="utf-8"))["customers"]}
TRIGGERS = {t["id"]: t for t in json.load(open(DS / "triggers_seed.json", encoding="utf-8"))["triggers"]}


# --- consent ---------------------------------------------------------------

def test_every_seeded_customer_trigger_pair_is_allowed():
    """
    Consent vocabulary varies per merchant (refill_reminders, winback_offers,
    kids_program_updates...). Each shipped customer holds the scope that matches
    their own trigger, so a correct gate lets all of them through.
    """
    blocked = []
    for t in TRIGGERS.values():
        cust = CUSTOMERS.get(t.get("customer_id") or "")
        if not cust:
            continue
        allowed, reason = customer_consent_ok(t, cust)
        if not allowed:
            blocked.append((t["kind"], cust["identity"]["name"], reason))
    assert not blocked, f"over-blocked: {blocked}"


def test_promo_is_blocked_for_a_reminders_only_customer():
    narrow = {"identity": {"name": "N"}, "preferences": {"reminder_opt_in": True},
              "consent": {"scope": ["recall_reminders"]}, "state": "lapsed_soft"}
    allowed, _ = customer_consent_ok({"kind": "customer_lapsed_soft"}, narrow)
    assert not allowed


def test_opt_out_and_churn_block_everything():
    optout = {"identity": {}, "preferences": {"reminder_opt_in": False},
              "consent": {"scope": ["promotional_offers"]}, "state": "active"}
    churned = {"identity": {}, "preferences": {"reminder_opt_in": True},
               "consent": {"scope": ["promotional_offers"]}, "state": "churned"}
    assert not customer_consent_ok({"kind": "recall_due"}, optout)[0]
    assert not customer_consent_ok({"kind": "recall_due"}, churned)[0]
    assert not customer_consent_ok({"kind": "recall_due"}, None)[0]


def test_unseen_scope_vocabulary_matches_on_keyword():
    """The judge injects customers we've never seen; scope names will differ."""
    unseen = {"identity": {}, "preferences": {"reminder_opt_in": True},
              "consent": {"scope": ["physio_recall_notices"]}, "state": "lapsed_soft"}
    assert customer_consent_ok({"kind": "recall_due"}, unseen)[0]
    # but an unrelated scope still doesn't unlock a promo
    unrelated = {"identity": {}, "preferences": {"reminder_opt_in": True},
                 "consent": {"scope": ["invoice_notifications"]}, "state": "lapsed_soft"}
    assert not customer_consent_ok({"kind": "customer_lapsed_soft"}, unrelated)[0]


def test_missing_consent_block_is_refused():
    assert not customer_consent_ok({"kind": "recall_due"}, {"identity": {}, "state": "active"})[0]


# --- expiry ----------------------------------------------------------------

def test_expiry_compares_against_the_judges_simulated_now():
    t = {"id": "t", "expires_at": "2026-05-03T00:00:00Z"}
    assert not is_expired(t, "2026-04-26T10:00:00Z")
    assert is_expired(t, "2026-06-01T10:00:00Z")


def test_unparseable_or_missing_expiry_is_not_treated_as_expired():
    assert not is_expired({"id": "t"}, "2026-04-26T10:00:00Z")
    assert not is_expired({"id": "t", "expires_at": "not-a-date"}, "2026-04-26T10:00:00Z")
    assert parse_iso("garbage") is None


def test_live_trigger_beats_expired_but_expired_still_fires_when_alone():
    live = {"id": "live", "urgency": 1, "expires_at": "2027-01-01T00:00:00Z"}
    dead = {"id": "dead", "urgency": 5, "expires_at": "2020-01-01T00:00:00Z"}
    best, past = select_best([dead, live], "2026-04-26T10:00:00Z")
    assert best["id"] == "live" and not past, "an expired trigger outranked a live one"
    best, past = select_best([dead], "2026-04-26T10:00:00Z")
    assert best["id"] == "dead" and past, "went silent when the only trigger was stale"


def test_higher_urgency_wins_among_live_triggers():
    low = {"id": "low", "urgency": 1, "expires_at": "2027-01-01T00:00:00Z"}
    high = {"id": "high", "urgency": 4, "expires_at": "2027-01-01T00:00:00Z"}
    assert select_best([low, high], "2026-04-26T10:00:00Z")[0]["id"] == "high"


def test_selection_is_deterministic():
    ts = [{"id": f"t{i}", "urgency": 3, "expires_at": "2027-01-01T00:00:00Z"} for i in range(5)]
    assert {select_best(list(reversed(ts)), "2026-04-26T10:00:00Z")[0]["id"] for _ in range(5)} == {"t0"}


# --- template params -------------------------------------------------------

def test_template_params_split_into_salutation_hook_and_cta():
    merchant = {"identity": {"name": "Dr. Meera's Dental Clinic", "owner_first_name": "Meera"}}
    body = "Meera, Your calls dropped 50% over the last 7d. Want me to look into it?"
    name, hook, cta = template_params(body, merchant, None)
    assert name == "Meera"
    assert "50%" in hook and name not in hook
    assert cta.endswith("?")
