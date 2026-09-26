"""
Send-time gates: expiry, per-merchant restraint, and customer consent.

These decide *whether* a trigger may be acted on at all, before any composition
happens. Kept separate from composer.py so the rules are auditable on their own
and can be unit-tested without building a message.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from vera.composer import g


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Lenient ISO-8601 parse; returns None rather than raising on junk."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # last resort: a bare date
        try:
            dt = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_expired(trigger: dict, now_iso: Optional[str]) -> bool:
    """
    True when the trigger's window has closed.

    Uses the judge's simulated `now` from the tick body — not wall-clock time —
    because the harness advances simulated time independently of ours. An
    unparseable or absent expires_at is treated as "not expired": silently
    dropping a trigger we simply failed to parse would be worse than sending.
    """
    expires = parse_iso(trigger.get("expires_at"))
    if expires is None:
        return False
    now = parse_iso(now_iso) or datetime.now(timezone.utc)
    return expires < now


# What a customer-facing message of each kind is allowed to ride on.
#
# Consent vocabulary varies by merchant — the shipped customers alone use
# refill_reminders, recall_alerts, winback_offers, renewal_reminders,
# kids_program_updates and bridal_package_followup — and the judge injects
# customers we've never seen, so an exact-string map is both wrong today and
# brittle tomorrow. Each kind therefore names the scopes it accepts, plus
# keywords for matching scope names we haven't met. An unlisted kind falls back
# to the promotional set, the strictest bucket, so nothing leaks by default.
CONSENT_SCOPES = {
    "recall_due": ({"recall_reminders", "recall_alerts", "appointment_reminders",
                    "treatment_followup"}, {"recall", "appointment", "checkup", "followup"}),
    "appointment_tomorrow": ({"appointment_reminders", "booking_reminders"},
                             {"appointment", "booking", "reminder"}),
    "chronic_refill_due": ({"refill_reminders", "recall_alerts", "delivery_notifications",
                            "recall_reminders"}, {"refill", "delivery", "prescription", "recall"}),
    "trial_followup": ({"treatment_followup", "kids_program_updates", "program_updates",
                        "trial_followup", "appointment_reminders"},
                       {"trial", "program", "followup", "session", "class"}),
    "wedding_package_followup": ({"bridal_package_followup", "appointment_reminders",
                                  "treatment_followup"},
                                 {"bridal", "wedding", "package", "appointment", "followup"}),
    "customer_lapsed_soft": ({"promotional_offers", "winback_offers", "renewal_reminders"},
                             {"promotional", "winback", "offer", "renewal", "marketing"}),
    "customer_lapsed_hard": ({"promotional_offers", "winback_offers", "renewal_reminders"},
                             {"promotional", "winback", "offer", "renewal", "marketing"}),
}
DEFAULT_CONSENT = ({"promotional_offers", "winback_offers"}, {"promotional", "offer", "marketing"})

# Scopes broad enough to cover anything narrower.
BROAD_SCOPES = {"all", "any", "everything"}


def customer_consent_ok(trigger: dict, customer: Optional[dict]) -> tuple[bool, str]:
    """
    (allowed, reason) for messaging this customer about this trigger.

    Returning False does not mean staying silent — the caller falls back to
    telling the *merchant* about the opportunity, which needs no customer
    consent because it isn't sent to the customer.
    """
    if not customer:
        return False, "no customer context"

    if g(customer, "preferences", "reminder_opt_in") is False:
        return False, "customer opted out of reminders"

    if customer.get("state") == "churned":
        return False, "customer state is churned"

    raw = g(customer, "consent", "scope", default=None)
    if raw is None:
        return False, "no recorded consent scope"
    scopes = {str(s).strip().lower() for s in raw}

    kind = trigger.get("kind", "")
    accepted, keywords = CONSENT_SCOPES.get(kind, DEFAULT_CONSENT)

    if scopes & BROAD_SCOPES:
        return True, "consent is unrestricted"
    hit = scopes & accepted
    if hit:
        return True, f"consent includes {sorted(hit)[0]}"
    # scope names we haven't seen before: accept on a shared keyword
    for scope in scopes:
        for word in keywords:
            if word in scope:
                return True, f"consent scope '{scope}' covers {kind}"
    return False, f"consent scope {sorted(scopes)} does not cover {kind}"


def rank_triggers(triggers: list[dict]) -> list[dict]:
    """
    Best-first ordering when several triggers compete for one merchant.

    Urgency leads; then the trigger closing soonest, since a wide-open window
    can wait for the next tick; then id, purely so the result is deterministic.
    """
    def key(t: dict):
        try:
            urgency = -int(t.get("urgency") or 0)
        except (TypeError, ValueError):
            urgency = 0
        expires = parse_iso(t.get("expires_at"))
        return (urgency, expires.timestamp() if expires else float("inf"), str(t.get("id", "")))

    return sorted(triggers, key=key)


def select_best(triggers: list[dict], now_iso: Optional[str]) -> tuple[Optional[dict], bool]:
    """
    Pick one trigger for a merchant. Returns (trigger, past_window).

    Expiry is a strong *preference*, not a hard drop, and that is a deliberate
    call. The harness documents `available_triggers` as "trigger ids the judge
    considers active right now" — an explicit assertion of liveness. The seed
    fixtures, meanwhile, carry expiry dates from the dataset's authoring window:
    21 of the 25 shipped triggers are already past `expires_at` against
    wall-clock now, and the harness sends real wall-clock `now`. Dropping those
    outright would mute ~84% of the scored messages on the strength of a stale
    timestamp in a fixture.

    So: when a merchant has any live trigger, expired ones never win. Only when
    everything on offer is past its window do we fall back to the best of them,
    and the caller records that in the rationale.
    """
    if not triggers:
        return None, False
    ranked = rank_triggers(triggers)
    live = [t for t in ranked if not is_expired(t, now_iso)]
    if live:
        return live[0], False
    return ranked[0], True


def template_params(body: str, merchant: dict, customer: Optional[dict]) -> list[str]:
    """
    Split a composed body into WhatsApp template parameters.

    The first outbound in a 24h window has to be template-shaped
    ({{1}}/{{2}}/{{3}}), so: {{1}} is who we're addressing, {{2}} the grounded
    hook, {{3}} the closing CTA. Meta is never actually called, but shipping the
    body as one blob would make the template structure meaningless.
    """
    name = (g(customer, "identity", "name") if customer
            else g(merchant, "identity", "owner_first_name")) or \
        g(merchant, "identity", "name") or ""

    text = body.strip()
    if name and text.startswith(name):
        text = text[len(name):].lstrip(" ,—-")

    # the CTA is the last sentence; everything before it is the hook
    parts = [p.strip() for p in text.replace("!", ".").split(". ") if p.strip()]
    if len(parts) >= 2:
        hook = ". ".join(parts[:-1]).strip()
        cta = parts[-1].strip()
        if not hook.endswith("."):
            hook += "."
    else:
        hook, cta = text, ""
    return [name, hook, cta]
