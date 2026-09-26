"""
Optional multi-turn contract from challenge-brief.md §7.4:

    def respond(state: ConversationState, merchant_message: str) -> dict: ...

Thin re-export — the real implementation (auto-reply detection, hostile
handling, intent-transition, off-topic redirect, anti-repetition) lives in
vera/conversation.py and is exercised over HTTP by /v1/reply in vera/server.py.
"""
from vera.conversation import new_state, respond  # noqa: F401
