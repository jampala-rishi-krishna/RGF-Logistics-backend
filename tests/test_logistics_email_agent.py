"""Inbound Logistics email agent. Gmail, OpenAI and the live caches are mocked; nothing is sent."""
import base64
import email
import json
from email import policy
from types import SimpleNamespace

import httpx
import pytest

from services import gmail_sender, live_sales_order_cache, logistics_email_agent as agent, staff_directory_cache

LOGISTICS = "martin.logistics@rareglobalfood.com"
DRIVER = {"id": 7, "name": "Juan Dela Cruz", "email": "juan@example.com", "warehouse": "METS", "active": True, "title": "DELIVERY DRIVER"}
TEAM = [{"name": "Jed Cruz", "email": "ops1@x.com"}, {"name": "Pau Reyes", "email": "ops2@x.com"}]


def _b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


def msg(mid, sender, body, *, thread="T1", subject="Driver assignment NAN1234", internal=1000, extra=None, message_id=None, references=None, label_ids=None):
    headers = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}, {"name": "Message-ID", "value": message_id or f"<{mid}@mail.example>"}]
    if references:
        headers.append({"name": "References", "value": references})
    headers += [{"name": k, "value": v} for k, v in (extra or {}).items()]
    message = {"id": mid, "threadId": thread, "internalDate": str(internal), "payload": {"mimeType": "text/plain", "headers": headers, "body": {"data": _b64(body)}}}
    if label_ids is not None:
        message["labelIds"] = label_ids
    return message


def order(so, customer, vehicle="NAN1234", driver_id=7, status="assigned", helpers=None, address="1 Cold St"):
    return SimpleNamespace(id=so, salesorder_number=so, customer_name=customer, vehicle_id=vehicle, driver_id=driver_id, helper_ids=helpers or [], assignment_status=status, raw_json={"shipping_address": {"address": address, "city": "Manila"}})


@pytest.fixture(autouse=True)
def env(monkeypatch):
    agent.reset_state()
    monkeypatch.setenv("GMAIL_COMMS_CLIENT_ID", "id")
    monkeypatch.setenv("GMAIL_COMMS_CLIENT_SECRET", "s")
    monkeypatch.setenv("GMAIL_COMMS_REFRESH_TOKEN", "r")
    monkeypatch.delenv("GMAIL_FROM_ADDRESS", raising=False)
    monkeypatch.setattr(gmail_sender, "mailbox_address", lambda: "martin@rareglobalfood.com")
    monkeypatch.setattr(staff_directory_cache, "all_staff", lambda: [DRIVER, {"id": 9, "name": "Ana", "email": "ana@example.com", "active": True}])
    monkeypatch.setattr(staff_directory_cache, "notify_list", lambda: TEAM)
    monkeypatch.setattr(live_sales_order_cache, "get_assigned_snapshot", lambda: [order("SO-1001", "Acme Foods")])


@pytest.fixture
def rig(monkeypatch):
    """Capture model prompts and outgoing sends; any non-Gmail HTTP call is recorded as n8n."""
    r = SimpleNamespace(prompts=[], sends=[], archived=[], http=[], model_json=None, send_error=None, archive_result=None)
    default = lambda intent: json.dumps({"replyMessage": "Thank you for confirming, Juan.", "action": "ACK_CONFIRM", "reasoning": "x"})

    def model(system, user):
        r.prompts.append((system, user))
        if isinstance(r.model_json, Exception):
            raise r.model_json
        return r.model_json if r.model_json is not None else default(None)

    def send(**kw):
        if r.send_error and kw["purpose"] == "logistics-agent-reply":
            raise gmail_sender.GmailSendError(r.send_error, 502)
        r.sends.append(kw)
        return {"id": f"sent{len(r.sends)}", "threadId": kw.get("thread_id") or "new", "to": kw["to"], "labels": {"logistics": "applied", "logisticsSent": "applied"}}

    def archive(message_id, label_ids=None):
        r.archived.append((message_id, label_ids))
        if isinstance(r.archive_result, Exception):
            return {"inbox": "failed", "error": str(r.archive_result)}
        return r.archive_result or {"inbox": "removed"}

    monkeypatch.setattr(agent, "_call_model", model)
    monkeypatch.setattr(gmail_sender, "send_email", send)
    monkeypatch.setattr(gmail_sender, "remove_inbox_label", archive)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: r.http.append(url))
    monkeypatch.setattr(httpx, "get", lambda url, **kw: r.http.append(url))
    r.replies = lambda: [s for s in r.sends if s["purpose"] == "logistics-agent-reply"]
    r.team = lambda: [s for s in r.sends if s["purpose"] == "logistics-agent-team"]
    return r


def run(*messages, dry_run=False):
    return agent.process_thread({"id": messages[0]["threadId"], "messages": list(messages)}, dry_run=dry_run)


def driver_msg(body, **kw):
    return msg(kw.pop("mid", "m1"), 'Juan <juan@example.com>', body, **kw)


# ---- intents / replies (1-6) ----

def test_confirm_replies_ack_confirm(rig):
    result = run(driver_msg("Hi Martin, sige, confirmed. I can take the assigned route today."))
    assert result["status"] == "replied" and result["intent"] == "CONFIRM" and result["action"] == "ACK_CONFIRM"
    assert len(rig.replies()) == 1
    assert rig.replies()[0]["extra_headers"] == {agent.ACTION_HEADER: "ACK_CONFIRM"}
    # a NEW confirmation notifies the team: one email per Notify List row
    assert sorted(s["to"] for s in rig.team()) == ["ops1@x.com", "ops2@x.com"] and result["teamNotified"] is True


def test_issue_acks_and_notifies_team(rig):
    rig.model_json = json.dumps({"replyMessage": "Sorry to hear that. The team is being notified now.", "action": "CONTINUE", "reasoning": ""})
    result = run(driver_msg("Boss na-flat tire ang truck, delayed ako"))
    assert result["intent"] == "ISSUE" and result["action"] == "ACK_ISSUE"  # action forced by deterministic intent
    assert sorted(s["to"] for s in rig.team()) == ["ops1@x.com", "ops2@x.com"]  # one email per row
    team = rig.team()[0]
    assert team["subject"] == "[Driver Reply] Juan Dela Cruz - NAN1234"
    assert "Hi Jed," in team["html"] and "Status: ACK_ISSUE" in team["html"] and "flat tire" in team["html"]
    assert "Hi Pau," in rig.team()[1]["html"]


def test_question_is_answered_from_context(rig):
    rig.model_json = json.dumps({"replyMessage": "Your pickup is at Mets Cold Storage.", "action": "ANSWER_QUESTION", "reasoning": ""})
    result = run(driver_msg("Saan ang pickup ko today?"))
    assert result["intent"] == "QUESTION" and result["action"] == "ANSWER_QUESTION" and rig.team() == []
    assert "Warehouse Pickup: Mets Cold Storage" in rig.prompts[0][1]


def test_escalation_notifies_team_once(rig):
    rig.model_json = json.dumps({"replyMessage": "Please call the Control Tower now. The team has been alerted.", "action": "ESCALATE", "reasoning": ""})
    result = run(driver_msg("Emergency, na-aksidente kami, please call me"))
    assert result["intent"] == "ESCALATION" and result["action"] == "ESCALATE" and len(rig.team()) == 2
    assert "Status: ESCALATE" in rig.team()[0]["html"]
    # escalation is notified once per THREAD: state is rebuilt from the header our earlier reply carried
    earlier = msg("m0", f"Martin Cuico <{LOGISTICS}>", "Please call the Control Tower.", internal=500, extra={agent.ACTION_HEADER: "ESCALATE"})
    agent._last_reply.clear()  # skip the 2-minute cooldown for this check
    again = run(earlier, driver_msg("Still stuck, need help", mid="m9", internal=9000))
    assert again["action"] == "ESCALATE" and again["teamNotified"] is False and len(rig.team()) == 2


def test_general_message_continues(rig):
    rig.model_json = json.dumps({"replyMessage": "Good morning, Juan.", "action": "CONTINUE", "reasoning": ""})
    result = run(driver_msg("Good morning Martin"))
    assert result["intent"] == "GENERAL" and result["action"] == "CONTINUE"


def test_tagalog_and_taglish_detection_reach_the_prompt(rig):
    assert agent.detect_language("Sige po, papunta na ako sa warehouse") == "Tagalog"
    assert agent.detect_language("Hi Martin, sige, confirmed. I can take the assigned route today.") == "Taglish"
    assert agent.detect_language("Confirmed, I will take the route") == "English"
    run(driver_msg("Opo, sige po, tanggap ko na"))
    assert "Driver language: Tagalog" in rig.prompts[0][1]


def test_intent_rules():
    d = agent.detect_intent
    assert d("no problem, confirmed") == "CONFIRM" and d("wala pong problema, sige") == "CONFIRM"
    assert d("stuck in traffic") == "ISSUE" and d("accident!") == "ESCALATION"
    # confirm + question in one message
    assert d("confirmed but where do I pick up?") == "CONFIRM_QUESTION"
    assert d("sige po, ano ang schedule?") == "CONFIRM_QUESTION"


@pytest.mark.parametrize("text", ["I am stranded", "may nasugatan", "nabangga kami", "need police and insurance", "saklolo po", "need help", "tulong po", "call me asap"])
def test_escalation_keywords(text):
    assert agent.detect_intent(text) == "ESCALATION"


def test_agreed_keyword_additions_and_exclusions():
    d = agent.detect_intent
    assert d("ready na po") == "CONFIRM"
    assert d("what is the route") == "QUESTION" and d("route please") == "QUESTION"
    assert d("address?") == "QUESTION" and d("send the schedule") == "QUESTION"
    assert d("confirmed, I can take the assigned route today") == "CONFIRM"  # weak words never turn a confirmation into a question
    # bare help / hindi / ayaw are deliberately NOT triggers
    assert d("help") == "GENERAL" and d("hindi po") == "GENERAL" and d("ayaw ko po nito") == "GENERAL"


def test_confirm_plus_question_answers_confirms_and_notifies(rig):
    rig.model_json = json.dumps({"replyMessage": "Thanks for confirming. Your pickup is at Mets Cold Storage.", "action": "ANSWER_QUESTION", "reasoning": ""})
    result = run(driver_msg("Confirmed po, saan ang pickup?"))
    assert result["intent"] == "CONFIRM_QUESTION" and result["action"] == "ACK_CONFIRM" and result["teamNotified"] is True
    assert "answer the question" in rig.prompts[0][1] and len(rig.team()) == 2
    assert rig.replies()[0]["extra_headers"] == {agent.ACTION_HEADER: "ACK_CONFIRM"}


def test_team_not_notified_for_plain_question_or_general(rig):
    rig.model_json = json.dumps({"replyMessage": "Your pickup is at Mets Cold Storage.", "action": "ANSWER_QUESTION", "reasoning": ""})
    run(driver_msg("saan ang pickup?"))
    rig.model_json = json.dumps({"replyMessage": "Good morning.", "action": "CONTINUE", "reasoning": ""})
    run(driver_msg("good morning", thread="T2", mid="m2"))
    assert rig.team() == []


def test_repeat_confirmation_does_not_renotify(rig):
    earlier = msg("m0", f"Martin Cuico <{LOGISTICS}>", "Thanks for confirming.", internal=500, extra={agent.ACTION_HEADER: "ACK_CONFIRM"})
    result = run(earlier, driver_msg("ok po ulit", internal=3000))
    assert result["status"] == "replied" and result["teamNotified"] is False and rig.team() == []
    assert result["state"] == {"stage": "CONFIRMED", "confirmed": True, "escalated": False}


def test_state_is_rebuilt_from_thread_headers_not_memory():
    def own(mid, action, t):
        return msg(mid, f"Martin Cuico <{LOGISTICS}>", "x", internal=t, extra={agent.ACTION_HEADER: action})

    assert agent.rebuild_state([]) == {"stage": "ASSIGNED", "confirmed": False, "escalated": False}
    state = agent.rebuild_state([own("a", "ACK_CONFIRM", 1), own("b", "ESCALATE", 2)])
    assert state == {"stage": "ESCALATED", "confirmed": True, "escalated": True}
    # a header on a message that is NOT ours must be ignored (drivers cannot spoof state)
    spoof = msg("c", "Juan <juan@example.com>", "hi", internal=3, extra={agent.ACTION_HEADER: "ESCALATE"})
    assert agent.rebuild_state([spoof])["escalated"] is False
    agent.reset_state()  # a "restart" loses bookkeeping, but not conversation state
    assert agent.rebuild_state([own("a", "ACK_CONFIRM", 1)])["confirmed"] is True


def test_signature_and_subject_fallback(rig):
    no_subject = msg("m1", "Juan <juan@example.com>", "Confirmed", subject="")
    run(no_subject)
    sent = rig.replies()[0]
    assert sent["subject"] == "Re: Dispatch Assignment"
    assert sent["text"].endswith("Regards,\nMartin Cuico\nIntelliFleet Logistics Team, Rare Global Food Trading Corp")
    assert "<b>Martin Cuico</b>" in sent["html"] and "IntelliFleet Logistics Team, Rare Global Food Trading Corp" in sent["html"]


# ---- unknown sender / sales / ignores (8-12) ----

def test_unknown_sender_is_not_answered(rig):
    result = run(msg("m1", "Stranger <who@else.com>", "Where is my delivery?"))
    assert result["status"] == "skipped" and "unknown sender" in result["reason"]
    assert rig.prompts == [] and rig.sends == []
    assert agent.status()["needsReview"][0]["kind"] == "unknown_sender"


def test_sales_question_is_flagged_for_decline(rig):
    rig.model_json = json.dumps({"replyMessage": "That is handled by a different team, I can't help with that here.", "action": "CONTINUE", "reasoning": ""})
    run(driver_msg("How much is the price of the salmon?"))
    system, user = rig.prompts[0]
    assert "pricing, products" not in user and "Sales or pricing topic mentioned: yes, decline it" in user
    assert "NEVER discuss sales pricing, product catalogs, or lead generation" in system and "martin@rareglobalfood.com" in system


@pytest.mark.parametrize("sender", [f"Martin Cuico <{LOGISTICS}>", "Martin Reyes <martin@rareglobalfood.com>", "Martin <MARTIN@rareglobalfood.com>"])
def test_own_identities_are_ignored(rig, sender):
    result = run(msg("m1", sender, "Confirmed"))
    assert result["status"] == "skipped" and "ours" in result["reason"] and rig.sends == [] and rig.prompts == []


@pytest.mark.parametrize("sender,extra,subject", [
    ("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", None, "Delivery Status Notification (Failure)"),
    ("noreply@system.com", None, "Hello"),
    ("juan@example.com", {"Auto-Submitted": "auto-replied"}, "Automatic reply: away"),
    ("juan@example.com", {"Precedence": "bulk"}, "Newsletter"),
])
def test_bounces_and_system_mail_are_ignored(rig, sender, extra, subject):
    result = run(msg("m1", sender, "x", extra=extra, subject=subject))
    assert result["status"] == "skipped" and "system" in result["reason"] and rig.sends == []


# ---- context / history (13-15) ----

def test_context_and_multiple_sos(rig, monkeypatch):
    monkeypatch.setattr(live_sales_order_cache, "get_assigned_snapshot", lambda: [
        order("SO-1001", "Acme Foods"), order("SO-1002", "Beta Mart", address="9 Pier Rd"),
        order("SO-2000", "Other Co", driver_id=99), order("SO-3000", "Helper Co", driver_id=99, helpers=[7]),
        order("SO-4000", "Done Co", status="completed"),
    ])
    run(driver_msg("ok po"))
    user = rig.prompts[0][1]
    for needle in ("Driver: Juan Dela Cruz", "Truck Plate: NAN1234", "Warehouse Pickup: Mets Cold Storage", "SO-1001 (Acme Foods", "SO-1002 (Beta Mart, 9 Pier Rd", "SO-3000 (Helper Co"):
        assert needle in user
    assert "SO-2000" not in user and "SO-4000" not in user
    assert "Assignment already confirmed: no" in user and "Already escalated: no" in user and "Today's date (Asia/Manila)" in user


def test_conversation_history_and_quoted_text_cleaning(rig):
    earlier = msg("m0", f"Martin Cuico <{LOGISTICS}>", "Please confirm your assignment.", internal=500)
    latest = driver_msg("Confirmed po.\n\nOn Mon, Oct 5, 2026 at 9:00 AM Martin <x@y.com> wrote:\n> Please confirm your assignment.", internal=2000)
    run(earlier, latest)
    user = rig.prompts[0][1]
    assert "Martin: Please confirm your assignment." in user
    assert "DRIVER EMAIL:\nConfirmed po." in user and "wrote:" not in user.split("DRIVER EMAIL:")[1]


# ---- threading + labels (16-17) ----

def test_reply_is_threaded_from_the_logistics_identity(rig):
    run(driver_msg("Confirmed", message_id="<abc@mail.example>", references="<first@mail.example>"))
    sent = rig.replies()[0]
    assert sent["to"] == "juan@example.com" and sent["subject"] == "Re: Driver assignment NAN1234"
    assert sent["thread_id"] == "T1" and sent["in_reply_to"] == "<abc@mail.example>"
    assert sent["references"] == "<first@mail.example> <abc@mail.example>"
    assert sent.get("label_logistics", True) is True and "from_address" not in sent  # identity comes from GMAIL_FROM_* config
    assert rig.archived == [("m1", None)]


def test_processed_inbound_message_has_inbox_removed_after_successful_reply(rig):
    result = run(driver_msg("Confirmed", label_ids=["INBOX", "Logistics"]))
    assert result["status"] == "replied" and result["inbox"] == {"inbox": "removed"}
    assert rig.archived == [("m1", ["INBOX", "Logistics"])]
    assert len(rig.replies()) == 1


def test_inbox_cleanup_failure_does_not_retry_or_block_handled_state(rig):
    rig.archive_result = RuntimeError("gmail modify down")
    result = run(driver_msg("Confirmed", label_ids=["INBOX", "Logistics"]))
    assert result["status"] == "replied" and result["inbox"]["inbox"] == "failed"
    assert len(rig.replies()) == 1
    again = run(driver_msg("Confirmed", label_ids=["INBOX", "Logistics"]))
    assert again["status"] == "skipped" and "already handled" in again["reason"]
    assert len(rig.replies()) == 1 and rig.archived == [("m1", ["INBOX", "Logistics"])]


def test_inbox_cleanup_skips_when_inbox_already_absent(rig):
    rig.archive_result = {"inbox": "skipped", "reason": "INBOX already absent"}
    result = run(driver_msg("Confirmed", label_ids=["Logistics"]))
    assert result["status"] == "replied"
    assert result["inbox"] == {"inbox": "skipped", "reason": "INBOX already absent"}
    assert rig.archived == [("m1", ["Logistics"])]


def test_thread_headers_and_threadid_reach_gmail_and_labels_are_applied(monkeypatch):
    """Real gmail_sender with Gmail mocked at HTTP: threadId in the send body, headers in the MIME, labels applied."""
    monkeypatch.setenv("GMAIL_FROM_ADDRESS", LOGISTICS)
    monkeypatch.setenv("GMAIL_FROM_NAME", "Martin Cuico")
    monkeypatch.setenv("GMAIL_REPLY_TO", LOGISTICS)
    gmail_sender._token.update(value="t", expires_at=9e12)
    gmail_sender._labels.update(ids={"Logistics": "L1", "Logistics/Sent": "L2"}, fetched_at=9e12)
    gmail_sender._sendas.update(addresses={"martin@rareglobalfood.com", LOGISTICS}, fetched_at=9e12)
    gmail_sender._sent_keys.clear()
    posts = []

    def post(url, **kw):
        posts.append((url, kw["json"]))
        return SimpleNamespace(status_code=200, json=lambda: {"id": "R1", "threadId": "T1"}, text="")

    monkeypatch.setattr(httpx, "post", post)
    out = gmail_sender.send_email(to="juan@example.com", subject="Re: Hi", html="<p>x</p>", thread_id="T1", in_reply_to="<abc@mail.example>", references="<a@x> <abc@mail.example>")
    send_url, body = posts[0]
    assert send_url == gmail_sender.SEND_URL and body["threadId"] == "T1"
    mime = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]), policy=policy.default)
    assert mime["In-Reply-To"] == "<abc@mail.example>" and mime["References"] == "<a@x> <abc@mail.example>"
    assert mime["From"] == f"Martin Cuico <{LOGISTICS}>" and mime["Reply-To"] == LOGISTICS
    # reply into an existing thread: Logistics on the THREAD, Logistics/Sent on the sent message only
    assert posts[1] == (gmail_sender.THREAD_MODIFY_URL.format(id="T1"), {"addLabelIds": ["L1"]})
    assert posts[2] == (gmail_sender.MODIFY_URL.format(id="R1"), {"addLabelIds": ["L2"]})
    assert out["labels"] == {"logistics": "applied", "logisticsSent": "applied"}


# ---- failures (18-19) ----

@pytest.mark.parametrize("bad", ["not json at all", json.dumps({"replyMessage": "", "action": "ACK_CONFIRM"}), json.dumps({"replyMessage": "Your truck is ZZZ9999.", "action": "CONTINUE"}), json.dumps({"replyMessage": "Delivering SO-9999 now.", "action": "CONTINUE"}), RuntimeError("openai down")])
def test_ai_failure_sends_no_reply_and_alerts_team_once(rig, bad):
    rig.model_json = bad
    result = run(driver_msg("Confirmed"))
    assert result["status"] == "failed" and rig.replies() == []
    assert len(rig.team()) == 2 and rig.team()[0]["subject"] == "[Driver Reply] Juan Dela Cruz - NAN1234"
    assert "Status: NEEDS_REVIEW" in rig.team()[0]["html"]
    run(driver_msg("Confirmed"))
    assert len(rig.team()) == 2  # not re-notified on retry
    assert agent.status()["needsReview"][0]["kind"] == "ai_failure"


def test_ai_failure_gives_up_after_max_attempts(rig):
    rig.model_json = "garbage"
    for _ in range(5):
        run(driver_msg("Confirmed"))
    assert len(rig.prompts) == agent.MAX_ATTEMPTS_PER_MESSAGE


def test_em_dash_is_replaced_and_context_ids_are_allowed(rig):
    rig.model_json = json.dumps({"replyMessage": "Thanks — your truck NAN1234 and SO-1001 are noted.", "action": "ACK_CONFIRM"})
    result = run(driver_msg("Confirmed"))
    assert result["reply"] == "Thanks, your truck NAN1234 and SO-1001 are noted."


def test_gmail_send_failure_is_reported_and_retried_without_marking_handled(rig):
    rig.send_error = "Gmail down"
    result = run(driver_msg("Confirmed"))
    assert result["status"] == "failed" and "gmail send" in result["reason"] and rig.team() == []
    rig.send_error = None
    assert run(driver_msg("Confirmed"))["status"] == "replied"


# ---- loop safety / dry run ----

def test_no_reply_loop_after_we_answer(rig):
    first = driver_msg("Confirmed")
    assert run(first)["status"] == "replied"
    ours = msg("m2", f"Martin Cuico <{LOGISTICS}>", "Thank you", internal=2000)
    again = run(first, ours)
    assert again["status"] == "skipped" and len(rig.replies()) == 1
    assert run(first)["status"] == "skipped"  # same inbound id is never answered twice


def test_cooldown_and_hourly_cap(rig, monkeypatch):
    assert run(driver_msg("Confirmed", mid="a"))["status"] == "replied"
    assert "rate limit" in run(driver_msg("ok po", mid="b", internal=3000))["reason"]
    agent.reset_state()
    monkeypatch.setattr(agent, "MAX_REPLIES_PER_HOUR", 0)
    assert "rate limit" in run(driver_msg("Confirmed"))["reason"]


def test_dry_run_drafts_but_sends_nothing(rig):
    result = run(driver_msg("Confirmed"), dry_run=True)
    assert result["status"] == "dry_run" and result["reply"] and rig.sends == []


# ---- polling + no n8n (20) ----

def test_poll_once_end_to_end_uses_only_gmail_and_no_n8n(rig, monkeypatch):
    inbound = driver_msg("Hi Martin, sige, confirmed. I can take the assigned route today.", thread="TX", mid="mx")
    calls = []

    def gget(path, params=None):
        calls.append(path)
        if path == "messages":
            assert "to:martin.logistics@rareglobalfood.com" in params["q"] and "label:logistics" in params["q"] and "deliveredto:" in params["q"]
            return {"messages": [{"id": "mx", "threadId": "TX"}, {"id": "my", "threadId": "TX"}]}
        return {"id": "TX", "messages": [inbound]}

    monkeypatch.setattr(agent, "_gmail_get", gget)
    results = agent.poll_once()
    assert [r["status"] for r in results] == ["replied"] and calls == ["messages", "threads/TX"]
    assert rig.http == []  # no httpx call to n8n (or anything else) from the agent itself
    assert not any("n8n" in str(v) for s in rig.sends for v in s.values())


def test_poll_requires_gmail_and_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("LOGISTICS_AGENT_ENABLED", raising=False)
    assert agent.enabled() is False
    monkeypatch.delenv("GMAIL_COMMS_REFRESH_TOKEN")
    assert agent.poll_once()[0]["status"] == "skipped"
    monkeypatch.setenv("LOGISTICS_AGENT_ENABLED", "true")
    assert agent.enabled() is True


def test_agent_source_has_no_n8n_reference():
    import inspect

    code = inspect.getsource(agent).split('"""', 2)[2]
    assert "n8n.cloud" not in code and "/webhook/" not in code and "N8N_API_KEY" not in code
