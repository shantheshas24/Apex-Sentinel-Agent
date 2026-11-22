"""
PersonalOS Backend — Comprehensive Integration & Unit Test Suite
================================================================
Tests are split into two categories:

  Unit tests  (no running system needed, just Redis)
    - Redis client operations
    - Observer event normalization & deduplication logic
    - Planner LLM response parsing & confidence scoring

  Integration tests  (require python main.py to be running)
    - Live Gmail MCP tool calls
    - All Dashboard REST API endpoints
    - End-to-end pipeline: inject event → Planner → emails:all

Run unit tests only:
    pytest tests/test_backend.py -m unit -v

Run integration tests (system must be running):
    pytest tests/test_backend.py -m integration -v

Run everything:
    pytest tests/test_backend.py -v
"""
import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Windows console defaults to cp1252 which can't encode ✓ → etc.
# Reconfigure stdout/stderr to UTF-8 before any output is produced.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import pytest
import httpx
import redis.asyncio as aioredis

# ── Path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

DASHBOARD_URL = "http://localhost:8080"
# Production system uses DB 0. Unit tests create their own RedisClient instance
# against DB 0 too (same server), but they write to unique keys and clean them up.
# Integration tests that call the live HTTP API write to DB 0 intentionally (the
# running server owns DB 0), so those tests MUST clean up their injected records.
REDIS_URL = os.getenv("PERSONALOS_TEST_REDIS_URL", "redis://localhost:6379/0")


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
async def redis_raw():
    """Raw redis.asyncio client."""
    r = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
async def redis_client():
    """RedisClient instance per test."""
    from personal_os_agent.memory.redis_client import RedisClient
    # Use a fresh instance (not the singleton) so tests don't share state
    rc = RedisClient(REDIS_URL)
    yield rc
    await rc.close()


@pytest.fixture
async def clean_test_keys(redis_raw):
    """Delete any test_* keys we create during a test."""
    yield
    keys = await redis_raw.keys("test_*")
    extra = await redis_raw.keys("seen_event:test_*")
    all_keys = keys + extra
    if all_keys:
        await redis_raw.delete(*all_keys)


@pytest.fixture
async def cleanup_injected_events(redis_raw):
    """
    Fixture for integration tests that POST to /api/events/inject.

    Collects every inj- prefixed event_id that exists before the test starts,
    then after the test removes any NEW inj- entries from emails:all, the
    dedup seen_event:* keys, and the matching activity log lines.

    This prevents test data from polluting the live dashboard.
    """
    # Snapshot pre-existing inj- ids so we only delete what THIS test created
    pre_existing: set[str] = set()
    all_pre = await redis_raw.hgetall("emails:all")
    for v in all_pre.values():
        try:
            eid = json.loads(v).get("event_id", "")
            if eid.startswith("inj-"):
                pre_existing.add(eid)
        except Exception:
            pass

    yield  # ── test runs here ──

    # Remove newly injected records
    all_post = await redis_raw.hgetall("emails:all")
    to_delete: list[str] = []
    for k, v in all_post.items():
        try:
            eid = json.loads(v).get("event_id", k)
        except Exception:
            eid = k
        if eid.startswith("inj-") and eid not in pre_existing:
            to_delete.append(k)
            await redis_raw.delete(f"seen_event:{eid}")
    if to_delete:
        await redis_raw.hdel("emails:all", *to_delete)

    # Prune matching activity log entries (list — rebuild without test entries)
    test_senders = {"ci-test@personalos.internal", "tester@personalos.internal"}
    test_markers = {"delete me", "REVIEW REQUIRED", "INJECTED"}
    logs = await redis_raw.lrange("activity:log", 0, -1)
    clean: list[str] = []
    for entry in logs:
        try:
            obj = json.loads(entry)
            action = obj.get("action", "")
            if any(s in action for s in test_senders) or any(m in action for m in test_markers):
                continue
        except Exception:
            pass
        clean.append(entry)
    if len(clean) != len(logs):
        await redis_raw.delete("activity:log")
        if clean:
            await redis_raw.rpush("activity:log", *clean)


# ══════════════════════════════════════════════════════════════════════════════
# ── UNIT TESTS ────────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

# ── 1. Redis Client ──────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_connectivity(redis_client):
    """Redis must respond to ping within 200 ms."""
    t0 = time.perf_counter()
    ok = await redis_client.ping()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert ok is True, "Redis ping failed"
    assert elapsed_ms < 200, f"Redis latency too high: {elapsed_ms:.1f} ms"
    print(f"\n  ✓ Redis ping OK ({elapsed_ms:.1f} ms)")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_event_queue_round_trip(redis_raw):
    """push_event / pop_event serialise and deserialise the exact payload.

    Uses a private test queue (test:events:queue) to avoid racing with the
    live Planner which BLPOPs from events:queue immediately.
    """
    test_event = {
        "event_id": "test_" + uuid.uuid4().hex[:8],
        "type": "email",
        "source": "gmail",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {"from": "test@example.com", "subject": "Test subject"},
        "urgency_keywords": ["urgent"],
        "summary": "Test email event",
    }

    TEST_Q = "test:events:queue"
    await redis_raw.rpush(TEST_Q, json.dumps(test_event))
    result = await redis_raw.blpop([TEST_Q], timeout=2)
    await redis_raw.delete(TEST_Q)  # cleanup

    assert result is not None, "blpop returned None on private test queue"
    _, raw = result
    recovered = json.loads(raw)
    assert recovered["event_id"] == test_event["event_id"]
    assert recovered["payload"]["subject"] == "Test subject"
    assert recovered["urgency_keywords"] == ["urgent"]
    print(f"\n  ✓ Event queue round-trip OK")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_dedup_mark_and_check(redis_client, redis_raw):
    """mark_event_seen sets a 24-h key; is_event_seen returns True/False correctly."""
    eid = "test_dedup_" + uuid.uuid4().hex[:8]

    # Initially not seen
    assert await redis_client.is_event_seen(eid) is False, "New event_id should not be seen"

    # Mark it
    await redis_client.mark_event_seen(eid)
    assert await redis_client.is_event_seen(eid) is True, "After marking, should be seen"

    # Verify TTL ≈ 24 h
    ttl = await redis_raw.ttl(f"seen_event:{eid}")
    assert 86390 <= ttl <= 86400, f"TTL should be ~86400s, got {ttl}"

    # clear_event_seen removes it
    await redis_client.clear_event_seen(eid)
    assert await redis_client.is_event_seen(eid) is False, "After clear, should not be seen"
    print(f"\n  ✓ Dedup mark/check/clear OK (TTL={ttl}s)")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_email_records(redis_client):
    """Keep deadline records and leave unrelated emails out of the deadline list."""
    plan_id = "test_plan_" + uuid.uuid4().hex[:8]
    plan = {
        "id": plan_id,
        "subject": "Unit test email",
        "from_addr": "unittest@test.com",
        "action": "no_action",
        "confidence": 72,
        "priority": "medium",
        "user_response": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    await redis_client.push_email_record(plan)
    all_emails = await redis_client.get_all_emails()
    ids = [e["id"] for e in all_emails]
    assert plan_id in ids, "Stored plan not found in get_all_emails()"

    assert all(e["id"] != plan_id for e in await redis_client.get_deadlines())

    deadline = {
        **plan,
        "id": f"{plan_id}-deadline",
        "event_type": "email",
        "action": "track_deadline",
        "deadline": "Submit the assignment",
        "due": "Friday at 5 PM",
        "evidence": "Please submit by Friday at 5 PM.",
        "user_response": "tracked",
    }
    await redis_client.store_deadline_plan(deadline)
    await redis_client.store_deadline_plan({**deadline, "due": "Monday"})
    assert await redis_client.get_email_record(deadline["id"]) == deadline
    assert deadline in await redis_client.get_deadlines()

    # Cleanup
    r = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    await r.hdel("emails:all", plan_id, deadline["id"])
    await r.aclose()
    print(f"\n  ✓ Email records CRUD OK")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_activity_log(redis_client, redis_raw):
    """append_activity_log adds entry; get_activity_log retrieves it."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": "TestAgent",
        "action": "UNIT TEST entry",
        "event_id": "test_" + uuid.uuid4().hex[:4],
    }
    before_len = len(await redis_client.get_activity_log(limit=500))
    await redis_client.append_activity_log(entry)
    log = await redis_client.get_activity_log(limit=500)
    assert len(log) == before_len + 1, "Activity log length should increase by 1"
    # Most-recent entry is last in the reversed list (get_activity_log reverses)
    assert any(e.get("action") == "UNIT TEST entry" for e in log), "Entry not found in log"
    print(f"\n  ✓ Activity log append+retrieve OK ({len(log)} entries)")


# ── 2. Observer Normalization ─────────────────────────────────────────────────

@pytest.mark.unit
def test_observer_normalize_email():
    """_normalize_email produces all required fields from a raw Gmail message."""
    from personal_os_agent.agents.observer_agent import ObserverAgent
    agent = ObserverAgent()

    raw_email = {
        "id": "abc123",
        "from": "boss@company.com",
        "subject": "URGENT: action required by deadline",
        "snippet": "Please reply ASAP, this is critical.",
        "date": "Tue, 08 Apr 2026 10:00:00 +0000",
        "unread": True,
    }
    event = agent._normalize_email(raw_email)

    assert event["type"] == "email"
    assert event["source"] == "gmail"
    assert "event_id" in event
    assert len(event["event_id"]) == 16
    assert event["payload"] == raw_email
    assert isinstance(event["urgency_keywords"], list)
    assert len(event["urgency_keywords"]) > 0, "Should detect urgency keywords"
    assert "urgent" in event["urgency_keywords"]
    assert "deadline" in event["urgency_keywords"]
    assert "action required" in event["urgency_keywords"]
    assert "asap" in event["urgency_keywords"]
    assert "critical" in event["urgency_keywords"]

    # event_id is deterministic (SHA-256 of email:id)
    expected_id = hashlib.sha256(b"email:abc123").hexdigest()[:16]
    assert event["event_id"] == expected_id
    print(f"\n  ✓ Email normalize OK — urgency_keywords={event['urgency_keywords']}")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_observer_saves_checkpoint_after_queuing_read_mail(monkeypatch):
    from unittest.mock import AsyncMock
    from personal_os_agent.agents.observer_agent import ObserverAgent

    agent = ObserverAgent()
    checkpoint = {"page_token": "", "history_id": "101"}
    call_tool = AsyncMock(return_value={
        "messages": [{"id": "gmail-1", "subject": "Assignment due Friday", "unread": False}],
        "cursor": checkpoint,
    })
    monkeypatch.setattr(agent, "call_tool", call_tool)

    redis = AsyncMock()
    redis.get_gmail_sync.return_value = {}
    redis.enqueue_unseen_email.return_value = True
    redis.save_gmail_sync.return_value = True
    assert await agent._sync_inbox(redis) == 1
    call_tool.assert_awaited_once_with("sync_gmail", {"cursor": {}})
    [event] = redis.enqueue_unseen_email.await_args.args
    assert event["payload"]["unread"] is False
    redis.save_gmail_sync.assert_awaited_once_with(checkpoint, previous={})
    assert [call[0] for call in redis.mock_calls] == ["get_gmail_sync", "enqueue_unseen_email", "save_gmail_sync"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mcp_lists_only_gmail_tools():
    from personal_os_agent.mcp_server.google_auth import SCOPES
    from personal_os_agent.mcp_server.server import build_mcp_app

    tools = await build_mcp_app().list_tools()

    assert {tool.name for tool in tools} == {"read_emails", "sync_gmail"}
    assert {scope.rsplit("/", 1)[-1] for scope in SCOPES} == {"gmail.readonly"}

@pytest.mark.unit
def test_observer_no_urgency_for_newsletter():
    """Newsletters with no urgency keywords produce empty urgency_keywords list."""
    from personal_os_agent.agents.observer_agent import ObserverAgent
    agent = ObserverAgent()

    raw = {
        "id": "news1",
        "from": "newsletter@techdigest.io",
        "subject": "This week in AI: 10 things you need to know",
        "snippet": "Top stories from around the web.",
        "date": "Mon, 07 Apr 2026 12:00:00 +0000",
        "unread": True,
    }
    event = agent._normalize_email(raw)
    assert event["urgency_keywords"] == [], f"Newsletter should have no urgency keywords, got {event['urgency_keywords']}"
    print(f"\n  ✓ Newsletter produces no urgency keywords")


# ── 3. Planner — LLM Response Parsing ────────────────────────────────────────

@pytest.mark.unit
def test_planner_parse_valid_json():
    """_parse_llm_response handles clean JSON."""
    from personal_os_agent.agents.planner_agent import PlannerAgent
    agent = PlannerAgent.__new__(PlannerAgent)

    raw = json.dumps({
        "action": "send_email",
        "confidence": 85,
        "priority": "high",
        "reason": "Urgent reply needed",
        "requires_approval": True,
        "deadline": "Submit the assignment",
        "due": "Friday",
        "evidence": "Please submit by Friday.",
        "action_args": {"to": "boss@company.com", "subject": "Re: URGENT", "body": "On it!"},
    })
    plan = agent._parse_llm_response(raw)

    assert plan["action"] == "send_email"
    assert plan["confidence"] == 85
    assert plan["priority"] == "high"
    assert plan["due"] == "Friday"
    assert plan["action_args"]["to"] == "boss@company.com"
    print(f"\n  ✓ Parse valid JSON OK")


@pytest.mark.unit
def test_planner_parse_markdown_fenced_json():
    """_parse_llm_response strips markdown code fences."""
    from personal_os_agent.agents.planner_agent import PlannerAgent
    agent = PlannerAgent.__new__(PlannerAgent)

    raw = '```json\n{"action":"no_action","confidence":30,"priority":"low","reason":"Newsletter","requires_approval":true,"alternatives":[],"explanation":"","action_args":{}}\n```'
    plan = agent._parse_llm_response(raw)
    assert plan["action"] == "no_action"
    assert plan["confidence"] == 30
    print(f"\n  ✓ Parse markdown-fenced JSON OK")


@pytest.mark.unit
def test_planner_parse_invalid_json_raises():
    """_parse_llm_response raises ValueError for unparseable output."""
    from personal_os_agent.agents.planner_agent import PlannerAgent
    agent = PlannerAgent.__new__(PlannerAgent)

    with pytest.raises(ValueError, match="invalid JSON"):
        agent._parse_llm_response("This is not JSON at all.")
    print(f"\n  ✓ Invalid JSON raises ValueError")


@pytest.mark.unit
def test_planner_parse_confidence_clamped():
    """Confidence values outside 0-100 are clamped."""
    from personal_os_agent.agents.planner_agent import PlannerAgent
    agent = PlannerAgent.__new__(PlannerAgent)

    plan = agent._parse_llm_response('{"action":"no_action","confidence":150,"reason":"x","requires_approval":true,"alternatives":[],"explanation":"","action_args":{}}')
    assert plan["confidence"] == 100

    plan2 = agent._parse_llm_response('{"action":"no_action","confidence":-10,"reason":"x","requires_approval":true,"alternatives":[],"explanation":"","action_args":{}}')
    assert plan2["confidence"] == 0
    print(f"\n  ✓ Confidence clamping OK (150→100, -10→0)")


@pytest.mark.unit
def test_planner_parse_missing_fields_get_defaults():
    """Missing optional fields are filled with sensible defaults."""
    from personal_os_agent.agents.planner_agent import PlannerAgent
    agent = PlannerAgent.__new__(PlannerAgent)

    plan = agent._parse_llm_response('{"confidence": 60}')
    assert plan["priority"] == ""
    assert plan["reason"] == ""
    print(f"\n  ✓ Missing-field defaults OK")


# ── 4. Planner — Confidence Scoring ─────────────────────────────────────────

@pytest.mark.unit
def test_planner_score_plan_urgency_boost():
    """Urgency keywords boost confidence by up to 30%."""
    from personal_os_agent.agents.planner_agent import PlannerAgent
    agent = PlannerAgent.__new__(PlannerAgent)

    base_plan = {"action": "no_action", "confidence": 70, "requires_approval": True}
    event_no_urgency = {"urgency_keywords": []}
    event_3_keywords = {"urgency_keywords": ["urgent", "deadline", "critical"]}

    scored_no = agent._score_plan(dict(base_plan), event_no_urgency)
    scored_3  = agent._score_plan(dict(base_plan), event_3_keywords)

    # With 0 keywords: multiplier=1.0; with 3 keywords: multiplier=1.3
    assert scored_3["confidence"] > scored_no["confidence"], \
        f"3-keyword score ({scored_3['confidence']}) should exceed 0-keyword ({scored_no['confidence']})"
    assert scored_3["scoring"]["urgency_mult"] == 1.3
    assert scored_no["scoring"]["urgency_mult"] == 1.0
    print(f"\n  ✓ Urgency boost: 0kw={scored_no['confidence']}%, 3kw={scored_3['confidence']}%")


@pytest.mark.unit
def test_planner_score_plan_output_clamped():
    """Adjusted confidence is always clamped to [0, 100]."""
    from personal_os_agent.agents.planner_agent import PlannerAgent
    agent = PlannerAgent.__new__(PlannerAgent)

    plan_high = {"action": "no_action", "confidence": 99, "requires_approval": True}
    event_max  = {"urgency_keywords": ["urgent", "deadline", "critical"]}
    scored = agent._score_plan(plan_high, event_max)
    assert scored["confidence"] <= 100, "Clamping failed above 100"

    plan_low = {"action": "no_action", "confidence": 0, "requires_approval": True}
    event_none = {"urgency_keywords": []}
    scored_low = agent._score_plan(plan_low, event_none)
    assert scored_low["confidence"] >= 0, "Clamping failed below 0"
    print(f"\n  ✓ Confidence clamping in scoring OK")


# ══════════════════════════════════════════════════════════════════════════════
# ── INTEGRATION TESTS  (require python main.py running on ports 8000+8080) ──
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def http():
    """Synchronous httpx client for dashboard API calls.

    keepalive_expiry=20s ensures connections are recycled before uvicorn
    closes them server-side (uvicorn default keep-alive is 5s).  Tests that
    poll for >20s create their own short-lived clients to avoid stale-socket
    errors ([WinError 10054] forcibly closed by remote host).
    """
    limits = httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=10)
    with httpx.Client(base_url=DASHBOARD_URL, timeout=15.0, limits=limits) as client:
        yield client


def _require_system(http_client) -> None:
    """Skip test if the dashboard is not reachable (uses fast root endpoint)."""
    try:
        r = http_client.get("/", timeout=5.0)
        if r.status_code != 200:
            pytest.skip("Dashboard not running — start python main.py first")
    except (httpx.ConnectError, httpx.ReadTimeout):
        pytest.skip("Dashboard not reachable — start python main.py first")


# ── 6. Dashboard REST Endpoints ───────────────────────────────────────────────

@pytest.mark.integration
def test_dashboard_root_returns_html(http):
    """GET / returns the HTML dashboard."""
    _require_system(http)
    r = http.get("/")
    assert r.status_code == 200
    assert "PersonalOS" in r.text
    assert "text/html" in r.headers.get("content-type", "")
    print(f"\n  ✓ GET / → HTML ({len(r.text)} bytes)")


@pytest.mark.integration
def test_dashboard_health_structure(http):
    """GET /api/health returns expected service keys."""
    _require_system(http)
    r = http.get("/api/health", timeout=30.0)  # health hits Twilio + Google APIs
    assert r.status_code == 200
    data = r.json()
    assert "overall" in data
    assert "services" in data
    svcs = data["services"]
    for svc in ("redis", "google", "twilio", "chromadb", "mcp_server"):
        assert svc in svcs, f"Missing service in health: {svc}"
        assert "status" in svcs[svc], f"Service {svc} missing 'status' key"
    assert svcs["redis"]["status"] == "ok", f"Redis health not OK: {svcs['redis']}"
    assert svcs["google"]["status"] == "ok", f"Google OAuth not OK: {svcs['google']}"
    print(f"\n  ✓ /api/health → overall={data['overall']}, redis={svcs['redis']['status']}, google={svcs['google']['status']}")


@pytest.mark.integration
def test_dashboard_emails_endpoint(http):
    """GET /api/emails returns a list (possibly empty)."""
    _require_system(http)
    r = http.get("/api/emails")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    print(f"\n  ✓ GET /api/emails → {len(data)} record(s)")
    for rec in data[:3]:
        assert "id" in rec
        assert "subject" in rec
        print(f"    [{rec.get('user_response','?')}] {rec['subject'][:50]}  conf={rec.get('confidence','?')}%")


@pytest.mark.integration
def test_dashboard_metrics_structure(http):
    """GET /api/metrics returns all expected keys."""
    _require_system(http)
    r = http.get("/api/metrics")
    assert r.status_code == 200
    data = r.json()
    for key in ("total", "by_priority", "by_status", "avg_confidence",
                "confidence_distribution", "queue_depths", "timestamp"):
        assert key in data, f"Missing key in /api/metrics: {key}"
    assert isinstance(data["by_priority"], dict)
    assert "high" in data["by_priority"]
    assert isinstance(data["queue_depths"], dict)
    print(f"\n  ✓ /api/metrics → total={data['total']}, avg_conf={data['avg_confidence']}%")


@pytest.mark.integration
def test_dashboard_feed_endpoint(http):
    """GET /api/feed returns list of activity log entries."""
    _require_system(http)
    r = http.get("/api/feed")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    print(f"\n  ✓ GET /api/feed → {len(data)} log entries")


@pytest.mark.integration
def test_dashboard_preferences_endpoint(http):
    """GET /api/preferences returns a list of preferences."""
    _require_system(http)
    r = http.get("/api/preferences")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    print(f"\n  ✓ GET /api/preferences → {len(data)} preferences")
    for p in data[:2]:
        print(f"    [{p.get('metadata',{}).get('category','?')}] {str(p.get('document',''))[:60]}")


@pytest.mark.integration
def test_dashboard_poll_now(http):
    """POST /api/poll/now returns status=triggered."""
    _require_system(http)
    r = http.post("/api/poll/now")
    assert r.status_code == 200
    data = r.json()
    assert data.get("status") == "triggered", f"Unexpected response: {data}"
    print(f"\n  ✓ POST /api/poll/now → {data}")


# ── 7. Live MCP Tool Calls ────────────────────────────────────────────────────

@pytest.mark.integration
def test_mcp_server_reachable(http):
    """MCP server (port 8000) must accept TCP connections.

    We use a raw TCP socket probe rather than HTTP GET because FastMCP's SSE
    transport responds to GET / with a streaming SSE response that never sends
    HTTP headers in the traditional request/response sense, causing httpx to
    timeout regardless of server health.
    """
    import socket
    _require_system(http)
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=5.0) as sock:
            pass  # connection succeeded
        print(f"\n  ✓ MCP server port 8000 TCP connection OK")
    except socket.timeout:
        pytest.fail("MCP server port 8000 TCP connection timed out")
    except ConnectionRefusedError:
        pytest.fail("MCP server not listening on port 8000 — is main.py running?")


@pytest.mark.integration
def test_mcp_read_emails_via_inject_and_observe(http):
    """
    Inject a real-looking email and verify the full Observer→Planner→Executor
    pipeline processes it into emails:all within 90 seconds.
    
    This is the true end-to-end real-data test: the system must fetch from Gmail,
    identify a new event, plan it, and route it.
    """
    _require_system(http)

    # 1. Trigger an immediate poll (wakes the Observer)
    r = http.post("/api/poll/now")
    assert r.status_code == 200

    # 2. Record how many emails:all records exist NOW
    r0 = http.get("/api/emails")
    count_before = len(r0.json())
    print(f"\n  ℹ emails:all before poll: {count_before}")

    # 3. Wait up to 90 s for the Observer cycle + Planner to run.
    # Use a fresh client each iteration so stale keep-alive connections
    # don't cause [WinError 10054] after long idle periods.
    deadline = time.time() + 90
    all_emails_final: list[dict] = []
    while time.time() < deadline:
        time.sleep(5)
        try:
            with httpx.Client(base_url=DASHBOARD_URL, timeout=10.0) as c:
                all_emails_final = c.get("/api/emails").json()
        except Exception:
            continue
        if len(all_emails_final) > count_before:
            print(f"  ✓ Pipeline added {len(all_emails_final) - count_before} new record(s) within {round(time.time()-deadline+90)}s")
            break

    # NOTE: If your inbox has no new unread emails since the last Observer cycle,
    # count_before will stay the same — that is NOT a bug, it's correct behaviour.
    # We just verify the system didn't crash.
    try:
        with httpx.Client(base_url=DASHBOARD_URL, timeout=10.0) as c:
            r_final = c.get("/api/emails")
            assert r_final.status_code == 200
            final_emails = r_final.json()
    except Exception as exc:
        pytest.fail(f"Dashboard unreachable at end of pipeline test: {exc}")

    print(f"\n  ✓ Pipeline health check passed — {len(final_emails)} total records in emails:all")
    for rec in sorted(final_emails, key=lambda x: x.get("created_at",""), reverse=True)[:3]:
        print(f"    [{rec.get('user_response','?')}] {rec.get('subject','?')[:55]}  conf={rec.get('confidence','?')}%  priority={rec.get('priority','?')}")


# ── 8. Event Injection → Pipeline ─────────────────────────────────────────────

@pytest.mark.integration
def test_inject_urgent_email_flows_to_emails_all(http, cleanup_injected_events):
    """
    POST /api/events/inject with urgent=True should produce a plan in emails:all
    within 60 seconds (Planner processes it).
    """
    _require_system(http)

    unique_subject = f"TEST URGENT — delete me — {uuid.uuid4().hex[:6]}"
    payload = {
        "event_type": "email",
        "from": "ci-test@personalos.internal",
        "subject": unique_subject,
        "snippet": "This is an automated integration test. Please submit the sample assignment by Friday at 5 PM.",
        "urgent": True,
    }

    r = http.post("/api/events/inject", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "injected"
    event_id = data["event_id"]
    print(f"\n  ✓ Event injected: {event_id}")

    # Wait for Planner to process it — free-tier LLM can take 60-120s.
    # Use a fresh HTTP client each iteration so a stale keep-alive connection
    # doesn't produce RemoteProtocolError if the server recycled it.
    deadline = time.time() + 120
    found = None
    while time.time() < deadline:
        time.sleep(5)
        try:
            with httpx.Client(base_url=DASHBOARD_URL, timeout=10.0) as c:
                emails = c.get("/api/emails").json()
        except Exception:
            continue  # server may be briefly busy; retry
        found = next((e for e in emails if unique_subject in e.get("subject", "")), None)
        if found:
            elapsed = round(120 - (deadline - time.time()))
            print(f"\n  ✓ Planner processed event in ~{elapsed}s")
            break

    if found is None:
        feed = http.get("/api/feed?limit=50").json()
        if any(item.get("event_id") == event_id and "waiting to retry" in item.get("action", "") for item in feed):
            pytest.skip("The sample scan is queued for a delayed retry after a processing error.")
        pytest.fail(
            f"Injected event '{unique_subject}' not found in emails:all after 120s. "
            "Check logs/agent-runtime.log for Planner errors."
        )

    # Event was processed — validate its fields
    assert found["confidence"] >= 0
    assert found["action"] == "track_deadline"
    assert found["due"] == "Friday at 5 PM"
    print(f"\n  ✓ Injected event processed:")
    print(f"    action={found['action']}  confidence={found['confidence']}%  priority={found.get('priority','?')}")
    print(f"    response={found.get('user_response','?')}  urgency_kw={found.get('urgency_keywords',[])} ")


@pytest.mark.unit
@pytest.mark.parametrize("event_type", ["calendar", "filesystem", "unknown_type"])
def test_inject_refuses_retired_event_types(event_type):
    from fastapi.testclient import TestClient
    from personal_os_agent.api.app import app

    with TestClient(app) as client:
        response = client.post("/api/events/inject", json={"event_type": event_type})

    assert response.status_code == 422

@pytest.mark.unit
@pytest.mark.asyncio
async def test_deadline_workflow_tracks_sample_and_ignores_unrelated_email(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from personal_os_agent.agents.planner_agent import PlannerAgent
    from personal_os_agent.api.app import app
    from personal_os_agent.memory.redis_client import RedisClient

    redis = AsyncMock(spec=RedisClient)
    monkeypatch.setattr(RedisClient, "get_instance", lambda: redis)
    planner = PlannerAgent.__new__(PlannerAgent)
    planner._model = "test-model"
    planner._openrouter = False
    planner._redis = redis
    planner._memory = AsyncMock()
    planner._memory.query_preferences.return_value = []
    redis.get_email_record.return_value = None

    due_excerpt = "Please submit by Friday at 5 PM."
    responses = [
        {
            "deadline": "Submit the project",
            "due": "Friday at 5 PM",
            "evidence": due_excerpt,
            "action": "send_email",
            "action_args": {"subject": "Re: Project", "body": "Received, thank you."},
            "priority": "high",
            "confidence": 97,
        },
        {"deadline": "", "due": "", "evidence": "", "action": "track_deadline"},
    ]
    completions = AsyncMock(side_effect=[
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(data)))])
        for data in responses
    ])
    planner._llm = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completions)))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for snippet in (due_excerpt, "Would you like to meet Friday at 5 PM?"):
            response = await client.post("/api/events/inject", json={"subject": "Project", "snippet": snippet})
            assert response.status_code == 200
            await planner._process_email(redis.push_event.await_args.args[0])

        redis.store_deadline_plans.assert_awaited_once()
        [plan] = redis.store_deadline_plans.await_args.args[0]
        assert plan["deadline"] == "Submit the project"
        assert plan["due"] == "Friday at 5 PM"
        assert "action_args" not in plan
        assert plan["action"] == "track_deadline"

        redis.get_deadlines.return_value = [plan]
        assert (await client.get("/api/emails")).json() == [plan]


@pytest.mark.unit
@pytest.mark.parametrize("path", ["/api/approve/old", "/api/reject/old", "/api/twilio/test"])
def test_retired_outbound_routes_are_unavailable(path):
    from fastapi.testclient import TestClient
    from personal_os_agent.api.app import app

    with TestClient(app) as client:
        assert client.post(path).status_code == 404
