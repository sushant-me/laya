# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Terminal-event sibling auto-resolution — the path with no coverage.

`_auto_resolve_terminal_siblings` closes the other cards in an entity when the work item
completes, and it is the mechanism behind an observation in #10 ("'PR #106 opened' stayed
HIGH/ready after its 'merged' sibling arrived — sibling auto-resolution didn't fire"). That
was a root cause elsewhere (a comment's entity key depended on the Pulls fetch window, so
one work item occupied two groups), but nothing here pinned the behaviour either way: the
function had no test at all, and neither did the gate that calls it. These tests pin both,
so the next report of "it didn't fire" can be answered by a failing test rather than by
reading the code.

`transition_card_status` goes through the real single-source-of-truth lifecycle, which
resolves `get_db()` to the fixture's connection, so these exercise the transitions rather
than a stub.
"""

from datetime import datetime, timezone

import pytest

from tests.conftest import insert_test_card

ENTITY = "github:pull_request:org/repo/106"
OTHER_ENTITY = "github:pull_request:org/repo/999"


def _event(raw_event_type: str) -> "object":
    from laya.models.event import LayaEvent

    return LayaEvent(
        event_id="evt_106",
        timestamp=datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc),
        source={"platform": "github", "raw_event_type": raw_event_type},
        actor={"name": "maintainer", "email": ""},
        subject={"type": "pull_request", "id": "org/repo/106", "title": "PR #106"},
        content={"body": "", "attachments": [], "metadata": {}},
    )


async def _status(db, card_id: str) -> str:
    rows = await db.execute_fetchall(
        "SELECT status FROM action_cards WHERE card_id = ?", (card_id,))
    return rows[0]["status"]


class TestTerminalEventClassification:
    """The gate's other half: which event types mean 'this work item is finished'."""

    def test_a_closed_pull_request_is_terminal(self):
        assert _event("pull_request_closed").is_terminal is True

    def test_an_updated_pull_request_is_not_terminal(self):
        assert _event("pull_request_updated").is_terminal is False

    def test_an_issue_comment_is_not_terminal(self):
        assert _event("issue_comment_created").is_terminal is False


@pytest.mark.asyncio
class TestCarryForwardGate:
    """`_count_siblings` decides whether auto-resolution runs at all."""

    async def test_a_second_card_in_the_entity_counts_as_a_sibling(self, db):
        from laya.pipeline.emit import _count_siblings

        await insert_test_card(db, card_id="card_a", event_id="evt_a", entity_id=ENTITY)
        await insert_test_card(db, card_id="card_b", event_id="evt_b", entity_id=ENTITY)
        await db.commit()
        assert await _count_siblings(db, ENTITY, "card_b") == 1

    async def test_a_card_in_another_entity_is_not_a_sibling(self, db):
        """A card existing elsewhere does not make this one a sibling: the count is scoped
        to the entity, which is what makes the entity key load-bearing. (The first version
        of this test asserted the wrong thing — `_count_siblings(ENTITY, card_b)` counts
        cards IN `ENTITY`, so it still saw `card_a` — and running it is what caught that.)"""
        from laya.pipeline.emit import _count_siblings

        await insert_test_card(db, card_id="card_a", event_id="evt_a", entity_id=ENTITY)
        await insert_test_card(db, card_id="card_b", event_id="evt_b", entity_id=OTHER_ENTITY)
        await db.commit()
        assert await _count_siblings(db, ENTITY, "card_a") == 0
        assert await _count_siblings(db, OTHER_ENTITY, "card_b") == 0


@pytest.mark.asyncio
class TestAutoResolveTerminalSiblings:
    async def _seed_pair(self, db, sibling_status: str | None):
        await insert_test_card(db, card_id="card_trigger", event_id="evt_t", entity_id=ENTITY,
                               header="PR #106 merged")
        if sibling_status is not None:
            await insert_test_card(db, card_id="card_sibling", event_id="evt_s", entity_id=ENTITY,
                                   priority="HIGH", status=sibling_status,
                                   header="PR #106 opened")
        await db.commit()

    async def test_a_terminal_event_resolves_a_live_sibling(self, db):
        from laya.pipeline.emit import _auto_resolve_terminal_siblings

        await self._seed_pair(db, sibling_status="ready")
        resolved = await _auto_resolve_terminal_siblings(db, _event("pull_request_closed"),
                                                         ENTITY, "card_trigger")
        assert resolved == 1
        assert await _status(db, "card_sibling") == "done"

    async def test_the_triggering_card_is_never_resolved_by_its_own_event(self, db):
        """It is the card that just arrived; resolving it would close the thing the user
        has not seen yet, and would also make `resolved` count the new card."""
        from laya.pipeline.emit import _auto_resolve_terminal_siblings

        await self._seed_pair(db, sibling_status="ready")
        await _auto_resolve_terminal_siblings(db, _event("pull_request_closed"),
                                              ENTITY, "card_trigger")
        assert await _status(db, "card_trigger") != "done"

    async def test_an_already_inactive_sibling_is_left_alone(self, db):
        """`done`/`dismissed`/`failed`/`archived` end up untouched and uncounted, so the
        number the function returns stays a count of what it actually closed.

        What this does NOT pin is which layer does it, and that is worth stating rather
        than implying: probed directly, `transition_card_status` refuses all four
        (`ValueError: Invalid transition done -> done` and the same for the others), and
        the resolver catches that and skips — so the query's `status NOT IN (inactive)`
        exclusion and the lifecycle guard are redundant for the outcome, the query clause
        only saving a pointless attempt per inactive sibling. A test that asserted "the
        SQL excludes them" would pass with either layer removed.
        """
        from laya.pipeline.emit import _auto_resolve_terminal_siblings

        for status in ("done", "dismissed", "failed", "archived"):
            await self._seed_pair(db, sibling_status=status)
            resolved = await _auto_resolve_terminal_siblings(
                db, _event("pull_request_closed"), ENTITY, "card_trigger")
            assert resolved == 0, f"{status} was re-resolved"
            assert await _status(db, "card_sibling") == status
            await db.execute("DELETE FROM action_cards")
            await db.commit()

    async def test_no_siblings_resolves_nothing(self, db):
        from laya.pipeline.emit import _auto_resolve_terminal_siblings

        await self._seed_pair(db, sibling_status=None)
        assert await _auto_resolve_terminal_siblings(
            db, _event("pull_request_closed"), ENTITY, "card_trigger") == 0

    async def test_a_sibling_in_another_entity_is_untouched(self, db):
        """The pair only see each other through the entity key — which is exactly why a
        work item split across two keys never resolves."""
        from laya.pipeline.emit import _auto_resolve_terminal_siblings

        await self._seed_pair(db, sibling_status=None)
        await insert_test_card(db, card_id="card_elsewhere", event_id="evt_e",
                               entity_id=OTHER_ENTITY, status="ready", header="other PR")
        await db.commit()
        assert await _auto_resolve_terminal_siblings(
            db, _event("pull_request_closed"), ENTITY, "card_trigger") == 0
        assert await _status(db, "card_elsewhere") == "ready"
