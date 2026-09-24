# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Tool call references are persisted with a message and returned with it.

A chat turn's tool calls were already written to `chat_messages.tool_calls_json`, but never
sent back: the final `chat_stream_done` message omitted them and `ChatMessage` had no field
for them, so the only record of what ran was a transient "Looking up: …" indicator that
disappears the moment the last tool finishes. These tests pin the round trip.

The live streaming loop that executes tools has no harness in this suite (nothing drives the
chat tool loop), so what is pinned here is the shared derivation both paths use and the load
path that returns it. The wiring itself is `tool_call_names(...)` at three call sites.

That derivation is deliberately one function in `laya/pipeline/chat.py`, called by the
pipeline with the in-memory log and by the history loaders with the stored JSON text. It used
to be two: the pipeline skipped an entry that was not a dict while the loader read it as a
bare name, so for a legacy bare-string `tool_calls_json` the live turn and a reload disagreed
about which tools produced a message. The bare-name reading won — see
`test_a_bare_string_entry_is_read_as_a_name` — because skipping it would silently drop the
very references this feature exists to surface, and the pipeline never writes a bare string,
so the tolerant reading cannot change what a live turn records.
"""

import json
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from laya.pipeline.chat import tool_call_names
from tests.conftest import insert_test_conversation


class TestToolCallNames:
    """The derivation shared by the persisted row, the stream payload, and the loader."""

    def test_names_in_first_seen_order(self):
        log = [{"name": "search_cards", "args": {}}, {"name": "get_event", "args": {}}]
        assert tool_call_names(log) == ["search_cards", "get_event"]

    def test_a_tool_called_twice_is_named_once(self):
        log = [{"name": "search_cards"}, {"name": "search_cards"}, {"name": "get_event"}]
        assert tool_call_names(log) == ["search_cards", "get_event"]

    def test_an_empty_log_yields_no_names(self):
        assert tool_call_names([]) == []

    def test_entries_without_a_usable_name_are_skipped(self):
        log = [{"args": {}}, {"name": None}, {"name": ""}, {"name": 7}, {"name": "ok"}]
        assert tool_call_names(log) == ["ok"]

    def test_a_bare_string_entry_is_read_as_a_name(self):
        """An entry that is not a dict is a bare name, not a malformed one.

        This is the semantics the two copies disagreed on: the pipeline's copy skipped a
        non-dict entry, the loader's read it as a name. Reading it as a name is the one that
        can be shared without losing information — an older `tool_calls_json` may be a plain
        list of names, and the pipeline never writes one, so nothing a live turn records
        changes. It still does not raise on a genuinely unusable entry.
        """
        assert tool_call_names(["search_cards", {"name": "get_event"}]) == [
            "search_cards", "get_event"]
        assert tool_call_names([1, None, "search_cards"]) == ["search_cards"]


class TestHistoryTolerance:
    """Loading history must not fall over on a row written by an older shape."""

    def test_null_and_empty_are_empty(self):
        assert tool_call_names(None) == []
        assert tool_call_names("") == []

    def test_unparseable_and_unexpected_shapes_are_empty(self):
        assert tool_call_names("not json") == []
        assert tool_call_names('{"name": "search_cards"}') == []  # a dict, not a list
        assert tool_call_names("[1, 2]") == []

    def test_the_persisted_shape_parses(self):
        raw = json.dumps([{"name": "search_cards", "args": {"q": "x"}},
                          {"name": "get_event", "args": {}}])
        assert tool_call_names(raw) == ["search_cards", "get_event"]

    def test_a_bare_list_of_strings_parses_too(self):
        """An older or hand-written value should still render rather than disappear."""
        assert tool_call_names('["search_cards", "get_event", "search_cards"]') == [
            "search_cards", "get_event"]


@pytest.mark.asyncio
class TestMessageLoadsCarryToolCalls:
    async def test_conversation_messages_return_the_tool_names(self, db):
        """The load the UI does when a conversation is reopened."""
        from laya.main import app

        conv_id = await insert_test_conversation(db, "conv_tools")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for message_id, role, content, tool_calls in (
            ("msg_user", "user", "which tickets?", None),
            ("msg_a", "assistant", "three of them",
             json.dumps([{"name": "search_cards"}, {"name": "search_cards"},
                         {"name": "get_event"}])),
        ):
            await db.execute(
                """INSERT INTO chat_messages
                   (message_id, timestamp, role, content, referenced_cards,
                    referenced_events, conversation_id, tool_calls_json)
                   VALUES (?, ?, ?, ?, '[]', '[]', ?, ?)""",
                (message_id, now, role, content, conv_id, tool_calls),
            )
        await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/chat/conversations/{conv_id}/messages")

        assert resp.status_code == 200
        payload = resp.json()
        messages = payload["messages"] if isinstance(payload, dict) else payload
        by_id = {m["message_id"]: m for m in messages}

        assert by_id["msg_a"]["tool_calls"] == ["search_cards", "get_event"]
        assert by_id["msg_user"]["tool_calls"] == []  # a user turn never has any

    async def test_a_row_with_no_tool_calls_still_loads(self, db):
        """The column is nullable: a message written before it existed must render."""
        from laya.main import app

        conv_id = await insert_test_conversation(db, "conv_old")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            """INSERT INTO chat_messages
               (message_id, timestamp, role, content, referenced_cards,
                referenced_events, conversation_id, tool_calls_json)
               VALUES ('msg_old', ?, 'assistant', 'old reply', '[]', '[]', ?, NULL)""",
            (now, conv_id),
        )
        await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/chat/conversations/{conv_id}/messages")

        assert resp.status_code == 200
        payload = resp.json()
        messages = payload["messages"] if isinstance(payload, dict) else payload
        assert messages[0]["tool_calls"] == []
