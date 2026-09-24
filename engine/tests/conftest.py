# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Shared test fixtures for Laya Engine tests."""

import atexit
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Keep the suite out of the developer's real ~/.laya AND their real keychain
#
# Two pieces of user state are reachable from a plain ``pytest`` run, and they
# need two different defences because they do not live in the same place:
#
#   1. ``laya/config.py`` resolves ``LAYA_HOME`` from ``Path.home()`` at *import*
#      time, and the fixtures below call ``load_settings()`` / ``save_settings()``.
#      Redirecting ``HOME`` (and ``USERPROFILE`` — ``Path.home()`` reads one or
#      the other) moves those paths, and it must happen before the first ``laya``
#      import: once ``config`` is imported its constants are bound to the real
#      paths, and patching them afterwards would miss every module that did
#      ``from laya.config import LAYA_...``.
#
#   2. The OS keychain is NOT under ``HOME``, so the redirect above does not
#      reach it. ``laya/security/keychain.py`` calls
#      ``keyring.set_password(SERVICE_NAME, ...)``, which talks to a per-user
#      daemon (SecretService on Linux, Keychain on macOS, Credential Locker on
#      Windows). The fixtures that call ``delete_mcp_token()`` therefore deleted
#      the developer's real MCP bearer token anyway — which presents as "my token
#      stopped working" to anyone who had Laya running before they ran the tests.
#      ``keyring.set_keyring`` swaps the process-global backend instead, so the
#      whole suite gets an in-memory store that dies with the process.
#
# The test directory is a FIXED path, reused across runs, not a fresh mkdtemp:
# redirecting HOME also relocates every cache underneath it, and ChromaDB's
# embedding model lives in one — so a throwaway directory made test_chromadb
# re-download the model on every run, taking the suite from 43s to 241s. Reusing
# it keeps that cache warm, which is the only reason it is fixed at all. Because
# a predictable name in a shared temp directory can be prepared by someone else,
# it is uid-suffixed, created 0o700, and its owner/mode/type are checked before
# use. It is deliberately NOT cleaned up: the warm cache is the point. Test state
# is not a concern for a SEQUENTIAL run — the DB fixtures are in-memory and
# settings are restored per test — but the file is a shared mutable HOME, and two
# suites at once rewrite the same ``settings.json`` in place. That is what the run
# marker below refuses.
# ---------------------------------------------------------------------------
def _current_uid():
    """The process uid, or None where the concept does not exist (Windows)."""
    return getattr(os, "getuid", lambda: None)()


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check. Undeterminable counts as dead, not as alive:
    blocking a legitimate run is a worse failure than an undetected collision."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


_RUN_MARKER = ".pytest-running"


def _acquire_run_marker(home: str) -> None:
    """Refuse to share the test HOME with another live test process.

    The directory is fixed rather than per-run so ChromaDB's model cache stays
    warm (see the comment block above), which makes it a single shared mutable
    HOME: two suites started at the same time rewrite the same ``settings.json``
    in place. Reproduced deliberately with two concurrent runs — one suite
    reported ``json.decoder.JSONDecodeError`` in
    ``test_agent_mcp_wiring.py::TestMcpConfigBuilder::test_space_id_becomes_query_param``
    (a ``load_settings()`` reading the file mid-write) and both took 75 s instead
    of 43 s, so the damage lands in whatever test happened to read the file. That
    is indistinguishable from a real regression and sends you bisecting the wrong
    change. A marker left by a crashed run names a dead pid and is simply taken
    over.
    """
    marker = os.path.join(home, _RUN_MARKER)
    if os.path.exists(marker):
        try:
            with open(marker) as fh:
                other = int(fh.read().strip())
        except (OSError, ValueError):
            other = None
        if other and other != os.getpid() and _pid_alive(other):
            raise RuntimeError(
                f"another Laya test run (pid {other}) is using {home}. The test "
                "HOME is shared so ChromaDB's model cache stays warm, so two "
                "concurrent suites overwrite each other's settings and fail in "
                "unrelated tests. Wait for that run to finish, or isolate this one "
                "with TMPDIR=<a fresh directory>."
            )
    with open(marker, "w") as fh:
        fh.write(str(os.getpid()))


_UID = _current_uid()
_TEST_HOME = os.path.join(
    os.path.realpath(tempfile.gettempdir()),
    "laya-test-home" if _UID is None else f"laya-test-home-{_UID}",
)
os.makedirs(_TEST_HOME, mode=0o700, exist_ok=True)
_stat = os.lstat(_TEST_HOME)
if (
    not os.path.isdir(_TEST_HOME)
    or os.path.islink(_TEST_HOME)
    or (_UID is not None and _stat.st_uid != _UID)
    or _stat.st_mode & 0o077
):
    raise RuntimeError(
        f"refusing to use {_TEST_HOME} as the test HOME: it must be a real "
        f"directory owned by uid {_UID} with no group/other access "
        f"(mode={oct(_stat.st_mode & 0o777)}, uid={_stat.st_uid}). Remove it "
        "and re-run, or point TMPDIR at a directory only you control."
    )
_acquire_run_marker(_TEST_HOME)
atexit.register(lambda: os.path.exists(os.path.join(_TEST_HOME, _RUN_MARKER))
                and os.remove(os.path.join(_TEST_HOME, _RUN_MARKER)))
os.environ["HOME"] = _TEST_HOME
os.environ["USERPROFILE"] = _TEST_HOME

import aiosqlite  # noqa: E402
import keyring  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from keyring.backend import KeyringBackend  # noqa: E402
from keyring.errors import PasswordDeleteError  # noqa: E402


class _InMemoryKeyring(KeyringBackend):
    """A keychain that exists only in this process and dies with it.

    Deliberately a faithful stand-in rather than a null backend: tests store a
    token and read it back (``test_mcp_http.py::test_ensure_startup_token_creates_
    when_missing``, ``test_agent_mcp_wiring.py::test_bearer_token_in_headers_when_
    auth_bearer``), so a backend that silently discards writes would fail them
    for the wrong reason.
    """

    priority = 100

    def __init__(self):
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def delete_password(self, service, username):
        try:
            del self._store[(service, username)]
        except KeyError:
            # The real backends raise here, and ``delete_mcp_token`` reports the
            # failure — keep that contract so the engine takes the same path.
            raise PasswordDeleteError(f"no password stored for {service}/{username}")


# Install it before the first ``laya`` import, alongside the HOME redirect above.
# ``set_keyring`` is the documented, explicit mechanism. Note that defining the
# subclass is *also* enough on its own: ``keyring`` discovers backends by walking
# ``KeyringBackend.__subclasses__()`` and picks the highest ``priority``, and 100
# beats every shipped backend, so merely importing this module already selects it.
# Both are kept — the explicit call states the intent, and the ``isinstance``
# guard below turns either mechanism failing into a hard stop.
keyring.set_keyring(_InMemoryKeyring())

from laya.config import LAYA_HOME, MIGRATIONS_DIR  # noqa: E402
from laya.db.migrate import run_migrations  # noqa: E402
from laya.models.classification import Persona, RouterOutput  # noqa: E402
from laya.models.event import LayaEvent  # noqa: E402
from laya.models.rules import RulesConfig  # noqa: E402
from laya.models.team import TeamConfig  # noqa: E402

# Both guards run at import, not in a test. That is the point: pytest collects
# and runs ``test_agent_mcp_wiring.py`` and ``test_mcp_http.py`` before any test
# file named after them, so a guard that lives in a test reports the damage only
# after the two fixtures that do it have already run. Here, a broken isolation
# stops the session before a single fixture can touch either store.
if not Path(LAYA_HOME).resolve().is_relative_to(Path(_TEST_HOME)):
    raise RuntimeError(
        f"LAYA_HOME resolved to {LAYA_HOME}, outside the test HOME "
        f"{_TEST_HOME}. The HOME redirect above must run before the first "
        "`laya` import; something imported laya.config earlier."
    )
if not isinstance(keyring.get_keyring(), _InMemoryKeyring):
    raise RuntimeError(
        f"the process keychain is {keyring.get_keyring()!r}, not the in-memory "
        "test backend. A fixture calling delete_mcp_token() would delete the "
        "developer's real bearer token."
    )


@pytest.fixture(autouse=True)
def _reset_http_client():
    """Reset the shared httpx client between tests to prevent state leakage."""
    import laya.http_client as hc
    old = hc._client
    hc._client = None
    yield
    hc._client = old


@pytest_asyncio.fixture
async def db(tmp_path):
    """In-memory SQLite database with ALL migrations applied."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")

    # Apply all migrations using the migration runner
    await run_migrations(conn)

    # Build FTS5 tables so tests exercise the BM25 search path (not just LIKE).
    from laya.db.fts import ensure_fts_tables
    await ensure_fts_tables(conn)

    # Patch _db so get_db() returns this connection via the real function.
    with patch("laya.db.sqlite._db", conn):
        # Clear omni state + the per-event related-context memo (P6-7) to prevent
        # cross-test leakage of module-level caches.
        from laya.pipeline.omni import _latest_cache, _resynthesis_gates
        from laya.pipeline.related_context import clear_related_context_cache
        _latest_cache.clear()
        _resynthesis_gates.clear()
        clear_related_context_cache()
        yield conn
        _latest_cache.clear()
        _resynthesis_gates.clear()
        clear_related_context_cache()

    await conn.close()


@pytest.fixture
def sample_event() -> LayaEvent:
    """A sample Jira event for testing."""
    return LayaEvent(
        event_id="evt_test-001",
        timestamp=datetime(2026, 2, 22, 14, 30, 0, tzinfo=timezone.utc),
        source={"platform": "jira", "raw_event_type": "issue_assigned"},
        actor={"name": "Sarah Chen", "email": "sarah@company.com"},
        subject={"type": "ticket", "id": "BUG-1234", "title": "NPE in PaymentService"},
        content={"body": "NullPointerException", "attachments": [], "metadata": {}},
    )


@pytest.fixture
def bot_event() -> LayaEvent:
    """An event from a bot actor."""
    return LayaEvent(
        event_id="evt_test-002",
        timestamp=datetime(2026, 2, 22, 14, 31, 0, tzinfo=timezone.utc),
        source={"platform": "jira", "raw_event_type": "issue_updated"},
        actor={"name": "CI Bot", "email": "ci-bot@company.com"},
        subject={"type": "ticket", "id": "BUG-1234", "title": "NPE"},
        content={"body": "Build changed", "attachments": [], "metadata": {}},
    )


@pytest.fixture
def slack_event() -> LayaEvent:
    """A Slack event with metadata."""
    return LayaEvent(
        event_id="evt_test-003",
        timestamp=datetime(2026, 2, 22, 14, 32, 0, tzinfo=timezone.utc),
        source={"platform": "slack", "raw_event_type": "message_received"},
        actor={"name": "Mike", "email": "mike@company.com"},
        subject={"type": "thread", "id": "thread-random", "title": "Hey everyone"},
        content={
            "body": "Hey everyone!",
            "attachments": [],
            "metadata": {"slack_channel": "random", "slack_channel_type": "public"},
        },
    )


@pytest.fixture
def sample_team() -> dict:
    """Team config with 3 members."""
    return {
        "members": [
            {"name": "Sarah Chen", "email": "sarah@company.com", "role": "teammate", "notes": "Backend"},
            {"name": "Mike Torres", "email": "mike@company.com", "role": "manager", "notes": "EM"},
            {"name": "CI Bot", "email": "ci@company.com", "role": "bot", "notes": "Jenkins"},
        ]
    }


@pytest.fixture
def sample_rules() -> dict:
    """Rules config with bot filter and channel filter."""
    return {
        "rules": [
            {
                "name": "Ignore bot messages",
                "enabled": True,
                "condition": {"field": "actor.email", "operator": "contains", "value": "bot"},
                "action": "drop",
            },
            {
                "name": "Mute #random",
                "enabled": True,
                "condition": {
                    "all": [
                        {"field": "source.platform", "operator": "equals", "value": "slack"},
                        {"field": "content.metadata.slack_channel", "operator": "equals", "value": "random"},
                    ]
                },
                "action": "drop",
            },
        ]
    }


@pytest.fixture
def mock_team(sample_team):
    """Patch load_team to return sample_team."""
    with patch("laya.pipeline.ingest.load_team", return_value=sample_team):
        with patch("laya.config.load_team", return_value=sample_team):
            yield sample_team


@pytest.fixture
def mock_rules(sample_rules):
    """Patch load_rules to return sample_rules."""
    with patch("laya.pipeline.rules.load_rules", return_value=sample_rules):
        with patch("laya.config.load_rules", return_value=sample_rules):
            yield sample_rules


# --- Router fixtures ---

MOCK_ROUTER_RESPONSE = {
    "category": "CODE",
    "persona": "ENGINEER",
    "priority": "HIGH",
    "confidence": 0.92,
    "entities": [
        {"entity_type": "ticket", "value": "BUG-1234", "platform": "jira"},
        {"entity_type": "file_path", "value": "PaymentService.java", "platform": None},
    ],
    "research_plan": [
        "Check git blame for recent changes to PaymentService.java",
        "Look for similar NPE issues in the project",
        "Review the null-safety of the customer ID parameter",
    ],
    "requires_research": True,
    "secondary_persona": None,
    "reasoning": "Jira bug report about an NPE — needs code investigation.",
}

MOCK_COMMS_RESPONSE = {
    "category": "COMMS",
    "persona": "COMMS",
    "priority": "MEDIUM",
    "confidence": 0.85,
    "entities": [
        {"entity_type": "person", "value": "Mike", "platform": "slack"},
    ],
    "research_plan": [
        "Check recent conversation context in the channel",
        "Look up any related tickets or PRs mentioned",
    ],
    "requires_research": False,
    "secondary_persona": None,
    "reasoning": "Slack message in a public channel — informational.",
}


@pytest.fixture
def mock_chromadb():
    """Patch ChromaDB embed and search functions."""
    with patch("laya.pipeline.emit.embed_document", new_callable=AsyncMock) as mock_embed:
        with patch("laya.pipeline.related_context.memory_search", new_callable=AsyncMock, return_value=[]) as mock_search:
            yield {"embed_document": mock_embed, "memory_search": mock_search}


def _make_mock_llm_response(parsed_dict: dict):
    """Create a mock LiteLLM acompletion response."""
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = json.dumps(parsed_dict)
    mock_response.choices[0].message.tool_calls = None
    mock_response.choices[0].finish_reason = "stop"
    mock_response.usage = MagicMock()
    mock_response.usage.prompt_tokens = 500
    mock_response.usage.completion_tokens = 200
    return mock_response


@pytest.fixture
def mock_llm_router():
    """Patch litellm.acompletion to return a router classification."""
    mock_resp = _make_mock_llm_response(MOCK_ROUTER_RESPONSE)
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp) as mock:
        with patch("laya.llm.client.load_settings", return_value={"models": {"router": "claude-haiku-4-5-20251001"}}):
            with patch("laya.pipeline.queue.get_model_timeout", return_value=120):
                with patch("laya.pipeline.queue.get_llm_retries", return_value=1):
                    yield mock


@pytest.fixture
def sample_router_output_engineer() -> RouterOutput:
    """RouterOutput for ENGINEER persona with requires_research=True."""
    return RouterOutput(**MOCK_ROUTER_RESPONSE)


@pytest.fixture
def sample_router_output_comms() -> RouterOutput:
    """RouterOutput for COMMS persona with requires_research=False."""
    return RouterOutput(**MOCK_COMMS_RESPONSE)


@pytest.fixture
def mock_repos():
    """Patch load_repos to return sample repo config."""
    repos = {"repos": [{"name": "payments-service", "path": "/tmp/test-repo", "platform": "github", "remote_id": "org/payments-service"}]}
    with patch("laya.workers.engineer.load_repos", return_value=repos):
        with patch("laya.config.load_repos", return_value=repos):
            yield repos


@pytest.fixture
def mock_session_manager():
    """Patch session_manager functions for worker tests."""
    mock_agent = MagicMock()
    mock_agent.stream_events = MagicMock(return_value=_empty_async_iter())
    mock_agent.get_status = MagicMock(return_value=MagicMock(value="completed"))

    with patch("laya.agents.session_manager.start_session", new_callable=AsyncMock, return_value=("sess_test123", mock_agent)) as mock_start:
        with patch("laya.agents.session_manager.complete_session", new_callable=AsyncMock) as mock_complete:
            with patch("laya.agents.session_manager.store_workspace_event", new_callable=AsyncMock) as mock_store:
                yield {
                    "start_session": mock_start,
                    "complete_session": mock_complete,
                    "store_workspace_event": mock_store,
                    "agent": mock_agent,
                }


async def _empty_async_iter():
    """Empty async iterator for mocking stream_events."""
    return
    yield  # noqa: unreachable — makes this an async generator


@pytest.fixture
def mock_llm_comms():
    """Patch litellm.acompletion to return a comms classification."""
    mock_resp = _make_mock_llm_response(MOCK_COMMS_RESPONSE)
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp) as mock:
        with patch("laya.llm.client.load_settings", return_value={"models": {"router": "claude-haiku-4-5-20251001"}}):
            with patch("laya.pipeline.queue.get_model_timeout", return_value=120):
                with patch("laya.pipeline.queue.get_llm_retries", return_value=1):
                    yield mock


# --- Stager / Emit fixtures ---

MOCK_STAGER_RESPONSE = {
    "header": "Fix NPE in PaymentService.processPayment()",
    "summary": "A NullPointerException was found in PaymentService.java when processing payments with null customer IDs. The ENGINEER worker has identified the root cause and prepared a fix.",
    "intelligence_report": [
        "NPE occurs at line 42 of PaymentService.java in processPayment() method",
        "Root cause: customer ID not validated before calling getCustomerProfile()",
        "Similar bug was fixed in OrderService.java 2 weeks ago (BUG-1198)",
        "3 tests in PaymentServiceTest.java need updating for null-safety",
        "No other callers pass null customer IDs in production code",
    ],
    "staged_output": {
        "type": "code_fix",
        "content": "Add null check for customerId in PaymentService.processPayment() before line 42.",
    },
    "suggested_actions": [
        {
            "action_id": "act_comment_jira",
            "label": "Post Jira Comment",
            "action_type": "comment",
            "target_platform": "jira",
            "payload": "{\"body\": \"Investigation complete. NPE caused by null customer ID.\"}",
        },
        {
            "action_id": "act_transition_jira",
            "label": "Move to In Progress",
            "action_type": "transition",
            "target_platform": "jira",
            "payload": "{\"transition_id\": \"21\"}",
        },
    ],
    "privacy_tier": 2,
}


@pytest.fixture
def mock_llm_stager():
    """Patch litellm.acompletion to return a stager response."""
    mock_resp = _make_mock_llm_response(MOCK_STAGER_RESPONSE)
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp) as mock:
        with patch("laya.llm.client.load_settings", return_value={"models": {"stager": "claude-sonnet-4-5-20250929"}}):
            with patch("laya.pipeline.queue.get_model_timeout", return_value=120):
                with patch("laya.pipeline.queue.get_llm_retries", return_value=1):
                    yield mock


@pytest.fixture
def sample_worker_result():
    """A sample ENGINEER WorkerResult with findings and session_id."""
    from laya.workers.base import WorkerResult

    return WorkerResult(
        persona="ENGINEER",
        findings={
            "root_cause": "Null customer ID in processPayment()",
            "affected_file": "PaymentService.java",
            "line_number": 42,
        },
        drafted_output={
            "task_prompt": "Fix the NPE by adding null check",
            "target_files": ["PaymentService.java"],
        },
        session_id="sess_test_eng",
    )


@pytest.fixture
def sample_worker_result_no_session():
    """A sample COMMS WorkerResult without a coding session."""
    from laya.workers.base import WorkerResult

    return WorkerResult(
        persona="COMMS",
        findings={
            "context": "Slack discussion about design review",
            "key_points": ["Need review by Friday", "Focus on API changes"],
        },
        drafted_output={
            "draft_reply": "I'll review the API changes by Friday.",
        },
        session_id=None,
    )


# --- Helper: insert test event ---

async def insert_test_event(db, event_id="evt_test", platform="jira",
                            raw_event_type="issue_assigned",
                            subject_type="ticket", subject_id="BUG-1234",
                            subject_title="NPE in PaymentService",
                            actor_name="Sarah", actor_email="sarah@company.com",
                            content_body="NullPointerException",
                            space_id=None):
    """Insert a test event row."""
    await db.execute(
        """INSERT INTO events
           (event_id, timestamp, source_platform, source_raw_event_type,
            subject_type, subject_id, subject_title, actor_name, actor_email,
            content_body, raw_json, processed, filtered, space_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (event_id, "2026-02-22T14:30:00Z", platform, raw_event_type,
         subject_type, subject_id, subject_title, actor_name, actor_email,
         content_body, "{}", True, False, space_id),
    )
    await db.commit()


async def insert_test_card(db, card_id="card_test", event_id="evt_test",
                           priority="HIGH", persona="ENGINEER", category="CODE",
                           status="pending", header="Test Card Header",
                           summary="Test summary", space_id=None,
                           entity_id=None, actions=None):
    """Insert a card with its parent event for testing."""
    # Ensure parent event exists
    existing = await db.execute_fetchall(
        "SELECT event_id FROM events WHERE event_id = ?", (event_id,)
    )
    if not existing:
        await insert_test_event(db, event_id, space_id=space_id)

    intelligence = json.dumps(["Finding 1", "Finding 2"])
    staged_output = json.dumps({"type": "code_fix", "content": "Add null check"})
    if actions is not None:
        suggested_actions = json.dumps(actions)
    else:
        suggested_actions = json.dumps([
            {"action_id": "act_1", "label": "Post Comment", "action_type": "comment",
             "target_platform": "jira", "payload": {"body": "Fix found"}}
        ])
    await db.execute(
        """INSERT INTO action_cards
           (card_id, event_id, priority, persona, category, header, summary,
            intelligence, staged_output, suggested_actions, status, privacy_tier,
            entity_id, space_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (card_id, event_id, priority, persona, category, header, summary,
         intelligence, staged_output, suggested_actions, status, 2,
         entity_id or f"jira:ticket:BUG-1234", space_id),
    )
    await db.commit()
