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
