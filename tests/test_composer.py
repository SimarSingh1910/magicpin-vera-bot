import json
from pathlib import Path

import pytest

from vera.composer import compose

DATASET = Path(__file__).parent.parent / "dataset"


def load_dataset():
    categories = {}
    for f in (DATASET / "categories").glob("*.json"):
        d = json.load(open(f, encoding="utf-8"))
        categories[d["slug"]] = d
    merchants = {m["merchant_id"]: m for m in json.load(open(DATASET / "merchants_seed.json", encoding="utf-8"))["merchants"]}
    customers = {c["customer_id"]: c for c in json.load(open(DATASET / "customers_seed.json", encoding="utf-8"))["customers"]}
    triggers = {t["id"]: t for t in json.load(open(DATASET / "triggers_seed.json", encoding="utf-8"))["triggers"]}
    return categories, merchants, customers, triggers


CATEGORIES, MERCHANTS, CUSTOMERS, TRIGGERS = load_dataset()


@pytest.mark.parametrize("trigger_id", list(TRIGGERS.keys()))
def test_compose_every_seed_trigger_produces_a_valid_message(trigger_id):
    trigger = TRIGGERS[trigger_id]
    merchant = MERCHANTS.get(trigger["merchant_id"])
    if merchant is None:
        pytest.skip("merchant not in the 10-merchant seed subset")
    category = CATEGORIES.get(merchant["category_slug"])
    customer = CUSTOMERS.get(trigger.get("customer_id")) if trigger.get("customer_id") else None

    result = compose(category, merchant, trigger, customer)

    assert result["body"], "body must not be empty"
    assert result["cta"] in ("binary_yes_no", "open_ended", "none")
    assert result["send_as"] in ("vera", "merchant_on_behalf")
    assert (result["send_as"] == "merchant_on_behalf") == (customer is not None)
    assert result["suppression_key"]
    assert result["rationale"]
    # single primary CTA: at most one '?' in the whole body
    assert result["body"].count("?") <= 1


def test_research_digest_cites_the_real_source_not_a_fabricated_one():
    trigger = TRIGGERS["trg_001_research_digest_dentists"]
    merchant = MERCHANTS["m_001_drmeera_dentist_delhi"]
    category = CATEGORIES["dentists"]

    result = compose(category, merchant, trigger, None)

    assert "JIDA Oct 2026, p.14" in result["body"]
    assert "2,100" in result["body"] or "2100" in result["body"]


def test_no_offer_is_invented_when_merchant_has_no_active_offers():
    merchant = dict(MERCHANTS["m_002_bharat_dentist_mumbai"])
    assert merchant["offers"] == []  # sanity check on the fixture
    category = CATEGORIES["dentists"]
    # generic fallback handler is the one that cites an active offer when present
    trigger = {"id": "trg_fake", "kind": "not_a_real_kind", "scope": "merchant",
               "source": "internal", "merchant_id": merchant["merchant_id"],
               "payload": {}, "urgency": 1, "suppression_key": "fake:bharat"}

    result = compose(category, merchant, trigger, None)

    assert "₹" not in result["body"]  # no offer to cite -> none invented


def test_recall_due_uses_real_slot_labels():
    trigger = TRIGGERS["trg_003_recall_due_priya"]
    merchant = MERCHANTS["m_001_drmeera_dentist_delhi"]
    customer = CUSTOMERS["c_001_priya_for_m001"]
    category = CATEGORIES["dentists"]

    result = compose(category, merchant, trigger, customer)

    assert result["send_as"] == "merchant_on_behalf"
    assert "Wed 5 Nov, 6pm" in result["body"]
    assert "Thu 6 Nov, 5pm" in result["body"]


def test_taboo_words_never_appear_for_dentists():
    category = CATEGORIES["dentists"]
    taboo = category["voice"]["vocab_taboo"]
    for trigger in TRIGGERS.values():
        merchant = MERCHANTS.get(trigger["merchant_id"])
        if not merchant or merchant["category_slug"] != "dentists":
            continue
        result = compose(category, merchant, trigger, None)
        low = result["body"].lower()
        for word in taboo:
            assert word.lower() not in low, f"taboo word '{word}' leaked into: {result['body']}"


def test_unknown_trigger_kind_falls_back_gracefully_without_crashing():
    merchant = MERCHANTS["m_001_drmeera_dentist_delhi"]
    category = CATEGORIES["dentists"]
    trigger = {"id": "trg_fake", "kind": "some_future_kind_not_yet_handled",
               "scope": "merchant", "source": "internal", "merchant_id": merchant["merchant_id"],
               "payload": {}, "urgency": 1, "suppression_key": "fake:1"}

    result = compose(category, merchant, trigger, None)
    assert result["body"]


def test_missing_category_does_not_crash():
    merchant = MERCHANTS["m_001_drmeera_dentist_delhi"]
    trigger = TRIGGERS["trg_001_research_digest_dentists"]
    result = compose(None, merchant, trigger, None)
    assert result["body"]


# --------------------------------------------------------------------------
# regression tests for defects found during the first scored evaluation pass
# --------------------------------------------------------------------------

def test_perf_dip_with_no_payload_numbers_never_claims_a_drop_that_didnt_happen():
    """A perf_dip trigger with a placeholder payload must not report a rise as a fall."""
    merchant = {
        "merchant_id": "m_test", "category_slug": "salons",
        "identity": {"name": "Test Salon", "owner_first_name": "Sushma", "languages": ["en"]},
        "performance": {"views": 2547, "calls": 22, "ctr": 0.058,
                        "delta_7d": {"views_pct": 0.08, "calls_pct": 0.02}},  # both UP
        "offers": [], "signals": [],
    }
    trigger = {"id": "t", "kind": "perf_dip", "scope": "merchant", "source": "internal",
               "merchant_id": "m_test", "payload": {"placeholder": True}, "urgency": 3,
               "suppression_key": "k"}

    body = compose(CATEGORIES["salons"], merchant, trigger, None)["body"].lower()

    assert "down" not in body and "dropped" not in body, f"claimed a drop that didn't happen: {body}"
    assert "2,547" in body  # still anchored on a real number


def test_no_internal_slug_leaks_into_any_message():
    """snake_case payload values / signal names must never reach the merchant."""
    import re
    leaks = []
    for trigger in TRIGGERS.values():
        merchant = MERCHANTS.get(trigger["merchant_id"])
        if not merchant:
            continue
        category = CATEGORIES.get(merchant["category_slug"])
        customer = CUSTOMERS.get(trigger.get("customer_id")) if trigger.get("customer_id") else None
        body = compose(category, merchant, trigger, customer)["body"]
        if re.search(r"[a-z]+_[a-z]+", body):
            leaks.append((trigger["id"], body))
    assert not leaks, f"internal slugs leaked: {leaks}"


def test_returning_customer_skips_first_timer_offers_but_keeps_price_anchors():
    gym = {
        "merchant_id": "m_gym", "category_slug": "gyms",
        "identity": {"name": "Zen Yoga", "owner_first_name": "Padma", "languages": ["en"]},
        "performance": {}, "signals": [],
        "offers": [{"id": "o1", "title": "First Month @ ₹499", "status": "active"},
                   {"id": "o2", "title": "Free Body Composition Analysis", "status": "active"}],
    }
    loyal = {"customer_id": "c", "merchant_id": "m_gym",
             "identity": {"name": "Diya", "language_pref": "en"},
             "relationship": {"visits_total": 9, "last_visit": "2026-04-01"}, "state": "lapsed_soft"}
    trigger = {"id": "t", "kind": "recall_due", "scope": "customer", "source": "internal",
               "merchant_id": "m_gym", "customer_id": "c", "payload": {"placeholder": True},
               "urgency": 3, "suppression_key": "k"}

    body = compose(CATEGORIES["gyms"], gym, trigger, loyal)["body"]
    assert "First Month" not in body, f"pitched a first-timer offer at a 9-visit member: {body}"

    # but a plain service-at-price offer IS still quoted to a returning patient
    meera = MERCHANTS["m_001_drmeera_dentist_delhi"]
    priya = CUSTOMERS["c_001_priya_for_m001"]
    recall = TRIGGERS["trg_003_recall_due_priya"]
    body = compose(CATEGORIES["dentists"], meera, recall, priya)["body"]
    assert "₹299" in body, f"dropped the price anchor for a returning patient: {body}"


def test_milestone_not_yet_reached_is_framed_as_approaching():
    merchant = MERCHANTS["m_001_drmeera_dentist_delhi"]
    trigger = {"id": "t", "kind": "milestone_reached", "scope": "merchant", "source": "internal",
               "merchant_id": merchant["merchant_id"], "urgency": 1, "suppression_key": "k",
               "payload": {"metric": "review_count", "value_now": 145,
                           "milestone_value": 150, "is_imminent": True}}

    body = compose(CATEGORIES["dentists"], merchant, trigger, None)["body"]

    assert "crossed 150" not in body and "crossed 145" not in body, body
    assert "145" in body and "150" in body


def test_customer_facing_message_identifies_the_business():
    meera = MERCHANTS["m_001_drmeera_dentist_delhi"]
    priya = CUSTOMERS["c_001_priya_for_m001"]
    body = compose(CATEGORIES["dentists"], meera, TRIGGERS["trg_003_recall_due_priya"], priya)["body"]
    assert "Dr. Meera's Dental Clinic" in body, f"customer can't tell who's writing: {body}"


def test_hindi_preference_gets_code_mixed_ask():
    meera = MERCHANTS["m_001_drmeera_dentist_delhi"]  # languages: en, hi
    en_only = dict(meera, identity=dict(meera["identity"], languages=["en"]))
    trigger = TRIGGERS["trg_002_compliance_dci_radiograph"]

    hi_body = compose(CATEGORIES["dentists"], meera, trigger, None)["body"]
    en_body = compose(CATEGORIES["dentists"], en_only, trigger, None)["body"]

    assert hi_body != en_body, "language preference made no difference"
    assert "Chahenge" in hi_body or "kar dun" in hi_body, hi_body
    assert "Want me to" in en_body, en_body
    # the factual half stays identical and in English either way
    for fact in ("1.0 mSv", "2026-12-15"):
        assert fact in hi_body and fact in en_body


def test_placeholder_event_payload_does_not_dredge_up_an_out_of_season_beat():
    merchant = MERCHANTS["m_007_powerhouse_gym_bangalore"] if "m_007_powerhouse_gym_bangalore" in MERCHANTS \
        else next(m for m in MERCHANTS.values() if m["category_slug"] == "gyms")
    trigger = {"id": "t", "kind": "festival_upcoming", "scope": "merchant", "source": "external",
               "merchant_id": merchant["merchant_id"], "urgency": 1, "suppression_key": "k",
               "payload": {"placeholder": True, "metric_or_topic": "festival_upcoming"}}

    body = compose(CATEGORIES["gyms"], merchant, trigger, None)["body"]

    assert "Jan" not in body and "resolution surge" not in body, f"cited an out-of-season beat: {body}"


def test_customer_scoped_trigger_without_customer_context_talks_to_the_merchant():
    """
    The judge harness pushes categories, merchants and triggers — never
    customers. A customer-scoped trigger must then become a merchant-facing
    opportunity nudge, not a message addressed to the merchant as if they were
    the patient.
    """
    merchant = MERCHANTS["m_001_drmeera_dentist_delhi"]
    trigger = TRIGGERS["trg_003_recall_due_priya"]

    result = compose(CATEGORIES["dentists"], merchant, trigger, None)  # no customer

    assert result["send_as"] == "vera", "with no customer context this must be merchant-facing"
    body = result["body"]
    assert "your last visit" not in body.lower(), f"addressed the dentist as the patient: {body}"
    assert "patient" in body.lower() or "customer" in body.lower()
    assert "Wed 5 Nov, 6pm" in body, "real slot data from the payload should still be used"
    # and it must not name or invent a customer we were never given
    assert "Priya" not in body


def test_customer_scoped_trigger_with_customer_context_still_writes_to_the_customer():
    merchant = MERCHANTS["m_001_drmeera_dentist_delhi"]
    trigger = TRIGGERS["trg_003_recall_due_priya"]
    customer = CUSTOMERS["c_001_priya_for_m001"]

    result = compose(CATEGORIES["dentists"], merchant, trigger, customer)

    assert result["send_as"] == "merchant_on_behalf"
    assert "Priya" in result["body"]
