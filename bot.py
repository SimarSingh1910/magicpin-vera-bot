"""
Entrypoint per challenge-testing-brief.md §7 (`uvicorn bot:app --host 0.0.0.0 --port 8080`).

All real logic lives in vera/ (composer.py, conversation.py, store.py, server.py)
so it can be unit-tested without spinning up HTTP.

Also runnable directly — `python bot.py` — which honors the $PORT most hosts
inject (Render, Railway, Fly, Heroku), so the same command works locally and
when deployed.
"""
import os

from vera.server import app  # noqa: F401

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8080")))
