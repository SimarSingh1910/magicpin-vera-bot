"""
Reply content: what to actually SAY on turn 2+.

The first outbound is composed by composer.py. Everything after that used to be
a single canned acknowledgement, which repeated itself and ignored the
question. This module fixes that with two pieces:

  * a **material pool** — every remaining real fact this conversation can still
    spend, built once from the same four contexts the opener used, so follow-up
    turns stay as grounded as turn one and never repeat a fact; and
  * an **intent classifier** — what did the merchant actually ask? — with an
    answer drawn from that pool.

The rule from the brief holds throughout: answer from context only, and say so
plainly when the context doesn't contain the answer. Nothing here invents a
number, a price, or a date.
"""
from __future__ import annotations

import re
from typing import Optional

from vera.composer import (
    active_offers, find_digest_item, g, humanize, humanize_signal, num, pct,
    signal_texts, top_digest_by_kind,
)

# --- what the merchant just said -------------------------------------------

COMMIT_PATTERNS = [
    r"\blet'?s do (it|this)\b", r"\bgo ahead\b", r"\bgo for it\b",
    r"\byes,? (please )?(do it|proceed|confirm|send|draft|go)\b",
    r"\b(ok(ay)?|fine|sure|alright|right),? (lets|let'?s|go|do it|do|send|draft|proceed)\b",
    r"\b(do|send|draft|share|make) it\b", r"\bplease (do|send|draft|proceed|go)\b",
    r"\byes please\b", r"\bconfirm(ed)?\b", r"\bproceed\b", r"\bsounds good\b",
    r"\bi want to join\b", r"\bsign me up\b", r"\bset it up\b",
    r"haan\b", r"\bkaro\b", r"theek hai", r"thik hai", r"chalega", r"bhej d",
]
PRICE_PATTERNS = [r"\bcost(s|ing)?\b", r"\bprice[sd]?\b", r"\bcharge[sd]?\b", r"\bfee[s]?\b",
                  r"how much", r"\bkitna\b", r"\bkitne ka\b", r"\bpaisa\b", r"\brate[s]?\b", r"₹"]
TIMING_PATTERNS = [r"\bwhen\b", r"how long", r"\bdeadline\b", r"\bdate\b", r"\btiming\b",
                   r"\bkab\b", r"kitne din", r"how soon", r"\bslot[s]?\b"]
PROOF_PATTERNS = [r"\bsource\b", r"\bstudy\b", r"\bresearch\b", r"\bproof\b", r"\bevidence\b",
                  r"\breference\b", r"\blink\b", r"kahan se", r"\bjournal\b", r"who says"]
DETAIL_PATTERNS = [r"tell me more", r"more detail", r"\bexplain\b", r"\bdetails?\b", r"\bhow does\b",
                   r"\bhow do\b", r"\bwhat is\b", r"\bkaise\b", r"aur batao", r"\bmore\b",
                   r"\binteresting\b", r"\bgo on\b"]
DEFER_PATTERNS = [r"\blater\b", r"\bbusy\b", r"\bnot now\b", r"baad mein", r"\btomorrow\b",
                  r"call me", r"\bnext week\b", r"abhi nahi"]
PIVOT_PATTERNS = [r"what about\b", r"\bhow about\b", r"aur\b.*\bka\b", r"\balso\b.*\?"]

HINDI_TOKENS = {
    "kya", "hai", "hain", "nahi", "nahin", "karo", "kar", "mujhe", "aap", "kitna", "kitne",
    "kaise", "theek", "thik", "acha", "achha", "haan", "bhej", "chahiye", "batao", "kab",
    "kyun", "mera", "meri", "dijiye", "dena", "abhi", "baad", "paisa", "chalega", "bhi",
}


def looks_hinglish(text: str) -> bool:
    """True if the merchant is writing Hindi/Hinglish this turn."""
    if re.search(r"[ऀ-ॿ]", text):  # Devanagari
        return True
    words = re.findall(r"[a-z]+", text.lower())
    return any(w in HINDI_TOKENS for w in words)


def _hit(text: str, patterns: list[str]) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in patterns)


def classify(message: str) -> str:
    """One label for the merchant's turn. Order matters — commitment wins."""
    if _hit(message, COMMIT_PATTERNS):
        return "commit"
    if _hit(message, PRICE_PATTERNS):
        return "price"
    if _hit(message, TIMING_PATTERNS):
        return "timing"
    if _hit(message, PROOF_PATTERNS):
        return "proof"
    if _hit(message, DEFER_PATTERNS):
        return "defer"
    if _hit(message, PIVOT_PATTERNS):
        return "pivot"
    if _hit(message, DETAIL_PATTERNS) or message.strip().endswith("?"):
        return "detail"
    return "ack"


# --- the pool of real facts this conversation can still spend ---------------

def build_material(trigger: dict, merchant: dict, category: Optional[dict],
                   customer: Optional[dict] = None) -> list[dict]:
    """
    Ordered follow-up facts, each tagged so an answer can pick the right kind.

    Every entry reads a real field. A context that doesn't carry a given fact
    simply contributes nothing — the pool gets shorter, never invented.
    """
    payload = trigger.get("payload") or {}
    out: list[dict] = []

    def add(kind: str, text: Optional[str]):
        if text:
            out.append({"kind": kind, "text": text})

    item = (find_digest_item(category, payload.get("top_item_id"))
            or find_digest_item(category, payload.get("digest_item_id")))
    if item:
        add("detail", item.get("summary"))
        add("detail", item.get("actionable"))
        if item.get("source"):
            cite = f"Source: {item['source']}"
            if item.get("trial_n"):
                cite += f" ({num(item['trial_n'])} patients"
                if item.get("patient_segment"):
                    cite += f", {humanize(item['patient_segment'])}"
                cite += ")"
            add("proof", cite + ".")

    # timing facts straight off the payload
    for key, label in (("deadline_iso", "Deadline"), ("due_date", "Due"),
                       ("date", "Date"), ("match_time_iso", "Starts"),
                       ("stock_runs_out_iso", "Stock runs out")):
        val = payload.get(key)
        if isinstance(val, str):
            shown = val.split("T")[0] if "T" in val else val
            add("timing", f"{label}: {shown}.")
    labels = [s.get("label") for s in (payload.get("available_slots") or []) if s.get("label")]
    if labels:
        add("timing", "Open slots: " + " or ".join(labels) + ".")
    if payload.get("days_until") is not None:
        add("timing", f"That's {payload['days_until']} days out.")

    # price facts — only ever real ones
    if payload.get("renewal_amount"):
        add("price", f"Renewal is ₹{num(payload['renewal_amount'])}.")
    if payload.get("fee"):
        add("price", f"Fee: {humanize(payload['fee'])}.")
    for offer in active_offers(merchant):
        add("price", f"Your live offer: {offer.get('title')}.")

    # the merchant's own numbers, and how they sit against peers
    perf = merchant.get("performance") or {}
    if perf.get("views") and perf.get("calls"):
        add("numbers", f"Last 30 days you had {num(perf['views'])} views and {num(perf['calls'])} calls.")
    peer_ctr, my_ctr = g(category, "peer_stats", "avg_ctr"), perf.get("ctr")
    if peer_ctr and my_ctr:
        gap = "under" if my_ctr < peer_ctr else "above"
        add("numbers", f"Your CTR is {pct(my_ctr)}, {gap} the peer average of {pct(peer_ctr)}.")
    agg = merchant.get("customer_aggregate") or {}
    if agg.get("lapsed_180d_plus"):
        add("numbers", f"You have {num(agg['lapsed_180d_plus'])} customers lapsed past 180 days.")

    for sig in signal_texts(merchant):
        add("detail", (lambda p: f"Also worth knowing: {p}." if p else None)(humanize_signal(sig)))

    # drop duplicates, keep order
    seen, deduped = set(), []
    for entry in out:
        if entry["text"] not in seen:
            seen.add(entry["text"])
            deduped.append(entry)
    return deduped


STOPWORDS = {
    "what", "about", "how", "the", "and", "for", "that", "this", "with", "your", "you",
    "can", "are", "does", "will", "from", "have", "has", "our", "any", "all", "also",
    "tell", "more", "much", "there", "them", "they", "would", "should", "could", "patients",
}


def topic_words(message: str) -> list[str]:
    """Content words worth matching material against."""
    return [w for w in re.findall(r"[a-z]{4,}", message.lower()) if w not in STOPWORDS]


def take(state: dict, kinds: tuple[str, ...], prefer: Optional[list[str]] = None) -> Optional[str]:
    """
    Next unused fact of any of these kinds; None when that well is dry.

    `prefer` biases selection toward material that actually mentions what the
    merchant asked about, so "what about aligners?" gets the aligner fact rather
    than whatever happened to be next in the queue.
    """
    used = state.setdefault("used_material", set())
    pool = [e for e in (state.get("material") or [])
            if e["kind"] in kinds and e["text"] not in used]
    if not pool:
        return None
    if prefer:
        for entry in pool:
            low = entry["text"].lower()
            if any(word in low for word in prefer):
                used.add(entry["text"])
                return entry["text"]
    used.add(pool[0]["text"])
    return pool[0]["text"]


def join_cta(fact: str, cta: str) -> str:
    """Punctuate the fact before appending the CTA."""
    fact = fact.strip()
    if fact and fact[-1] not in ".!?":
        fact += "."
    return f"{fact} {cta}".strip()


# --- closing lines ----------------------------------------------------------

CTA_LINES = {
    "en": {
        "go": "Want me to go ahead?",
        "draft": "Want me to draft it for you?",
        "more": "Want the rest of it?",
        "confirm": "Shall I set it up?",
    },
    "hi_en": {
        "go": "Aage badhun?",
        "draft": "Main draft kar dun?",
        "more": "Baaki bhi bhej dun?",
        "confirm": "Set up kar dun?",
    },
}


def cta_line(key: str, hinglish: bool) -> str:
    return CTA_LINES["hi_en" if hinglish else "en"][key]


def unknown_answer(topic: str, hinglish: bool) -> str:
    """Say plainly that the contexts don't carry this, then keep moving."""
    if hinglish:
        return f"{topic} ka exact figure mere paas abhi nahi hai — main confirm karke bata deti hoon."
    return f"I don't have the exact {topic} in front of me — I'll confirm and come back on that."
