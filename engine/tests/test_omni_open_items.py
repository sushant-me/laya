# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""Open items offered to the resynthesis as attention CANDIDATES.

Attention is carried forward, so a space whose attention section was emptied once cannot
repopulate on its own: the request carries no attention to carry and the `since` window
excludes a backlog months old, so the model returns an empty section for a starved request.
`_fetch_open_items` gives it something to reconsider.

Two properties keep the block honest, and both are pinned here because the block's opening
sentence asserts them:

* it says its entries are "NOT in the snapshot above", so a subject the snapshot already
  accounts for must not be re-offered;
* it says the attention section is empty, so the block must not be emitted at all while the
  previous snapshot still carries attention forward — nor on a first synthesis, where the
  model is already handed every one of those subjects in the new-cards block.

The property that matters most here is the one that keeps this input-only: nothing is
promoted into the output by code, and a run that ignores the block behaves exactly as before
— `test_the_block_is_the_only_difference_in_the_request` is the assertion that pins that, by
diffing the built request with and without the candidates.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from tests.conftest import insert_test_card

SPACE = "default"
OTHER_SPACE = "other"

# Older than every card these tests insert, so `since` (read from the newest snapshot's
# generated_at) does not exclude them from `new_cards`.
BEFORE_THE_CARDS = "2026-01-01 00:00:00"


async def _card(db, card_id, *, entity, priority="HIGH", status="pending", space=SPACE,
                minutes_ago=0, event=None):
    await insert_test_card(
        db, card_id=card_id, event_id=event or f"evt_{card_id}", space_id=space,
        priority=priority, status=status, entity_id=entity, header=f"{card_id} header",
    )
    if minutes_ago:
        ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
            "%Y-%m-%d %H:%M:%S")
        await db.execute("UPDATE action_cards SET created_at = ? WHERE card_id = ?",
                         (ts, card_id))
    await db.commit()


def _sections(attention=None, recent=None):
    """A snapshot body shaped the way the pipeline stores one."""
    return [
        {"type": "attention", "label": None, "items": attention or []},
        {"type": "recent", "label": None, "items": recent or []},
        {"type": "period", "label": None, "items": []},
        {"type": "milestone", "label": None, "items": []},
    ]


async def _snapshot(db, sections, *, space=SPACE, version=1, generated_at=BEFORE_THE_CARDS,
                    card_ids=None):
    """Store a full (non-delta) snapshot — the state `_resynthesize_space` loads."""
    await db.execute(
        """INSERT INTO omni_snapshots
           (snapshot_id, space_id, version, generated_at, snapshot_type, content_json,
            card_ids, events_processed, created_at, is_delta)
           VALUES (?, ?, ?, ?, 'manual', ?, ?, 0, ?, 0)""",
        (f"snap_{space}_{version}", space, version, generated_at,
         json.dumps({"sections": sections}), json.dumps(card_ids or []), generated_at),
    )
    await db.commit()


@pytest.mark.asyncio
class TestFetchOpenItems:
    async def test_only_live_high_and_critical_subjects_in_this_space(self, db):
        from laya.pipeline.omni import _fetch_open_items

        await _card(db, "c_high", entity="e:high")
        await _card(db, "c_critical", entity="e:critical", priority="CRITICAL")
        await _card(db, "c_low", entity="e:low", priority="LOW")
        await _card(db, "c_medium", entity="e:medium", priority="MEDIUM")
        await _card(db, "c_done", entity="e:done", status="done")
        await _card(db, "c_dismissed", entity="e:dismissed", status="dismissed")
        await _card(db, "c_elsewhere", entity="e:other", space=OTHER_SPACE)

        items, total = await _fetch_open_items(db, SPACE)
        entities = {i["entity_id"] for i in items}

        assert entities == {"e:high", "e:critical"}
        assert total == 2

    async def test_one_row_per_entity_showing_its_newest_card(self, db):
        """A subject's newest card is what describes its current state."""
        from laya.pipeline.omni import _fetch_open_items

        await _card(db, "c_old", entity="e:one", minutes_ago=90, event="evt_a")
        await _card(db, "c_new", entity="e:one", minutes_ago=5, event="evt_b")

        items, total = await _fetch_open_items(db, SPACE)

        assert total == 1
        assert len(items) == 1
        assert items[0]["header"] == "c_new header"
        assert items[0]["open_cards"] == 2  # both cards on the subject are still open

    async def test_a_card_with_no_entity_key_is_skipped(self, db):
        """NULL or empty — the same degenerate case `LayaEvent.entity_id` guards against,
        where a blank key would collapse unrelated subjects into one group. Here it would
        offer the model a candidate that names no subject at all.

        `insert_test_card` supplies a default entity id when passed None, so a test that
        passes None is asserting against the fixture's default rather than a blank key —
        the first version of this test did exactly that and was testing nothing. The blank
        keys are written directly.
        """
        from laya.pipeline.omni import _fetch_open_items

        await _card(db, "c_null", entity="e:placeholder")
        await _card(db, "c_empty", entity="e:placeholder")
        await _card(db, "c_with_entity", entity="e:real")
        await db.execute("UPDATE action_cards SET entity_id = NULL WHERE card_id = 'c_null'")
        await db.execute("UPDATE action_cards SET entity_id = '' WHERE card_id = 'c_empty'")
        await db.commit()

        items, total = await _fetch_open_items(db, SPACE)

        assert total == 1
        assert [i["entity_id"] for i in items] == ["e:real"]

    async def test_an_entity_the_snapshot_already_lists_is_not_re_offered(self, db):
        """The block opens with "subjects ... that are NOT in the snapshot above".

        Without the exclusion the request contradicts itself: a subject the snapshot already
        carries is offered back to the model as one the snapshot had lost, which is how an
        item gets duplicated or a de-escalation the model already made gets re-litigated.
        """
        from laya.pipeline.omni import _fetch_open_items

        await _card(db, "c_known", entity="e:known")
        await _card(db, "c_lost", entity="e:lost")

        items, total = await _fetch_open_items(db, SPACE, {"e:known"})

        assert [i["entity_id"] for i in items] == ["e:lost"]
        assert total == 1, "the total counts what is offered, not what was excluded"

    async def test_the_cap_limits_the_rows_but_not_the_total(self, db):
        """The prompt has to be able to say it is showing a window, not everything."""
        from laya.pipeline.omni import _OPEN_ITEMS_CAP, _fetch_open_items

        for i in range(_OPEN_ITEMS_CAP + 5):
            await _card(db, f"c_cap_{i:03d}", entity=f"e:cap:{i:03d}", minutes_ago=i)

        items, total = await _fetch_open_items(db, SPACE)

        assert len(items) == _OPEN_ITEMS_CAP
        assert total == _OPEN_ITEMS_CAP + 5
        # the window is the most recently active, not an arbitrary slice
        assert items[0]["entity_id"] == "e:cap:000"

    async def test_equally_active_subjects_have_a_total_order(self, db):
        """Batch-created cards share a `created_at`, so activity alone is not a total order
        and which of them fills the window could vary run to run. `card_id` breaks the tie.

        The cards here are all stamped to the same second on purpose, so `last_active` is
        identical for all three and only the tiebreaker can order them.
        """
        from laya.pipeline.omni import _fetch_open_items

        for card_id in ("c_b", "c_a", "c_c"):
            await _card(db, card_id, entity=f"e:{card_id}")
        await db.execute(
            "UPDATE action_cards SET created_at = ? WHERE card_id IN ('c_a', 'c_b', 'c_c')",
            (BEFORE_THE_CARDS,),
        )
        await db.commit()

        items, _total = await _fetch_open_items(db, SPACE)

        assert [i["entity_id"] for i in items] == ["e:c_c", "e:c_b", "e:c_a"]
        again, _total2 = await _fetch_open_items(db, SPACE)
        assert [i["entity_id"] for i in again] == [i["entity_id"] for i in items]


@pytest.mark.asyncio
class TestSnapshotEntityIds:
    """What the snapshot already accounts for, which is what the block must not re-offer."""

    async def test_entity_ids_come_from_items_and_from_source_cards(self, db):
        """Two sources, because a stored snapshot may only carry one of them."""
        from laya.pipeline.omni import _fetch_card_meta, _snapshot_entity_ids

        await _card(db, "c_card_only", entity="e:from_cards")
        snapshot = {"sections": _sections(
            attention=[{"text": "named outright", "entity_ids": ["e:from_item"]}],
            recent=[{"text": "only a card id", "source_cards": ["c_card_only"]}],
        )}
        meta = await _fetch_card_meta(db, ["c_card_only"])

        assert _snapshot_entity_ids(snapshot, meta) == {"e:from_item", "e:from_cards"}

    async def test_no_snapshot_accounts_for_nothing(self, db):
        from laya.pipeline.omni import _snapshot_entity_ids

        assert _snapshot_entity_ids(None, {}) == set()


class TestWhenTheBlockIsEmitted:
    """The omission conditions, checked next to the sentence that states them."""

    def _messages(self, snapshot="empty-attention", **kwargs):
        from laya.llm.prompts.omni import build_omni_resynthesis_messages

        snapshots = {
            "none": None,
            "empty-attention": {"sections": _sections()},
            "attention": {"sections": _sections(attention=[
                {"text": "carried forward", "entity_ids": ["e:carried"],
                 "priority": "HIGH"}])},
        }
        base = dict(current_snapshot=snapshots[snapshot], new_cards=[], acted_cards=[],
                    pinned_items=[])
        return build_omni_resynthesis_messages(**{**base, **kwargs})[1]["content"]

    def _items(self, n=2):
        return [{"entity_id": f"e:{i}", "header": f"subject {i}", "priority": "HIGH",
                 "status": "pending", "open_cards": 1, "last_active": "2026-09-24 10:00:00"}
                for i in range(n)]

    def test_no_block_without_candidates(self):
        assert "[OPEN ITEMS" not in self._messages()

    def test_no_block_on_a_first_synthesis(self):
        """There is nothing starved to repair: with no prior snapshot the model is already
        handed every one of these subjects in [NEW CARDS SINCE LAST SYNTHESIS], so the block
        would restate them at the cost of window."""
        assert "[OPEN ITEMS" not in self._messages(
            snapshot="none", open_items=self._items(), open_items_total=2)

    def test_no_block_while_the_snapshot_still_carries_attention(self):
        """Nothing is starved while attention is being carried forward, and re-offering
        subjects the model has already placed is how a de-escalation gets re-litigated."""
        assert "[OPEN ITEMS" not in self._messages(
            snapshot="attention", open_items=self._items(), open_items_total=2)

    def test_the_block_appears_for_a_starved_snapshot(self):
        text = self._messages(open_items=self._items(), open_items_total=2)
        assert "[OPEN ITEMS THAT MAY NEED ATTENTION — CANDIDATES, NOT ATTENTION]" in text
        assert "subject 0" in text and "subject 1" in text

    def test_the_block_says_they_are_candidates_not_attention(self):
        text = self._messages(open_items=self._items(), open_items_total=2)
        assert "do NOT add one merely because it is listed here" in text

    def test_the_framing_matches_what_was_emitted(self):
        """The defect this pins: the prose described a filter the query did not apply and a
        placement the caller did not enforce. Both sentences must now be true of the data the
        block actually carries — no hedge ("usually"), and the empty-attention claim that
        `needs_open_items` guarantees."""
        text = self._messages(open_items=self._items(), open_items_total=2)

        assert "NOT in the snapshot above" in text
        assert "The snapshot's attention section is empty" in text
        assert "usually because" not in text

    def test_the_block_states_whether_it_is_everything_or_a_window(self):
        windowed = self._messages(open_items=self._items(3), open_items_total=57)
        assert "57 open subject(s) in total" in windowed
        assert "the 3 most recently active are listed" in windowed

        whole = self._messages(open_items=self._items(2), open_items_total=2)
        assert "all of them are listed" in whole

    def test_the_block_is_the_only_difference_in_the_request(self):
        """The whole point: this is input. Building the request with candidates must not
        change anything else about it — nothing is injected into the snapshot or the
        sections by code, so a model that ignores the block behaves exactly as before."""
        without = self._messages()
        with_items = self._messages(open_items=self._items(), open_items_total=2)

        assert "[OPEN ITEMS THAT MAY NEED ATTENTION" in with_items
        # Consume the whole block including the newline that introduces it, so the rest of
        # the request can be compared byte for byte with the request built without them.
        stripped = re.sub(r"\n?\[OPEN ITEMS.*?\[END OPEN ITEMS\]\n?", "", with_items, flags=re.S)
        assert stripped == without


@pytest.mark.asyncio
class TestOpenItemsOfferedOnTheFirstFoldOnly:
    def _spy_patches(self):
        """Capture the candidate list each fold is built with, and stub the LLM.

        `seen_items` holds the entity_ids of the `open_items` each fold received, which is
        what a caller can observe of the block without parsing the request.
        """
        from laya.pipeline import omni as omni_pipeline

        seen_items: list[list[str]] = []
        seen_counts: list[tuple[int, int]] = []
        real_build = omni_pipeline.build_omni_resynthesis_messages

        def spy_build(**kwargs):
            items = kwargs.get("open_items") or []
            seen_items.append([i["entity_id"] for i in items])
            seen_counts.append((len(items), kwargs.get("open_items_total")))
            return real_build(**kwargs)

        async def fake_llm(**kwargs):
            class R:
                parsed = {"sections": []}
                truncated = False
                output_tokens = 10
                model = "test"
            return R()

        return seen_items, seen_counts, [
            patch.object(omni_pipeline, "build_omni_resynthesis_messages", new=spy_build),
            patch.object(omni_pipeline, "llm_call", new=fake_llm),
        ]

    async def test_later_chunks_do_not_repeat_the_block(self, db):
        """It is the same candidate list for every fold, and after the first fold the model
        has already decided what belongs in attention — so repeating it costs window for
        nothing. (Same reasoning as the prune hints.)

        The snapshot here is the one the block is for: a prior synthesis left the attention
        section empty. That is what makes the block legitimate on the first fold — without it
        there is no starved section to repair and no block at all.
        """
        from laya.pipeline import omni as omni_pipeline

        omni_pipeline._latest_cache.pop(SPACE, None)
        await _snapshot(db, _sections())
        count = omni_pipeline._RESYNTH_CHUNK_SIZE * 2 + 5  # forces 3 chunks
        for i in range(count):
            await _card(db, f"c_burst_{i:03d}", entity=f"e:burst:{i:03d}",
                        minutes_ago=count - i)

        seen_items, seen_counts, patches = self._spy_patches()
        with patches[0], patches[1]:
            await omni_pipeline._resynthesize_space(
                db, SPACE, density="compact", snapshot_type="manual", event_threshold=50)

        assert len(seen_counts) == 3, f"expected 3 folds, saw {len(seen_counts)}"
        assert seen_counts[0][0] > 0 and seen_counts[0][1] > 0, "first fold carries them"
        assert seen_counts[1] == (0, 0) and seen_counts[2] == (0, 0), "later folds do not"

    async def test_a_snapshot_that_still_carries_attention_gets_no_candidates(self, db):
        """The end-to-end half of the omission condition: the block is not even built."""
        from laya.pipeline import omni as omni_pipeline

        omni_pipeline._latest_cache.pop(SPACE, None)
        await _card(db, "c_carried", entity="e:carried")
        await _snapshot(db, _sections(attention=[
            {"text": "already under attention", "entity_ids": ["e:carried"],
             "priority": "HIGH", "source_cards": ["c_carried"]}]))
        for i in range(3):
            await _card(db, f"c_more_{i}", entity=f"e:more:{i}", minutes_ago=i)

        _seen_items, seen_counts, patches = self._spy_patches()
        with patches[0], patches[1]:
            await omni_pipeline._resynthesize_space(
                db, SPACE, density="compact", snapshot_type="manual", event_threshold=50)

        assert seen_counts[0] == (0, 0), "attention is carried forward; nothing is starved"

    async def test_a_starved_snapshot_still_excludes_what_it_lists_elsewhere(self, db):
        """Attention is empty, so the block is legitimate — but a subject the snapshot lists
        under another section is still "in the snapshot above" and must not be re-offered.

        The snapshot's `recent` item names only a source card, with no `entity_ids`, so the
        exclusion has to resolve the card through live metadata rather than read a field the
        LLM may not have written.
        """
        from laya.pipeline import omni as omni_pipeline

        omni_pipeline._latest_cache.pop(SPACE, None)
        await _card(db, "c_listed", entity="e:listed")
        await _card(db, "c_lost", entity="e:lost")
        await _snapshot(db, _sections(recent=[
            {"text": "the snapshot knows this one", "source_cards": ["c_listed"]}]))

        seen_items, seen_counts, patches = self._spy_patches()
        with patches[0], patches[1]:
            await omni_pipeline._resynthesize_space(
                db, SPACE, density="compact", snapshot_type="manual", event_threshold=50)

        assert seen_counts[0][0] > 0, "a starved snapshot does get candidates"
        assert seen_items[0] == ["e:lost"], (
            "the subject the snapshot already lists must not be offered as lost")
