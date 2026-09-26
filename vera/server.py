"""
FastAPI app implementing the 5 endpoints from challenge-testing-brief.md §2.

Run: uvicorn bot:app --host 0.0.0.0 --port 8080   (bot.py at repo root re-exports `app`)
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from vera.composer import compose
from vera.conversation import new_state, respond
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
# suppression_key -> conversation_id already sent, so the same trigger is
# never re-composed into a second outbound conversation.
used_suppression_keys: dict[str, str] = {}

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

    for trigger_id in body.available_triggers:
        if len(actions) >= TICK_ACTION_CAP:
            break

        trigger = store.get("trigger", trigger_id)
        if not trigger:
            continue

        suppression_key = trigger.get("suppression_key") or f"{trigger.get('kind')}:{trigger.get('merchant_id')}"
        if suppression_key in used_suppression_keys:
            continue  # already engaged this trigger — restraint over spam

        merchant_id = trigger.get("merchant_id")
        merchant = store.get("merchant", merchant_id) if merchant_id else None
        if not merchant:
            continue  # can't compose without the merchant we're speaking about/for

        category = _category_for(merchant)
        customer_id = trigger.get("customer_id")
        customer = store.get("customer", customer_id) if customer_id else None

        try:
            composed = compose(category, merchant, trigger, customer)
        except Exception:
            continue  # fail closed: never send a malformed/crashed composition

        if not composed.get("body"):
            continue

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
        used_suppression_keys[suppression_key] = conversation_id

        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed["send_as"],
            "trigger_id": trigger_id,
            "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
            "template_params": [
                merchant.get("identity", {}).get("name", ""),
                composed["body"],
            ],
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
    return {"ok": True}
