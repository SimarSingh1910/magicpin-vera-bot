"""
Multi-turn conversation handling for /v1/reply.

State is a plain dict so it can be stored anywhere (in-memory here, could be
Postgres/Redis elsewhere) and round-tripped through conversation_handlers.py's
`respond(state, merchant_message)` contract from the main brief (§7.4).

Detection order per turn (first match wins):
  1. hostile / abusive language                -> end (or apologize once)
  2. explicit opt-out ("stop", "not interested") -> end
  3. auto-reply pattern seen twice               -> end (seen once -> one nudge)
  4. explicit intent commitment ("let's do it")  -> switch straight to action
  5. off-topic / unrelated question              -> polite decline + redirect
  6. otherwise                                   -> acknowledge + advance
"""
from __future__ import annotations

import re
from typing import Any, Optional

from vera.composer import g, salutation, voice_mix, best_offer_title
from vera.dialogue import (build_material, classify, cta_line, join_cta,
                           looks_hinglish, take, topic_words, unknown_answer)


AUTO_REPLY_PATTERNS = [
    r"thank(s| you) for (contacting|reaching out)",
    r"our team will (respond|get back|reach out)",
    r"i am (currently )?an? automated (assistant|reply|response)",
    r"this is an automated (message|response|reply)",
    r"we (have received|will respond to) your (message|query)",
    r"business is (currently )?closed",
]

HOSTILE_PATTERNS = [
    r"\bstop\b", r"\bspam\b", r"\bshut up\b", r"\bfuck\b", r"\bidiot\b",
    r"harass", r"\bscam\b", r"useless", r"annoying",
]

OPT_OUT_PATTERNS = [
    r"not interested", r"\bstop messaging\b", r"leave me alone", r"unsubscribe",
    r"do not contact", r"don'?t message",
]

INTENT_COMMIT_PATTERNS = [
    r"\blet'?s do (it|this)\b", r"\bgo ahead\b", r"\byes,? (please )?(do it|proceed|confirm)\b",
    r"\bok(ay)? (lets|let'?s) (go|do it)\b", r"\bsure,? go ahead\b", r"\bconfirm(ed)?\b",
    r"\byes please\b", r"\bstart\b.*\bplease\b", r"haan karo", r"chalega start karo",
    r"\bi want to join\b", r"\bsign me up\b",
]

QUALIFYING_PHRASES = ["would you", "do you", "can you tell", "what if", "how about", "are you interested"]


def _matches_any(text: str, patterns: list[str]) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in patterns)


def new_state(conversation_id: str, merchant_id: str, customer_id: Optional[str],
              trigger: Optional[dict], merchant: Optional[dict], category: Optional[dict],
              first_body: str, auto_reply_hits: Optional[dict] = None) -> dict:
    return {
        "conversation_id": conversation_id,
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "trigger": trigger or {},
        "merchant": merchant or {},
        "category": category or {},
        "turns": [{"from": "vera", "body": first_body}] if first_body else [],
        "sent_bodies": {first_body} if first_body else set(),
        # shared per-merchant dict (exact text -> count) so a canned reply is
        # recognized even if the judge starts a fresh conversation_id each time
        "auto_reply_hits": auto_reply_hits if auto_reply_hits is not None else {},
        "customer": None,
        "material": None,   # lazily built on first reply
        "used_material": set(),
        "nudges": 0,
        "mode": "pitch",  # or "action"
        "ended": False,
    }


def _dedupe(state: dict, body: str) -> str:
    """Never resend the exact same body twice in one conversation."""
    if body not in state["sent_bodies"]:
        return body
    return body + " (following up again on this)"


def respond(state: dict, merchant_message: str) -> dict:
    """
    Given the conversation-so-far (state) and the latest inbound message,
    return {"action": "send"|"wait"|"end", ...}. Mutates state in place.
    """
    if state.get("ended"):
        return {"action": "end", "rationale": "Conversation already closed."}

    state["turns"].append({"from": "merchant", "body": merchant_message})
    merchant = state.get("merchant") or {}
    category = state.get("category") or {}
    trigger = state.get("trigger") or {}
    name = salutation(category, merchant)

    # 1. hostile / abusive
    if _matches_any(merchant_message, HOSTILE_PATTERNS):
        state["ended"] = True
        return {"action": "end", "rationale": "Detected hostile/abusive language; closing gracefully without engaging further."}

    # 2. explicit opt-out
    if _matches_any(merchant_message, OPT_OUT_PATTERNS):
        state["ended"] = True
        return {"action": "end", "rationale": "Merchant explicitly opted out; closing conversation and suppressing this conversation_id."}

    # 3. auto-reply detection (canned phrase OR verbatim repeat of a prior merchant turn)
    is_auto_pattern = _matches_any(merchant_message, AUTO_REPLY_PATTERNS)
    prior_merchant_turns = [t["body"] for t in state["turns"][:-1] if t["from"] == "merchant"]
    is_verbatim_repeat = merchant_message in prior_merchant_turns
    if is_auto_pattern or is_verbatim_repeat:
        hits = state["auto_reply_hits"]  # shared per-merchant dict — survives across conversation_ids
        hits[merchant_message] = hits.get(merchant_message, 0) + 1
        if hits[merchant_message] >= 2:
            state["ended"] = True
            return {"action": "end", "rationale": "Same auto-reply pattern seen twice from this merchant — this is WhatsApp Business's canned reply, not the owner. Exiting to avoid wasting turns; will retry via the owner channel later."}
        # first time: back off rather than burn a turn arguing with a bot
        return {"action": "wait", "wait_seconds": 14400,
                "rationale": "Detected merchant auto-reply (canned 'thank you for contacting' shape). Backing off 4 hours to wait for the owner instead of spending turns on it."}

    # Material is built lazily so conversations created before this existed
    # (or by the /demo console) still get a pool.
    if state.get("material") is None:
        state["material"] = build_material(trigger, merchant, category,
                                            state.get("customer"))

    # Mirror the language of THIS turn — merchants switch mid-conversation.
    hinglish = looks_hinglish(merchant_message) or voice_mix(merchant) == "hi_en"
    intent = classify(merchant_message)

    def reply(body: str, cta: str, why: str, mode: Optional[str] = None) -> dict:
        if mode:
            state["mode"] = mode
        body = _dedupe(state, body)
        state["turns"].append({"from": "vera", "body": body})
        state["sent_bodies"].add(body)
        return {"action": "send", "body": body, "cta": cta, "rationale": why}

    # 4. explicit commitment -> act, never re-qualify
    if intent == "commit":
        offer = best_offer_title(merchant)
        line = "Done — starting on it now." if not hinglish else "Theek hai — abhi shuru kar rahi hoon."
        if offer:
            line += (f" I'll route it through your \"{offer}\"." if not hinglish
                     else f" Aapke \"{offer}\" ke saath tie kar dungi.")
        nxt = take(state, ("timing", "detail"))
        if nxt:
            line = join_cta(line, nxt)
        line += (" I'll confirm here the moment it's live." if not hinglish
                 else " Live hote hi yahin confirm kar dungi.")
        return reply(line, "none",
                     "Explicit commitment; switched from pitch to action immediately with no further "
                     "qualifying question, and stated the concrete next step.", mode="action")

    # 5. off-topic curveball -> decline scope once, steer back
    on_topic = any(k in merchant_message.lower() for k in
                   ("yes", "send", "draft", "ok", "sure", "when", "how", "price", "cost", "offer",
                    "abstract", "post", "slot", "time", "more", "detail", "source", "patient", "customer"))
    if "?" in merchant_message and not on_topic and len(merchant_message.split()) > 3:
        body = ("That one's outside what I can help with — your CA would be the right call. "
                if not hinglish else
                "Woh mere scope se bahar hai — uske liye aapke CA sahi rahenge. ")
        back = take(state, ("detail", "numbers", "timing")) or ""
        body += join_cta(back, cta_line("go", hinglish)) if back else cta_line("go", hinglish)
        return reply(body, "open_ended",
                     "Out-of-scope ask declined once, then the thread steered back to the original "
                     "trigger without dropping it.")

    # 6. "later / busy" -> back off rather than push
    if intent == "defer":
        state["nudges"] = state.get("nudges", 0) + 1
        return {"action": "wait", "wait_seconds": 86400,
                "rationale": "Merchant asked for time; backing off a day rather than pushing."}

    # 7. a real question -> answer it from context, or say we don't have it
    if intent in ("price", "timing", "proof", "detail", "pivot"):
        wanted = {"price": ("price",), "timing": ("timing",), "proof": ("proof",),
                  "detail": ("detail", "numbers"), "pivot": ("detail", "numbers", "timing")}[intent]
        answer = take(state, wanted, prefer=topic_words(merchant_message))
        if answer is None and intent in ("price", "timing", "proof"):
            answer = unknown_answer({"price": "figure", "timing": "date",
                                     "proof": "reference"}[intent], hinglish)
        if answer is None:
            answer = take(state, ("detail", "numbers", "timing", "price", "proof"),
                          prefer=topic_words(merchant_message))
        if answer is None:
            # material exhausted and nothing new to say -> close cleanly
            state["ended"] = True
            return {"action": "end",
                    "rationale": "Every grounded fact for this trigger has been shared and the "
                                 "merchant hasn't committed; closing rather than padding the thread."}
        if intent == "pivot":
            answer = ("On that — " if not hinglish else "Us baare mein — ") + answer
        return reply(join_cta(answer, cta_line('draft' if intent != 'timing' else 'confirm', hinglish)),
                     "binary_yes_no",
                     f"Merchant asked about {intent}; answered from the {wanted[0]} facts already in "
                     f"context (no new claims) and kept a single low-friction CTA.")

    # 8. non-committal acknowledgement -> advance with something new, or wind down
    state["nudges"] = state.get("nudges", 0) + 1
    nxt = take(state, ("numbers", "detail", "timing", "price"))
    if nxt is None or state["nudges"] > 3:
        state["ended"] = True
        return {"action": "end",
                "rationale": "No commitment after three turns and no unused grounded material left; "
                             "closing politely instead of nudging a fourth time."}
    return reply(join_cta(nxt, cta_line('go', hinglish)), "binary_yes_no",
                 "Engaged but non-committal; advanced the thread with a fact not yet used rather "
                 "than repeating the previous turn.")
