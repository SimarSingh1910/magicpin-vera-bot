"""
FastAPI app implementing the 5 endpoints from challenge-testing-brief.md §2.

Run: uvicorn bot:app --host 0.0.0.0 --port 8080   (bot.py at repo root re-exports `app`)
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from vera.composer import compose
from vera.conversation import new_state, respond
from vera.gating import customer_consent_ok, select_best, template_params
from vera.store import ContextStore, VALID_SCOPES

app = FastAPI(title="Vera-2 (magicpin AI Challenge bot)")

START = time.time()
store = ContextStore()

# conversation_id -> conversation state dict (see bot/conversation.py)
conversations: dict[str, dict] = {}
# merchant_id -> {exact_text: count}; shared across conversation_ids for that
# merchant so a WA Business canned auto-reply is caught even if the judge
# starts a fresh conversation_id per turn.
merchant_auto_hits: dict[str, dict[str, int]] = defaultdict(dict)
# suppression_key -> (conversation_id, unix_ts of the send). Dedup is a WINDOW,
# not a permanent ban: within a run the same trigger never fires twice, but a
# second judge pass hours later must not find every trigger silently muted and
# score zero messages. Default window covers the 60-minute test window.
used_suppression_keys: dict[str, tuple[str, float]] = {}
SUPPRESSION_TTL_SECONDS = float(os.getenv("SUPPRESSION_TTL_SECONDS", "3600"))
# merchant_id -> trigger_ids that lost this tick's contest for that merchant.
# Restraint must not mean data loss: only one message per merchant goes out per
# tick, but the runners-up carry forward and get their turn on a later tick
# rather than being silently dropped.
pending_triggers: dict[str, list[str]] = {}
MAX_PENDING_PER_MERCHANT = 20

TICK_ACTION_CAP = 20

# Identity reported at GET /v1/metadata. Override any of these with env vars at
# deploy time (TEAM_NAME, TEAM_MEMBERS as a comma-separated list, CONTACT_EMAIL)
# so the deployed bot reports the right thing without a code change.
METADATA = {
    "team_name": os.getenv("TEAM_NAME", "staleCode"),
    "team_members": [m.strip() for m in os.getenv("TEAM_MEMBERS", "Gursimar").split(",") if m.strip()],
    "model": "rule-based (no external LLM call — see README for rationale)",
    "approach": "trigger-kind dispatch table over 4-context inputs; every sentence reads a real "
                "field off category/merchant/trigger/customer, never invents one; separate "
                "conversation state machine for auto-reply / hostile / intent-transition handling",
    "contact_email": os.getenv("CONTACT_EMAIL", "gursimar.chauhan@gmail.com"),
    "version": "1.0.0",
    "submitted_at": datetime.now(timezone.utc).isoformat(),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Browser-facing extras — NOT part of the judge contract (§2 defines /v1/* only).
# The submission URL gets opened by a human, so "/" serves a small console that
# drives the real endpoints, and "/info" keeps the machine-readable card.
# Everything here is namespaced under /demo so it can never collide with /v1.
# ---------------------------------------------------------------------------
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
DATASET_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dataset")


@app.get("/", response_class=HTMLResponse)
async def console():
    index = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(index):
        return HTMLResponse("<h1>Vera-2</h1><p>Console not bundled. See <a href='/info'>/info</a>.</p>")
    with open(index, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.post("/demo/seed")
async def demo_seed():
    """
    Load the bundled seed dataset so an evaluator has something to drive.

    Pushes at version 1 through the same store the judge writes to — so if the
    judge later pushes its own version 1, that push is correctly rejected as
    stale. Hit "Reset" (POST /v1/teardown) before a real judged run.
    """
    if not os.path.isdir(DATASET_DIR):
        return JSONResponse(status_code=404, content={"ok": False, "reason": "dataset not bundled"})

    loaded = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    cat_dir = os.path.join(DATASET_DIR, "categories")
    if os.path.isdir(cat_dir):
        for fname in os.listdir(cat_dir):
            if not fname.endswith(".json"):
                continue
            with open(os.path.join(cat_dir, fname), encoding="utf-8") as f:
                payload = json.load(f)
            if store.push("category", payload.get("slug", fname[:-5]), 1, payload)[0]:
                loaded["category"] += 1

    for fname, scope, container, key in (
        ("merchants_seed.json", "merchant", "merchants", "merchant_id"),
        ("customers_seed.json", "customer", "customers", "customer_id"),
        ("triggers_seed.json", "trigger", "triggers", "id"),
    ):
        path = os.path.join(DATASET_DIR, fname)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for item in (json.load(f).get(container) or []):
                if key in item and store.push(scope, item[key], 1, item)[0]:
                    loaded[scope] += 1

    return {"ok": True, "loaded": loaded, "contexts_loaded": store.counts()}


@app.get("/demo/catalog")
async def demo_catalog():
    """Merchants and triggers currently in the store, for the console's pickers."""
    merchants, triggers = [], []
    for (scope, cid), entry in store.items():
        payload = entry["payload"]
        if scope == "merchant":
            merchants.append({
                "id": cid,
                "name": payload.get("identity", {}).get("name", cid),
                "category": payload.get("category_slug", ""),
            })
        elif scope == "trigger":
            triggers.append({
                "id": cid,
                "kind": payload.get("kind", ""),
                "merchant_id": payload.get("merchant_id", ""),
                "customer_id": payload.get("customer_id"),
                "urgency": payload.get("urgency"),
            })
    merchants.sort(key=lambda m: m["id"])
    triggers.sort(key=lambda t: t["id"])
    return {"merchants": merchants, "triggers": triggers}


class DemoComposeBody(BaseModel):
    trigger_id: str
    conversation_id: Optional[str] = None


@app.post("/demo/compose")
async def demo_compose(body: DemoComposeBody):
    """
    Compose a preview for one trigger, repeatably.

    Deliberately NOT /v1/tick: tick dedupes on suppression_key, so a second
    click on the same trigger correctly returns nothing. That's right for the
    judge and useless for a human poking at it, so this calls the composer
    directly and touches no conversation state.
    """
    trigger = store.get("trigger", body.trigger_id)
    if not trigger:
        return JSONResponse(status_code=404, content={"error": "unknown trigger_id"})
    merchant = store.get("merchant", trigger.get("merchant_id"))
    if not merchant:
        return JSONResponse(status_code=404, content={"error": "merchant for this trigger isn't loaded"})
    category = _category_for(merchant)
    customer_id = trigger.get("customer_id")
    customer = store.get("customer", customer_id) if customer_id else None

    composed = compose(category, merchant, trigger, customer)

    # register real conversation state so the console's reply flow exercises the
    # same code path /v1/reply uses after a real /v1/tick send
    if body.conversation_id:
        conversations[body.conversation_id] = new_state(
            conversation_id=body.conversation_id,
            merchant_id=trigger.get("merchant_id"),
            customer_id=customer_id,
            trigger=trigger, merchant=merchant, category=category,
            first_body=composed["body"],
            auto_reply_hits=merchant_auto_hits[trigger.get("merchant_id")],
        )

    composed["merchant_name"] = merchant.get("identity", {}).get("name", "")
    composed["merchant_id"] = trigger.get("merchant_id")
    composed["trigger_kind"] = trigger.get("kind", "")
    composed["customer_name"] = (customer or {}).get("identity", {}).get("name")
    return composed


@app.get("/info")
async def info():
    return {
        "service": "Vera-2 — magicpin AI Challenge bot",
        "team": METADATA["team_name"],
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": store.counts(),
        "endpoints": {
            "GET  /v1/healthz": "liveness + context counts",
            "GET  /v1/metadata": "bot identity",
            "POST /v1/context": "receive a context push (idempotent on version)",
            "POST /v1/tick": "periodic wake-up; returns proactive actions",
            "POST /v1/reply": "merchant/customer reply -> send | wait | end",
            "POST /v1/teardown": "wipe all state (optional)",
        },
        "interactive_docs": "/docs",
    }


# ---------------------------------------------------------------------------
# 2.4 GET /v1/healthz
# ---------------------------------------------------------------------------
@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": store.counts(),
    }


# ---------------------------------------------------------------------------
# 2.5 GET /v1/metadata
# ---------------------------------------------------------------------------
@app.get("/v1/metadata")
async def metadata():
    return METADATA


# ---------------------------------------------------------------------------
# 2.1 POST /v1/context
# ---------------------------------------------------------------------------
class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in VALID_SCOPES:
        return JSONResponse(status_code=400, content={
            "accepted": False, "reason": "invalid_scope",
            "details": f"scope must be one of {sorted(VALID_SCOPES)}",
        })

    accepted, current_version = store.push(body.scope, body.context_id, body.version, body.payload)
    if not accepted:
        return JSONResponse(status_code=409, content={
            "accepted": False, "reason": "stale_version", "current_version": current_version,
        })

    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


# ---------------------------------------------------------------------------
# 2.2 POST /v1/tick
# ---------------------------------------------------------------------------
class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


def _category_for(merchant: Optional[dict]) -> Optional[dict]:
    if not merchant:
        return None
    slug = merchant.get("category_slug")
    return store.get("category", slug) if slug else None


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions: list[dict] = []

    # --- gate first, then pick at most one trigger per merchant -------------
    # Firing two proactive messages at the same merchant in one tick reads as
    # spam, so competing triggers are ranked and only the best one survives.
    carried = [tid for ids in pending_triggers.values() for tid in ids]
    candidate_ids = list(dict.fromkeys(list(body.available_triggers) + carried))
    pending_triggers.clear()

    eligible: dict[str, list[dict]] = {}
    for trigger_id in candidate_ids:
        trigger = store.get("trigger", trigger_id)
        if not trigger:
            continue
        suppression_key = trigger.get("suppression_key") or f"{trigger.get('kind')}:{trigger.get('merchant_id')}"
        prior = used_suppression_keys.get(suppression_key)
        if prior and (time.time() - prior[1]) < SUPPRESSION_TTL_SECONDS:
            continue  # already engaged this trigger in this window
        merchant_id = trigger.get("merchant_id")
        if not merchant_id or not store.get("merchant", merchant_id):
            continue  # can't compose without the merchant we're speaking about/for
        eligible.setdefault(merchant_id, []).append({**trigger, "id": trigger.get("id", trigger_id)})

    chosen = []
    for mid, candidates in eligible.items():
        best, past_window = select_best(candidates, body.now)
        if not best:
            continue
        chosen.append((mid, best, past_window))
        runners_up = [t["id"] for t in candidates if t.get("id") != best.get("id")]
        if runners_up:
            pending_triggers[mid] = runners_up[:MAX_PENDING_PER_MERCHANT]
    chosen.sort(key=lambda row: row[0])  # deterministic action order

    for merchant_id, trigger, past_window in chosen:
        if len(actions) >= TICK_ACTION_CAP:
            break

        trigger_id = trigger.get("id")
        suppression_key = trigger.get("suppression_key") or f"{trigger.get('kind')}:{merchant_id}"
        merchant = store.get("merchant", merchant_id)
        category = _category_for(merchant)

        customer_id = trigger.get("customer_id")
        customer = store.get("customer", customer_id) if customer_id else None
        consent_note = ""
        if customer_id:
            allowed, reason = customer_consent_ok(trigger, customer)
            if not allowed:
                # Never message a customer outside their consent. Tell the
                # merchant about the opportunity instead — that needs no
                # customer consent, because it isn't sent to the customer.
                customer = None
                consent_note = f" Customer-facing send withheld ({reason}); routed to the merchant instead."

        try:
            composed = compose(category, merchant, trigger, customer)
        except Exception:
            continue  # fail closed: never send a malformed/crashed composition

        if not composed.get("body"):
            continue
        stale_note = " Trigger is past its stated expires_at but the judge listed it as active." if past_window else ""
        composed["rationale"] = composed.get("rationale", "") + consent_note + stale_note

        conv_suffix = customer_id or trigger_id
        conversation_id = f"conv_{merchant_id}_{conv_suffix}"[:120]

        conversations[conversation_id] = new_state(
            conversation_id=conversation_id,
            merchant_id=merchant_id,
            customer_id=customer_id,
            trigger=trigger,
            merchant=merchant,
            category=category,
            first_body=composed["body"],
            auto_reply_hits=merchant_auto_hits[merchant_id],
        )
        used_suppression_keys[suppression_key] = (conversation_id, time.time())

        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed["send_as"],
            "trigger_id": trigger_id,
            "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
            # {{1}} salutation, {{2}} the grounded hook, {{3}} the single CTA
            "template_params": template_params(composed["body"], merchant, customer),
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": suppression_key,
            "rationale": composed["rationale"],
        })

    return {"actions": actions}


# ---------------------------------------------------------------------------
# 2.3 POST /v1/reply
# ---------------------------------------------------------------------------
class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: Optional[str] = None
    turn_number: Optional[int] = None


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    state = conversations.get(body.conversation_id)

    if state is None:
        # Judge may address a conversation_id we never saw via /v1/tick
        # (e.g. isolated replay scenarios). Build the best state we can from
        # whatever context has been pushed, rather than erroring out.
        merchant_id = body.merchant_id or ""
        merchant = store.get("merchant", merchant_id) or {}
        category = _category_for(merchant)
        state = new_state(
            conversation_id=body.conversation_id,
            merchant_id=merchant_id,
            customer_id=body.customer_id,
            trigger={},
            merchant=merchant,
            category=category,
            first_body="",
            auto_reply_hits=merchant_auto_hits[merchant_id],
        )
        conversations[body.conversation_id] = state

    result = respond(state, body.message)
    return result


# ---------------------------------------------------------------------------
# Optional teardown (testing brief §11 — wipe state at end of test)
# ---------------------------------------------------------------------------
@app.post("/v1/teardown")
async def teardown():
    store.clear()
    conversations.clear()
    merchant_auto_hits.clear()
    used_suppression_keys.clear()
    pending_triggers.clear()
    return {"ok": True}
