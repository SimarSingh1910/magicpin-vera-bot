from vera.conversation import new_state, respond

MERCHANT = {
    "merchant_id": "m_001_drmeera_dentist_delhi",
    "identity": {"name": "Dr. Meera's Dental Clinic", "owner_first_name": "Meera", "languages": ["en", "hi"]},
    "offers": [{"id": "o1", "title": "Dental Cleaning @ ₹299", "status": "active"}],
}
CATEGORY = {"slug": "dentists"}


def make_state(conv_id="conv_1", auto_reply_hits=None):
    return new_state(conv_id, MERCHANT["merchant_id"], None, {}, MERCHANT, CATEGORY,
                      first_body="Dr. Meera, quick nudge on your profile.",
                      auto_reply_hits=auto_reply_hits if auto_reply_hits is not None else {})


def test_auto_reply_first_hit_waits_second_hit_ends():
    # mirrors server.py: one shared auto_reply_hits dict per merchant_id, even
    # across different conversation_ids, since a canned WA Business reply
    # doesn't know or care which of our conversation_ids it's replying to.
    shared_hits: dict = {}
    canned = "Thank you for contacting us! Our team will respond shortly."

    state1 = make_state("conv_1", shared_hits)
    r1 = respond(state1, canned)
    assert r1["action"] == "wait"

    state2 = make_state("conv_2", shared_hits)
    r2 = respond(state2, canned)
    assert r2["action"] == "end", "second occurrence of the same canned reply for this merchant must end, not loop"


def test_explicit_opt_out_ends_immediately():
    state = make_state()
    r = respond(state, "Not interested. Stop messaging me.")
    assert r["action"] == "end"


def test_hostile_language_ends_without_engaging():
    state = make_state()
    r = respond(state, "This is useless spam, stop.")
    assert r["action"] == "end"


def test_intent_commitment_switches_to_action_not_another_question():
    state = make_state()
    r = respond(state, "Ok lets do it. Whats next?")
    assert r["action"] == "send"
    body_low = r["body"].lower()
    qualifying = ["would you", "do you", "can you tell", "how about", "are you interested"]
    assert not any(p in body_low for p in qualifying), f"still qualifying after commitment: {r['body']}"
    assert any(w in body_low for w in ("done", "starting", "confirm")), r["body"]


def test_curveball_off_topic_declines_politely_and_redirects():
    state = make_state()
    r = respond(state, "Btw can you also help me with my GST filing this month?")
    assert r["action"] == "send"
    assert "gst" not in r["body"].lower() or "outside" in r["body"].lower()


def test_engaged_reply_never_resends_identical_body_twice():
    state = make_state()
    r1 = respond(state, "Yes please, send it over")
    r2 = respond(state, "sure, go ahead")
    assert r1["body"] != r2["body"]


def test_ended_conversation_stays_ended():
    state = make_state()
    respond(state, "Not interested. Stop messaging me.")
    r = respond(state, "Actually wait, I changed my mind")
    assert r["action"] == "end"


# --------------------------------------------------------------------------
# turn-2+ behaviour: answer the question, never repeat, wind down cleanly
# --------------------------------------------------------------------------

import json
from pathlib import Path

from vera.composer import compose

_DS = Path(__file__).parent.parent / "dataset"
_CATS = {json.load(open(f, encoding="utf-8"))["slug"]: json.load(open(f, encoding="utf-8"))
         for f in (_DS / "categories").glob("*.json")}
_MERCH = {m["merchant_id"]: m for m in json.load(open(_DS / "merchants_seed.json", encoding="utf-8"))["merchants"]}
_TRIGS = {t["id"]: t for t in json.load(open(_DS / "triggers_seed.json", encoding="utf-8"))["triggers"]}


def _conv(mid="m_001_drmeera_dentist_delhi", tid="trg_001_research_digest_dentists"):
    m, t = _MERCH[mid], _TRIGS[tid]
    c = _CATS[m["category_slug"]]
    first = compose(c, m, t, None)
    return new_state("c", mid, None, t, m, c, first["body"], {})


def test_every_turn_says_something_new():
    """The old bug: same canned body every turn. Each reply must differ."""
    st = _conv()
    bodies = []
    for msg in ["Interesting, tell me more", "How much would that cost me?",
                "Whats the source?", "And how long does it take?"]:
        r = respond(st, msg)
        if r.get("body"):
            bodies.append(r["body"])
    assert len(bodies) == len(set(bodies)), f"repeated a body: {bodies}"
    assert not any("following up again" in b for b in bodies), "fell back to the repeat suffix"


def test_source_question_is_answered_with_the_real_citation():
    st = _conv()
    r = respond(st, "Whats the source for that?")
    assert "JIDA Oct 2026, p.14" in r["body"], r["body"]


def test_price_question_answers_from_real_offers_only():
    st = _conv()
    r = respond(st, "How much will this cost me?")
    # the only price fact this merchant has is their live offer
    assert "₹299" in r["body"], r["body"]


def test_unanswerable_question_says_so_instead_of_inventing():
    """A merchant with no price data anywhere must not get a made-up figure."""
    bare = {"merchant_id": "m_bare", "category_slug": "dentists",
            "identity": {"name": "Bare Clinic", "owner_first_name": "Asha", "languages": ["en"]},
            "performance": {}, "offers": [], "signals": [], "customer_aggregate": {}}
    trigger = {"id": "t", "kind": "curious_ask_due", "scope": "merchant", "source": "internal",
               "merchant_id": "m_bare", "payload": {}, "urgency": 1, "suppression_key": "k"}
    st = new_state("c", "m_bare", None, trigger, bare, {"slug": "dentists"}, "opener", {})
    r = respond(st, "How much does it cost?")
    body = r.get("body", "")
    assert "₹" not in body, f"invented a price: {body}"
    assert "don't have" in body.lower() or r["action"] == "end", body


def test_commitment_phrasings_all_route_to_action():
    for phrasing in ["Fine, do it", "go for it", "please draft it", "theek hai karo", "sounds good"]:
        st = _conv()
        r = respond(st, phrasing)
        assert r["action"] == "send", phrasing
        low = r["body"].lower()
        assert any(w in low for w in ("starting", "done", "shuru")), f"{phrasing} -> {r['body']}"
        assert "?" not in r["body"], f"re-qualified after commitment: {r['body']}"


def test_busy_merchant_gets_backed_off_not_pushed():
    st = _conv()
    r = respond(st, "abhi busy hoon, baad mein")
    assert r["action"] == "wait" and r["wait_seconds"] >= 3600


def test_conversation_winds_down_instead_of_nudging_forever():
    st = _conv()
    actions = [respond(st, "hmm ok")["action"] for _ in range(6)]
    assert "end" in actions, f"never wound down: {actions}"


def test_language_is_mirrored_per_turn():
    """An English-only merchant writing Hinglish gets Hinglish back."""
    m = dict(_MERCH["m_001_drmeera_dentist_delhi"])
    m["identity"] = dict(m["identity"], languages=["en"])
    t = _TRIGS["trg_001_research_digest_dentists"]
    st = new_state("c", m["merchant_id"], None, t, m, _CATS["dentists"], "opener", {})
    r = respond(st, "iska source kya hai?")
    assert any(w in r["body"] for w in ("kar dun", "Aage badhun", "karke")), r["body"]
