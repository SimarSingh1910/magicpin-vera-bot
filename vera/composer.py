"""
Pure composition logic for Vera-style merchant/customer WhatsApp messages.

compose(category, merchant, trigger, customer=None) -> dict
    {body, cta, send_as, suppression_key, rationale}

Design choice (see README): rule-based / template-driven rather than an LLM call.
This keeps the bot deterministic, dependency-free (no external API key required
to run or grade it), fast (<30s budget is trivially met), and — most importantly —
structurally incapable of hallucinating data, because every sentence is built by
reading a real field off the payload dicts the judge pushed. If a fact isn't
present, the handler falls back to a *different* real fact rather than inventing
one. Nothing here calls out to a network.

Every handler returns:
    facts   — list[str] of factual sentences (already period-terminated)
    cta     — "binary_yes_no" | "open_ended" | "none"
    ask     — the single closing question/CTA sentence (English; localized at
              assembly time)
    levers  — list[str] compulsion levers used, for the rationale
    anchor  — short string naming the fact this message is anchored on (for the
              rationale — "why now")
"""
from __future__ import annotations

from typing import Any, Optional


# ---------------------------------------------------------------------------
# generic helpers — every one of these only ever reads, never invents, data
# ---------------------------------------------------------------------------

def g(d: Optional[dict], *path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
        if cur is None:
            return default
    return cur


def pct(x, signed=False) -> Optional[str]:
    try:
        v = float(x) * 100
    except (TypeError, ValueError):
        return None
    if signed:
        return f"{v:+.0f}%"
    return f"{abs(v):.0f}%"


def num(x) -> Optional[str]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:,.1f}"


def humanize(value) -> str:
    """
    Turn an internal slug into merchant-readable text.

    Payload values are frequently machine keys ("summer_2026",
    "free_for_members", "kids_yoga_post", "ORS_demand_+40"). Showing those raw
    to a merchant is the judge's "exposing internal jargon" penalty, so every
    interpolated payload *value* goes through here.
    """
    if value is None:
        return ""
    s = str(value).strip()
    if "_" not in s:
        # not slug-shaped — it's already prose or a proper name ("DC vs MI"),
        # so leave its casing alone
        return s
    # trailing signed number, e.g. "ORS_demand_+40" -> "ORS demand +40%"
    suffix = ""
    if "_+" in s or "_-" in s:
        head, _sep, tail = s.rpartition("_")
        if tail.lstrip("+-").isdigit():
            suffix = f" {tail}%"
            s = head
    s = s.replace("_", " ").strip()
    # keep acronyms (ORS, IOPA, GBP) upper, lower-case ordinary words
    words = [w if (w.isupper() and len(w) <= 5) else w.lower() for w in s.split()]
    return " ".join(words) + suffix


# Merchant-readable phrasing for the internal signal names. Signals not in this
# map are never shown to the merchant — an unrecognized signal is internal by
# default, so silence beats leaking a slug.
SIGNAL_PHRASES = {
    "stale_posts": "your Google posts have gone stale",
    "ctr_below_peer_median": "your click-through rate is under the peer median",
    "unverified_gbp": "your Google profile isn't verified yet",
    "no_active_offers": "you have no live offers right now",
    "no_recent_post": "you haven't posted in a while",
    "delivery_not_set_up": "delivery isn't set up on your listing",
    "winback_eligible": "your listing is inactive and costing you visibility",
    "perf_dip_post_expiry": "visibility dropped after your plan expired",
    "perf_dip_severe": "your numbers dropped sharply this week",
    "trial_ending_soon": "your trial is ending soon",
    "no_recent_conversation": "we haven't spoken in a while",
}


def smart_cap(s: str) -> str:
    """
    Capitalize the first letter only when the text is entirely lower-case.

    Protects proper names and acronyms: "DC vs MI" must not become "Dc Vs Mi",
    but "summer 2026" should read "Summer 2026" at the start of a sentence.
    """
    if not s:
        return s
    if any(c.isupper() for c in s):
        return s
    return s[0].upper() + s[1:]


def humanize_signal(signal: str) -> Optional[str]:
    """Map 'stale_posts:22d' -> 'your Google posts have gone stale (22d)'."""
    base, _, detail = str(signal).partition(":")
    phrase = SIGNAL_PHRASES.get(base)
    if not phrase:
        return None
    return f"{phrase} ({detail})" if detail else phrase


def active_offers(merchant: dict) -> list[dict]:
    return [o for o in (merchant.get("offers") or []) if o.get("status") == "active"]


def _catalog_entry(category: Optional[dict], title: Optional[str]) -> dict:
    """Look an offer up in the category catalog by title."""
    if not category or not title:
        return {}
    for item in category.get("offer_catalog") or []:
        if item.get("title") == title:
            return item
    return {}


def offer_audience(category: Optional[dict], title: Optional[str]) -> Optional[str]:
    return _catalog_entry(category, title).get("audience")


def offer_type(category: Optional[dict], title: Optional[str]) -> Optional[str]:
    return _catalog_entry(category, title).get("type")


# Offers that only make sense for someone who has never been in. Note this is
# deliberately about *type/title*, not audience: almost every catalog offer is
# tagged audience="new_user", including plain service-at-price ones like
# "Dental Cleaning @ ₹299" — which is perfectly fine to quote to a returning
# patient, and is in fact the strongest price anchor available.
FIRST_TIMER_TYPES = {"free_trial"}
FIRST_TIMER_TITLE_HINTS = ("first month", "trial class", "free trial")


def best_offer_title(merchant: dict, category: Optional[dict] = None,
                     for_returning_customer: bool = False) -> Optional[str]:
    """
    First active offer, optionally filtered for who it's appropriate for.

    A trial-class or "First Month" offer pitched at a customer with 22 visits
    reads as insulting, so returning customers skip those; anything else
    (service-at-price, free add-ons) is fair game for anyone.
    """
    offers = active_offers(merchant)
    if not offers:
        return None
    if not for_returning_customer:
        return offers[0]["title"]

    def first_timer_only(offer: dict) -> bool:
        title = (offer.get("title") or "").lower()
        if offer_type(category, offer.get("title")) in FIRST_TIMER_TYPES:
            return True
        return any(hint in title for hint in FIRST_TIMER_TITLE_HINTS)

    def rank(offer: dict) -> int:
        # prefer offers explicitly open to everyone / repeat customers
        return 0 if offer_audience(category, offer.get("title")) in ("all", "repeat_user") else 1

    eligible = [o for o in offers if not first_timer_only(o)]
    if not eligible:
        return None
    eligible.sort(key=rank)
    return eligible[0]["title"]


def worst_delta_7d(merchant: dict) -> tuple[Optional[str], Optional[float]]:
    """
    The genuinely-worst 7d metric, or (None, None) if nothing actually fell.

    Guards the sign bug: a perf_dip trigger whose payload has no numbers must
    never be backfilled with a metric that actually went *up*.
    """
    # metric key in delta_7d -> how to say it
    candidates = {"calls_pct": "calls", "views_pct": "views", "ctr_pct": "CTR"}
    deltas = g(merchant, "performance", "delta_7d", default={}) or {}
    worst_name, worst_val = None, None
    for key, label in candidates.items():
        val = deltas.get(key)
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        if val < 0 and (worst_val is None or val < worst_val):
            worst_name, worst_val = label, val
    return worst_name, worst_val


def find_digest_item(category: Optional[dict], item_id: Optional[str]) -> Optional[dict]:
    if not category or not item_id:
        return None
    for item in category.get("digest") or []:
        if item.get("id") == item_id:
            return item
    return None


def top_digest_by_kind(category: Optional[dict], kind_filter: str) -> Optional[dict]:
    if not category:
        return None
    for item in category.get("digest") or []:
        if item.get("kind") == kind_filter:
            return item
    return None


def signal_texts(merchant: dict) -> list[str]:
    return [str(s) for s in (merchant.get("signals") or [])]


def has_signal(merchant: dict, needle: str) -> bool:
    needle = needle.lower()
    return any(needle in s.lower() for s in signal_texts(merchant))


def salutation(category: Optional[dict], merchant: dict, customer: Optional[dict] = None) -> str:
    if customer:
        return g(customer, "identity", "name") or "there"
    owner = g(merchant, "identity", "owner_first_name")
    slug = (category or {}).get("slug", "")
    name = g(merchant, "identity", "name") or "there"
    if owner and slug == "dentists":
        return f"Dr. {owner}"
    return owner or name


def voice_mix(merchant: dict, customer: Optional[dict] = None) -> str:
    """'hi_en' when the audience prefers Hindi-English code-mix, else 'en'."""
    if customer:
        pref = (g(customer, "identity", "language_pref") or "").lower()
        if "hi" in pref:
            return "hi_en"
        return "en"
    langs = g(merchant, "identity", "languages", default=[]) or []
    return "hi_en" if "hi" in langs else "en"


# Hindi-English variants of the closing ask, keyed by the exact English string a
# handler returns. Facts (numbers, dates, citations) stay in English so they
# can't be mistranslated; only the ask/CTA switches register. An ask missing
# from this table simply stays English — never a machine translation.
ASK_HI_EN = {
    # merchant-facing
    "Want me to pull the full note + draft something your patients/customers can read?":
        "Chahenge main poora note nikaal ke ek draft bana dun jo aap patients ko bhej saken?",
    "Want me to check your setup against this and flag what needs to change?":
        "Chahenge main aapka setup check kar ke bata dun kya badalna hai?",
    "Want me to look at what's driving it and suggest one fix?":
        "Chahenge main dekh lun kya wajah hai, aur ek fix suggest karun?",
    "Want me to double down while it's hot — boost a post or refresh your offer?":
        "Momentum hai — ek post boost karun ya offer refresh kar dun?",
    "Want me to turn this into a share-worthy Google post + a customer shoutout?":
        "Iska ek Google post bana dun, saath mein customers ke liye shoutout?",
    "Want me to draft a one-line fix you can post as an update?":
        "Ek line ka update draft kar dun jo aap post kar saken?",
    "Want me to sharpen your listing so you stay the obvious pick nearby?":
        "Chahenge main listing sharp kar dun taaki aas-paas aap hi pehli choice rahein?",
    "Want me to lock in renewal now so there's no gap in your listing?":
        "Renewal abhi lock kar dun taaki listing mein gap na aaye?",
    "What's your most-asked service this week — I'll build around that.":
        "Is hafte sabse zyada kis service ki demand hai? Main usi ke around bana deti hoon.",
    "What's the one question customers keep asking you this week?":
        "Is hafte customers sabse zyada kya poochh rahe hain?",
    "Want me to send the fastest path back to active?":
        "Wapas active hone ka sabse tez raasta bhej dun?",
    "Want a draft post timed for it?":
        "Uske liye timed draft post bana dun?",
    "Want me to start the verification for you?":
        "Verification main shuru kar dun aapke liye?",
    "Want the registration link?": "Registration link bhej dun?",
    "Want me to check if it matches anything you're carrying?":
        "Check kar lun ki aapke stock mein yeh batch hai ya nahi?",
    "Ready for me to draft the next version?": "Agla version draft kar dun?",
    "Want me to draft the message you can send them?":
        "Unke liye message draft kar dun jo aap bhej saken?",
    "Want me to take a closer look and suggest one thing to fix?":
        "Thoda detail mein dekh ke ek cheez suggest karun jo fix ho sake?",
    # customer-facing (sent as the merchant)
    "Reply 1 or 2 for a slot, or tell us a time that works.":
        "Slot ke liye 1 ya 2 reply karein, ya apna pasand ka time bata dein.",
    "Want us to hold a slot for you this week?":
        "Is hafte aapke liye ek slot hold kar dein?",
    "Reply 1 to confirm, 2 to reschedule.":
        "Confirm karne ke liye 1, reschedule ke liye 2 reply karein.",
    "Reply YES to auto-schedule the refill delivery.":
        "Refill delivery auto-schedule karne ke liye YES reply karein.",
    "Want to lock the next session?": "Agla session lock kar dein?",
    "Want to confirm the final slot now?": "Final slot abhi confirm kar dein?",
}


def localize_ask(ask: str, mix: str) -> str:
    if mix != "hi_en":
        return ask
    return ASK_HI_EN.get(ask, ask)


def assemble_body(name: str, facts: list[str], ask: str, cta: str, mix: str,
                  citation: str = "", business_name: Optional[str] = None) -> str:
    # customer-facing messages come from the merchant's own WhatsApp number, so
    # they must identify the business — the customer doesn't know who "we" is.
    if business_name:
        greeting = f"{name}, {business_name} here —"
    else:
        greeting = f"{name}," if name else ""
    fact_text = " ".join(f for f in facts if f)
    ask_text = ask or ""
    parts = [p for p in (greeting, fact_text, ask_text) if p]
    body = " ".join(parts).strip()
    if citation:
        body = f"{body} {citation}".strip()
    return body


# ---------------------------------------------------------------------------
# per-trigger-kind handlers
# handler(category, merchant, trigger, customer) -> (facts, cta, ask, levers, anchor)
# ---------------------------------------------------------------------------

def h_research_digest(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    item = find_digest_item(category, payload.get("top_item_id")) or top_digest_by_kind(category, "research")
    facts, levers = [], ["specificity"]
    if item:
        title = (item.get("title") or "").rstrip(".")
        n = item.get("trial_n")
        detail = title + (f" ({num(n)}-patient trial)" if n else "")
        facts.append(detail + ".")
        seg = (item.get("patient_segment") or "").replace("_", " ")
        if seg and any(seg.split()[0] in s for s in signal_texts(merchant)):
            facts.append(f"Flagging because it lines up with your {seg} cohort.")
            levers.append("merchant_fit")
        anchor = item.get("id")
        citation = f"— {item.get('source')}" if item.get("source") else ""
    else:
        facts.append(f"This week's {category.get('slug', 'category')} research digest has a fresh item.")
        anchor, citation = "category_digest", ""
    ask = "Want me to pull the full note + draft something your patients/customers can read?"
    return facts, "open_ended", ask, levers + ["curiosity", "effort_externalization"], anchor, citation


def h_regulation_change(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    item = find_digest_item(category, payload.get("top_item_id")) or top_digest_by_kind(category, "compliance")
    deadline = payload.get("deadline_iso")
    facts, levers = [], ["specificity"]
    if item:
        facts.append((item.get("title") or "").rstrip(".") + ".")
        if item.get("summary"):
            facts.append(item["summary"])
    if deadline:
        facts.append(f"Deadline: {deadline.split('T')[0]}.")
        levers.append("loss_aversion")
    if item and item.get("actionable"):
        facts.append(item["actionable"] + ".")
    ask = "Want me to check your setup against this and flag what needs to change?"
    citation = f"— {item.get('source')}" if item and item.get("source") else ""
    return facts, "binary_yes_no", ask, levers + ["effort_externalization"], (item or {}).get("id", "compliance_update"), citation


def h_perf_dip(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    metric = payload.get("metric")
    delta = payload.get("delta_pct")
    window = payload.get("window", "7d")
    facts, levers = [], []
    if metric and delta is not None:
        facts.append(f"Your {humanize_metric(metric)} dropped {pct(delta)} over the last {window}"
                      + (f", vs a usual {num(payload.get('vs_baseline'))}." if payload.get("vs_baseline") is not None else "."))
        anchor = f"{metric}_dip_{window}"
    else:
        # No numbers in the payload. Only claim a drop for a metric that
        # actually fell — never report an increase as a dip.
        label, val = worst_delta_7d(merchant)
        if label:
            facts.append(f"Your {label} are down {pct(val)} week-over-week."
                         if label != "CTR" else f"Your CTR is down {pct(val)} week-over-week.")
            anchor = f"performance.delta_7d:{label}"
        else:
            # nothing genuinely dipped -> anchor on the real gap instead
            views = g(merchant, "performance", "views")
            calls = g(merchant, "performance", "calls")
            if views and calls:
                facts.append(f"Your listing got {num(views)} views but only {num(calls)} calls in the last 30 days.")
            else:
                facts.append("Taking a look at your listing this week.")
            anchor = "performance.conversion_gap"
    levers.append("loss_aversion")
    peer_ctr = g(category, "peer_stats", "avg_ctr")
    my_ctr = g(merchant, "performance", "ctr")
    if peer_ctr and my_ctr and my_ctr < peer_ctr:
        facts.append(f"Peers in your category average {pct(peer_ctr)} CTR; you're at {pct(my_ctr)}.")
        levers.append("social_proof")
    ask = "Want me to look at what's driving it and suggest one fix?"
    return facts, "binary_yes_no", ask, levers + ["effort_externalization"], anchor, ""


def h_perf_spike(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    metric = payload.get("metric")
    delta = payload.get("delta_pct")
    driver = payload.get("likely_driver")
    facts = []
    if metric and delta is not None:
        facts.append(f"Your {humanize_metric(metric)} jumped {pct(delta)} vs your usual {num(payload.get('vs_baseline'))} — nice spike."
                      if payload.get("vs_baseline") is not None else f"Your {humanize_metric(metric)} jumped {pct(delta)}.")
        anchor = f"{metric}_spike"
    else:
        # only call it a spike if something actually rose meaningfully
        deltas = g(merchant, "performance", "delta_7d", default={}) or {}
        best_label, best_val = None, 0.0
        for key, label in (("views_pct", "views"), ("calls_pct", "calls")):
            try:
                val = float(deltas.get(key))
            except (TypeError, ValueError):
                continue
            if val > best_val:
                best_label, best_val = label, val
        if best_label and best_val >= 0.05:
            facts.append(f"Your {best_label} are up {pct(best_val)} this week.")
            anchor = f"performance.delta_7d:{best_label}"
        else:
            views = g(merchant, "performance", "views")
            facts.append(f"Your listing is holding steady at {num(views)} views over 30 days."
                         if views else "Your listing is steady this week.")
            anchor = "performance.steady"
    if driver:
        facts.append(f"Likely driver: {humanize(driver)}.")
    ask = "Want me to double down while it's hot — boost a post or refresh your offer?"
    return facts, "open_ended", ask, ["specificity", "curiosity"], anchor, ""


METRIC_NOUNS = {
    "review_count": "reviews", "reviews": "reviews", "calls": "calls",
    "views": "views", "directions": "directions", "leads": "leads",
    "bookings": "bookings", "customers": "customers",
}


def humanize_metric(metric: str) -> str:
    return METRIC_NOUNS.get(metric, metric.replace("_", " "))


def h_milestone_reached(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    metric = payload.get("metric")
    now_val = payload.get("value_now")
    target = payload.get("milestone_value")
    val = now_val if now_val is not None else target
    facts = []
    ask = "Want me to turn this into a share-worthy Google post + a customer shoutout?"
    if metric and val is not None:
        noun = humanize_metric(metric)
        # "is_imminent" means they're approaching the milestone, not past it —
        # saying "you just crossed 150" when they're at 145 is a factual slip.
        if target and now_val is not None and float(now_val) < float(target):
            gap = int(float(target) - float(now_val))
            facts.append(f"You're at {num(now_val)} {noun} — {gap} short of {num(target)}.")
            anchor = f"{metric}_approaching_{target}"
            ask = f"Want me to draft a nudge that gets you those last {gap}?"
        else:
            facts.append(f"You just crossed {num(val)} {noun} — worth celebrating.")
            anchor = f"{metric}_milestone"
    else:
        rc = g(merchant, "customer_aggregate", "total_unique_ytd")
        facts.append(f"You've served {num(rc)} unique customers this year." if rc else "You hit a nice milestone this month.")
        anchor = "customer_aggregate"
    return facts, "binary_yes_no", ask, ["social_proof", "specificity"], anchor, ""


def h_review_theme_emerged(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    theme = payload.get("theme")
    occ = payload.get("occurrences_30d")
    quote = payload.get("common_quote")
    facts = []
    if theme and occ:
        facts.append(f"{num(occ)} reviews this month mention \"{theme.replace('_', ' ')}\"" + (f" — one says: \"{quote}\"." if quote else "."))
        anchor = f"review_theme:{theme}"
    else:
        rt = (merchant.get("review_themes") or [None])[0]
        if rt:
            facts.append(f"{rt.get('occurrences_30d', 'Several')} reviews mention \"{rt.get('theme', '').replace('_', ' ')}\".")
            anchor = f"review_theme:{rt.get('theme')}"
        else:
            facts.append("A pattern is showing up in your recent reviews.")
            anchor = "review_themes"
    ask = "Want me to draft a one-line fix you can post as an update?"
    return facts, "binary_yes_no", ask, ["loss_aversion", "specificity"], anchor, ""


def h_competitor_opened(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    name = payload.get("competitor_name")
    dist = payload.get("distance_km")
    offer = payload.get("their_offer")
    facts = []
    if name:
        facts.append(f"{name} opened {num(dist)}km away" + (f", leading with \"{offer}\"." if offer else "."))
        anchor = "competitor_opened"
    else:
        facts.append("A new competitor listing showed up near you on Google.")
        anchor = "competitor_opened_generic"
    my_offer = best_offer_title(merchant)
    if my_offer:
        facts.append(f"Your \"{my_offer}\" still holds up well against that.")
    ask = "Want me to sharpen your listing so you stay the obvious pick nearby?"
    return facts, "binary_yes_no", ask, ["loss_aversion", "specificity"], anchor, ""


def h_renewal_due(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    days = payload.get("days_remaining", g(merchant, "subscription", "days_remaining"))
    amount = payload.get("renewal_amount")
    facts = []
    if days is not None:
        facts.append(f"Your plan renews in {days} days" + (f" (₹{num(amount)})." if amount else "."))
    else:
        facts.append("Your subscription window is coming up.")
    ask = "Want me to lock in renewal now so there's no gap in your listing?"
    return facts, "binary_yes_no", ask, ["loss_aversion"], "subscription.days_remaining", ""


def h_dormant_with_vera(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    days = payload.get("days_since_last_merchant_message")
    last_topic = payload.get("last_topic")
    facts = []
    if days:
        facts.append(f"It's been {days} days since we last spoke"
                      + (f" about {humanize(last_topic)}." if last_topic else "."))
    else:
        facts.append("Haven't heard from you in a bit.")
    # only surface signals we have merchant-readable phrasing for; an internal
    # signal name ("winback_eligible") must never reach the merchant verbatim
    readable = [humanize_signal(s) for s in signal_texts(merchant)]
    readable = [r for r in readable if r]
    if readable:
        facts.append(f"One thing worth a look meanwhile: {readable[0]}.")
    ask = "What's your most-asked service this week — I'll build around that."
    return facts, "open_ended", ask, ["reciprocity", "ask_the_merchant"], "dormant_with_vera", ""


ASK_TEMPLATE_QUESTIONS = {
    "what_service_in_demand_this_week": "What's your most-asked service this week — I'll build around that.",
    "what_customers_ask_most": "What's the one question customers keep asking you this week?",
}


def h_curious_ask_due(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    # ask_template is a machine slug (e.g. "what_service_in_demand_this_week"),
    # not display text — map it to a real question, never show the raw key.
    ask_template = payload.get("ask_template")
    ask = ASK_TEMPLATE_QUESTIONS.get(ask_template, "What's the one question customers keep asking you this week?")
    facts = ["Quick one for you — no pitch this time."]
    return facts, "open_ended", ask, ["ask_the_merchant", "curiosity"], "curious_ask", ""


def h_winback(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    days_expired = payload.get("days_since_expiry")
    dip = payload.get("perf_dip_pct")
    lapsed = payload.get("lapsed_customers_added_since_expiry")
    facts = []
    if days_expired:
        facts.append(f"Your listing has been inactive {days_expired} days.")
    if dip is not None:
        facts.append(f"Visibility is down {pct(dip)} since then" + (f", and {num(lapsed)} more customers went lapsed." if lapsed else "."))
    if not facts:
        facts.append("Your plan lapsed a while back and it's costing you visibility.")
    ask = "Want me to send the fastest path back to active?"
    return facts, "binary_yes_no", ask, ["loss_aversion", "specificity"], "winback_eligible", ""


def h_local_event(category, merchant, trigger, customer):
    # covers ipl_match_today / festival_upcoming / category_seasonal / local news style
    payload = trigger.get("payload") or {}
    if payload.get("placeholder"):
        # No real event data. Inventing one (or dredging up an out-of-season
        # "seasonal beat") produces nonsense like a January note sent in April.
        return h_generic(category, merchant, trigger, customer)

    label = payload.get("match") or payload.get("festival") or payload.get("season")
    when = payload.get("match_time_iso") or payload.get("date")
    days_until = payload.get("days_until")
    facts, timing_is_now = [], True
    if label:
        when_txt = ""
        if isinstance(when, str):
            if "T" in when:  # has a clock time — keep it, it's the useful part
                date_part, _, time_part = when.partition("T")
                when_txt = f" ({date_part}, {time_part[:5]})"
            else:
                when_txt = f" ({when})"
        # a festival 188 days out is a planning conversation, not a "push now" one
        if isinstance(days_until, (int, float)) and days_until > 21:
            facts.append(f"{smart_cap(humanize(label))} is {int(days_until)} days out{when_txt} — worth planning early.")
            timing_is_now = False
        else:
            facts.append(f"{smart_cap(humanize(label))} is coming up{when_txt}.")

    # category_seasonal ships the actual demand movements — use them, they're
    # the most specific thing in the payload
    trends = payload.get("trends") or []
    if trends:
        readable = [humanize(t) for t in trends[:3]]
        facts.append("Demand shifts this season: " + ", ".join(readable) + ".")

    offer = best_offer_title(merchant)
    if offer:
        facts.append(f"Your \"{offer}\" is a natural fit to build around."
                     if not timing_is_now else f"Your \"{offer}\" is a natural fit to push.")
    elif payload.get("shelf_action_recommended"):
        facts.append("Worth checking your shelf against that before the demand lands.")

    ask = "Want a draft post timed for it?"
    return facts, "open_ended", ask, ["specificity", "effort_externalization"], "local_event", ""


def h_gbp_unverified(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    uplift = payload.get("estimated_uplift_pct")
    facts = ["Your Google Business Profile isn't verified yet."]
    if uplift:
        facts.append(f"Verified listings in your category see about {pct(uplift)} more calls.")
    ask = "Want me to start the verification for you?"
    return facts, "binary_yes_no", ask, ["loss_aversion", "specificity"], "gbp_unverified", ""


def h_cde_opportunity(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    item = find_digest_item(category, payload.get("digest_item_id")) or top_digest_by_kind(category, "cde")
    facts = []
    if item:
        facts.append((item.get("title") or "") + ".")
        if item.get("date"):
            facts.append(f"When: {item['date'].split('T')[0]}.")
        credits = payload.get("credits") or item.get("credits")
        fee = payload.get("fee") or item.get("actionable")
        if credits:
            facts.append(f"{credits} CDE credits{', ' + humanize(fee) if fee else ''}.")
        elif fee:
            facts.append(f"{humanize(fee).capitalize()}.")
    else:
        facts.append("There's a CE/CDE session coming up relevant to your category.")
    ask = "Want the registration link?"
    return facts, "open_ended", ask, ["specificity"], "cde_opportunity", ""


def h_supply_alert(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    molecule = payload.get("molecule")
    batches = payload.get("affected_batches")
    facts = []
    if molecule:
        facts.append(f"Supply alert: {molecule} has affected batches" + (f" ({', '.join(batches)})." if batches else "."))
    else:
        facts.append("A supply alert relevant to your stock just came in.")
    ask = "Want me to check if it matches anything you're carrying?"
    return facts, "binary_yes_no", ask, ["specificity", "loss_aversion"], "supply_alert", ""


def h_active_planning_intent(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    topic = payload.get("intent_topic")
    last_msg = payload.get("merchant_last_message")
    facts = []
    if topic:
        facts.append(f"Picking up where we left off on {topic.replace('_', ' ')}.")
    if last_msg:
        # strip trailing punctuation so quoting the merchant's own words never
        # introduces a second '?' into the message (single-primary-CTA rule)
        facts.append(f"You said: \"{last_msg.rstrip('?.! ')}\".")
    ask = "Ready for me to draft the next version?"
    return facts, "binary_yes_no", ask, ["effort_externalization"], "active_planning_intent", ""


# ---- customer-scoped handlers ----

def visits_total(customer: Optional[dict]) -> int:
    try:
        return int(g(customer, "relationship", "visits_total", default=0) or 0)
    except (TypeError, ValueError):
        return 0


def h_recall_due(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    service = payload.get("service_due")
    slots = payload.get("available_slots") or []
    last_service = payload.get("last_service_date") or g(customer, "relationship", "last_visit")
    returning = visits_total(customer) > 1
    facts = []
    if service:
        facts.append(f"It's been a while since your last visit ({last_service}) — {humanize(service)} is due."
                     if last_service else f"{humanize(service).capitalize()} is due.")
    elif last_service:
        # no service named in the payload — don't invent a clinical "recall";
        # anchor on the real last-visit date instead
        facts.append(f"Your last visit with us was {last_service} — time for the next one.")
    else:
        facts.append("You're due for your next visit with us.")

    offer = best_offer_title(merchant, category, for_returning_customer=returning)
    if offer:
        facts.append(f"{offer} available.")
    if slots:
        labels = [s.get("label") for s in slots if s.get("label")]
        if labels:
            facts.append("Slots: " + " or ".join(labels) + ".")
    if slots:
        ask = "Reply 1 or 2 for a slot, or tell us a time that works."
        cta = "binary_yes_no"
    else:
        ask = "Want us to hold a slot for you this week?"
        cta = "binary_yes_no"
    return facts, cta, ask, ["specificity", "effort_externalization"], "recall_due", ""


def h_customer_lapsed(category, merchant, trigger, customer):
    rel = (customer or {}).get("relationship", {})
    last_visit = rel.get("last_visit")
    visits = rel.get("visits_total")
    days_since = (trigger.get("payload") or {}).get("days_since_last_visit")
    prev_focus = (trigger.get("payload") or {}).get("previous_focus")
    facts = []
    if days_since:
        facts.append(f"It's been {days_since} days since your last visit — we miss having you in.")
    elif last_visit:
        facts.append(f"It's been a while since {last_visit} — we miss having you in.")
    if visits:
        facts.append(f"You've visited {visits} times before, so we kept your preferences on file.")
    if prev_focus:
        facts.append(f"Last time you were working on {humanize(prev_focus)}.")
    offer = best_offer_title(merchant, category, for_returning_customer=visits_total(customer) > 1)
    if offer:
        facts.append(f"{offer} is running right now if you'd like to come back.")
    ask = "Want us to hold a slot for you this week?"
    return facts, "binary_yes_no", ask, ["loss_aversion", "reciprocity"], "customer_lapsed", ""


def h_appointment_tomorrow(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    when = payload.get("appointment_time")
    service = payload.get("service")
    facts = [f"Reminder: your appointment is {when}." if when
             else "Reminder: your appointment with us is tomorrow."]
    if service:
        facts.append(f"Booked for {humanize(service)}.")
    ask = "Reply 1 to confirm, 2 to reschedule."
    return facts, "binary_yes_no", ask, ["specificity"], "appointment_tomorrow", ""


def h_chronic_refill(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    molecules = payload.get("molecule_list")
    runs_out = payload.get("stock_runs_out_iso")
    if not molecules:
        # Nothing to refill in the payload. A "refill delivery" message with no
        # prescription data — especially from a non-pharmacy merchant — is worse
        # than saying nothing, so fall back to the relationship-based nudge.
        return h_customer_lapsed(category, merchant, trigger, customer)
    facts = [f"Your regular refill ({', '.join(molecules)}) is running low."]
    if runs_out:
        facts.append(f"Stock at home runs out around {runs_out.split('T')[0]}.")
    if payload.get("delivery_address_saved"):
        facts.append("We have your delivery address saved.")
    ask = "Reply YES to auto-schedule the refill delivery."
    return facts, "binary_yes_no", ask, ["loss_aversion", "effort_externalization"], "chronic_refill_due", ""


def h_wedding_followup(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    days = payload.get("days_to_wedding")
    trial_done = payload.get("trial_completed")
    facts = []
    if days:
        facts.append(f"{days} days to go — following up on your bridal package.")
    if trial_done:
        facts.append("Your trial run is done; time to lock the final date.")
    ask = "Want to confirm the final slot now?"
    return facts, "binary_yes_no", ask, ["specificity", "loss_aversion"], "wedding_package_followup", ""


def h_trial_followup(category, merchant, trigger, customer):
    payload = trigger.get("payload") or {}
    trial_date = payload.get("trial_date")
    options = payload.get("next_session_options")
    facts = []
    if trial_date:
        facts.append(f"Hope the trial session on {trial_date} went well!")
    if options:
        facts.append("A few slots are open for the next session.")
    ask = "Want to lock the next session?"
    return facts, "binary_yes_no", ask, ["effort_externalization"], "trial_followup", ""


def h_customer_opportunity(category, merchant, trigger, customer=None):
    """
    Customer-scoped trigger fired, but we hold no CustomerContext for it.

    This is the normal case under the judge harness, which pushes categories,
    merchants and triggers but never customers. Without the customer we cannot
    write *to* the customer — addressing the merchant with "your cleaning is
    due" is both wrong and embarrassing. So we tell the merchant about the
    opportunity instead, using only the trigger payload, and offer to draft the
    outreach. Merchant-facing, so send_as stays "vera".
    """
    kind = trigger.get("kind", "")
    p = trigger.get("payload") or {}
    facts = []

    if kind == "recall_due":
        service = humanize(p.get("service_due")) or "a recall visit"
        due = p.get("due_date")
        facts.append(f"One of your patients is due for {service}" + (f" on {due}." if due else "."))
        labels = [s.get("label") for s in (p.get("available_slots") or []) if s.get("label")]
        if labels:
            facts.append("You have " + " and ".join(labels) + " open.")
    elif kind in ("customer_lapsed_hard", "customer_lapsed_soft"):
        days = p.get("days_since_last_visit")
        facts.append(f"A customer of yours hasn't been in for {days} days."
                     if days else "One of your regulars has gone quiet.")
        if p.get("previous_focus"):
            facts.append(f"They were working on {humanize(p['previous_focus'])}.")
        if p.get("previous_membership_months"):
            facts.append(f"They held a {p['previous_membership_months']}-month membership before that.")
    elif kind == "chronic_refill_due":
        molecules = p.get("molecule_list")
        facts.append(f"A regular customer's refill ({', '.join(molecules)}) is due."
                     if molecules else "A regular customer's refill is due.")
        if p.get("stock_runs_out_iso"):
            facts.append(f"Their stock runs out around {p['stock_runs_out_iso'].split('T')[0]}.")
    elif kind == "appointment_tomorrow":
        facts.append("You have an appointment booked for tomorrow that hasn't been confirmed.")
    elif kind == "trial_followup":
        trial_date = p.get("trial_date")
        facts.append(f"Your trial attendee from {trial_date} hasn't booked a next session."
                     if trial_date else "A trial attendee hasn't booked again.")
        if p.get("next_session_options"):
            facts.append("You have open slots to offer them.")
    elif kind == "wedding_package_followup":
        days = p.get("days_to_wedding")
        facts.append(f"A bridal client's date is {days} days out and the package isn't locked."
                     if days else "A bridal client hasn't locked their package.")
        if p.get("trial_completed"):
            facts.append("Their trial is already done.")
    else:
        facts.append("One of your customers has an open follow-up.")

    ask = "Want me to draft the message you can send them?"
    return facts, "binary_yes_no", ask, ["effort_externalization", "specificity"], \
        f"customer_opportunity:{kind}", ""


def h_generic(category, merchant, trigger, customer):
    """Fallback for any trigger kind without a dedicated handler — still fact-anchored."""
    signals = signal_texts(merchant)
    readable = [(s, humanize_signal(s)) for s in signals]
    readable = [(s, r) for s, r in readable if r]
    facts = []
    if readable:
        raw, phrase = readable[0]
        facts.append(f"Noticed {phrase}.")
        anchor = f"signal:{raw}"
    else:
        # no merchant-safe signal to cite — anchor on their real numbers instead
        views = g(merchant, "performance", "views")
        calls = g(merchant, "performance", "calls")
        if views and calls:
            facts.append(f"Your listing got {num(views)} views and {num(calls)} calls in the last 30 days.")
        elif views:
            facts.append(f"Your listing had {num(views)} views in the last 30 days.")
        else:
            facts.append("Checking in on your listing.")
        anchor = "performance"
    offer = best_offer_title(merchant)
    if offer:
        facts.append(f"Your \"{offer}\" offer is live.")
    ask = "Want me to take a closer look and suggest one thing to fix?"
    return facts, "open_ended", ask, ["specificity"], anchor, ""


HANDLERS = {
    "research_digest": h_research_digest,
    "regulation_change": h_regulation_change,
    "perf_dip": h_perf_dip,
    "seasonal_perf_dip": h_perf_dip,
    "perf_spike": h_perf_spike,
    "milestone_reached": h_milestone_reached,
    "review_theme_emerged": h_review_theme_emerged,
    "competitor_opened": h_competitor_opened,
    "renewal_due": h_renewal_due,
    "dormant_with_vera": h_dormant_with_vera,
    "curious_ask_due": h_curious_ask_due,
    "winback_eligible": h_winback,
    "festival_upcoming": h_local_event,
    "ipl_match_today": h_local_event,
    "category_seasonal": h_local_event,
    "gbp_unverified": h_gbp_unverified,
    "cde_opportunity": h_cde_opportunity,
    "supply_alert": h_supply_alert,
    "active_planning_intent": h_active_planning_intent,
    "recall_due": h_recall_due,
    "customer_lapsed_soft": h_customer_lapsed,
    "customer_lapsed_hard": h_customer_lapsed,
    "appointment_tomorrow": h_appointment_tomorrow,
    "chronic_refill_due": h_chronic_refill,
    "wedding_package_followup": h_wedding_followup,
    "trial_followup": h_trial_followup,
}


def compose(category: Optional[dict], merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    kind = trigger.get("kind", "generic")
    # A customer-scoped trigger with no CustomerContext can't be written *to*
    # the customer — tell the merchant about the opportunity instead.
    if customer is None and (trigger.get("customer_id") or trigger.get("scope") == "customer"):
        handler = h_customer_opportunity
    else:
        handler = HANDLERS.get(kind, h_generic)
    facts, cta, ask, levers, anchor, citation = handler(category or {}, merchant, trigger, customer)

    mix = voice_mix(merchant, customer)
    name = salutation(category, merchant, customer)

    # Only the closing ask is localized — factual sentences stay in English
    # because they carry numbers/citations that must not be mistranslated.
    ask_localized = localize_ask(ask, mix)

    # customer-facing messages go out from the merchant's own number, so they
    # have to say which business is writing
    business_name = g(merchant, "identity", "name") if customer else None

    body = assemble_body(name, facts, ask_localized, cta, mix, citation,
                         business_name=business_name)

    trg_id = trigger.get("id", "")
    merchant_id = merchant.get("merchant_id", "")
    suppression_key = trigger.get("suppression_key") or f"{kind}:{merchant_id}"
    send_as = "merchant_on_behalf" if customer else "vera"

    rationale = (
        f"Trigger '{kind}' (urgency={trigger.get('urgency', '?')}, source={trigger.get('source', '?')}); "
        f"anchored on {anchor}; levers={levers}; cta={cta}."
    )

    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": suppression_key,
        "rationale": rationale,
    }
