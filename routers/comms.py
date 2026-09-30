from __future__ import annotations

from fastapi import APIRouter

from services import memory_tables

router = APIRouter(tags=["comms"])

# 2026-09-24: this module used to be a full simulated messaging pipeline (preferences,
# messages, conversations, calls, webhooks) with no real provider ever behind it. An audit
# confirmed the frontend only ever calls /templates - everything else had zero call sites -
# so the rest was removed along with its now-dropped tables. `conversations`/`messages`
# themselves moved to memory too (Step 6) - routers/agent.py still uses them for AI chat
# history; this module just never touched them. notification_templates: nothing ever wrote
# a row (in-memory, always empty, matching current Neon state before this change).


@router.get("/templates")
def list_templates():
    return memory_tables.notification_templates.list()
