"""Tests for tools/search.py."""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from src.zulipchat_mcp.tools.search import (
    _MAX_BACKWARD_PAGES,
    _MAX_LIMIT,
    _MIN_LIMIT,
    _WHOLE_WINDOW_PAGE_SIZE,
    AmbiguousUserError,
    UserNotFoundError,
    _walk_messages_for_window,
    advanced_search,
    check_messages_match_narrow,
    construct_narrow,
    resolve_user_identifier,
    search_messages,
)


def _message(msg_id: int, timestamp: float) -> dict[str, Any]:
    return {
        "id": msg_id,
        "sender_full_name": "U",
        "sender_email": "e",
        "timestamp": timestamp,
        "content": f"msg{msg_id}",
        "type": "stream",
        "display_recipient": "test-stream",
        "subject": "topic",
    }


def _paginated_get_messages_raw(
    all_messages: list[dict[str, Any]],
) -> Callable[..., dict[str, object]]:
    """Build a get_messages_raw side_effect serving `all_messages` (must be
    ascending by id/timestamp) as real backward pages, honoring anchor and
    num_before like the real Zulip API does."""

    def get_messages_raw(**kwargs: object) -> dict[str, object]:
        anchor = kwargs["anchor"]
        num_before = cast(int, kwargs["num_before"])
        if anchor == "newest":
            page = all_messages[-num_before:]
        else:
            older = [m for m in all_messages if m["id"] < cast(int, anchor)]
            page = older[-num_before:]
        return {"result": "success", "messages": page}

    return get_messages_raw


def _full_page_in_window(now: datetime, num_before: int) -> list[dict[str, Any]]:
    """A page of exactly `num_before` messages, all landing within a
    typical cutoff window - simulates a backward-walk page that never
    looks "exhausted" (shorter than requested) or crosses the cutoff,
    regardless of how large a page is requested."""
    return [
        _message(i, (now - timedelta(seconds=num_before - i)).timestamp())
        for i in range(num_before)
    ]


def _noise_wall(now: datetime) -> list[dict[str, Any]]:
    """5 target messages 80-100h old, plus 20 noise messages landing in the
    last 60h - one page's worth at limit=10 (page_size=20), so a single
    anchor="newest" fetch surfaces only noise and never reaches the target
    window. Ascending by id and by timestamp, as the real Zulip API returns."""
    target = [
        _message(i, (now - timedelta(hours=100 - i * 5)).timestamp())
        for i in range(1, 6)
    ]
    noise = [
        _message(100 + k, (now - timedelta(hours=60 - k * 3)).timestamp())
        for k in range(20)
    ]
    return target + noise


class TestSearchTools:
    """Tests for search tools."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_users.return_value = {
            "result": "success",
            "members": [
                {"full_name": "Test User", "email": "user@example.com", "user_id": 1},
                {
                    "full_name": "Another User",
                    "email": "another@example.com",
                    "user_id": 2,
                },
                {"full_name": "Test Bot", "email": "bot@example.com", "user_id": 3},
            ],
        }
        client.get_messages_raw.return_value = {
            "result": "success",
            "messages": [],
            "anchor": 100,
        }
        client.get_streams.return_value = {
            "result": "success",
            "streams": [{"name": "general", "description": "General stream"}],
        }
        # For check_messages_match_narrow
        client.client.call_endpoint.return_value = {
            "result": "success",
            "messages": {"1": {}},
        }
        return client

    @pytest.fixture
    def mock_deps(self, mock_client):
        with patch("src.zulipchat_mcp.tools.search.get_client") as mock_get_client:
            mock_get_client.return_value = mock_client
            yield mock_client

    @pytest.mark.asyncio
    async def test_resolve_user_identifier(self, mock_client):
        """Test user resolution logic."""
        # Exact email
        res = await resolve_user_identifier("user@example.com", mock_client)
        assert res["email"] == "user@example.com"

        # Exact name
        res = await resolve_user_identifier("Test User", mock_client)
        assert res["email"] == "user@example.com"

        # Fuzzy name
        res = await resolve_user_identifier("another", mock_client)
        assert res["email"] == "another@example.com"

        # Not found
        with pytest.raises(UserNotFoundError):
            await resolve_user_identifier("nonexistent", mock_client)

        # Ambiguous (mock behavior for similar names)
        mock_client.get_users.return_value["members"].append(
            {"full_name": "Another User 2", "email": "a2@example.com"}
        )
        # "Another" matches both Another User and Another User 2
        # But "Another User" matches exact.
        # Let's try "Another"
        with pytest.raises(AmbiguousUserError):
            await resolve_user_identifier("Another", mock_client)

    @pytest.mark.asyncio
    async def test_search_messages_basic(self, mock_deps):
        """Test basic search."""
        mock_deps.get_messages_raw.return_value = {
            "result": "success",
            "messages": [
                {
                    "id": 1,
                    "sender_full_name": "U",
                    "sender_email": "e",
                    "timestamp": 100,
                    "content": "c",
                    "type": "stream",
                    "display_recipient": "s",
                    "subject": "t",
                }
            ],
        }

        result = await search_messages(query="hello")

        assert result["status"] == "success"
        assert len(result["messages"]) == 1

        # Verify call
        args = mock_deps.get_messages_raw.call_args[1]
        narrow = args["narrow"]
        assert {"operator": "search", "operand": "hello"} in narrow

    @pytest.mark.parametrize(
        "limit",
        [_MIN_LIMIT - 1, -5, _MAX_LIMIT + 1],
        ids=["zero", "negative", "too_high"],
    )
    @pytest.mark.asyncio
    async def test_search_messages_rejects_out_of_range_limit(self, mock_deps, limit):
        """An out-of-range `limit` must be rejected before any upstream
        fetch happens - not silently drive a huge backward walk (too high),
        and not silently return the whole fetched page for limit<=0, since
        messages[-0:] returns everything, not nothing."""
        result = await search_messages(stream="test-stream", limit=limit)

        assert result["status"] == "error"
        assert result["error"]["code"] == "INVALID_LIMIT"
        assert mock_deps.get_messages_raw.call_count == 0

    @pytest.mark.asyncio
    async def test_search_messages_accepts_boundary_limits(self, mock_deps):
        """The boundary values themselves must still succeed (not an
        off-by-one rejection)."""
        mock_deps.get_messages_raw.return_value = {
            "result": "success",
            "messages": [],
            "anchor": 1,
        }

        for boundary in (_MIN_LIMIT, _MAX_LIMIT):
            result = await search_messages(stream="test-stream", limit=boundary)
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_search_messages_runs_direct_fetch_off_event_loop(self, mock_deps):
        """The single-fetch path (no time filter) must dispatch the
        blocking client.get_messages_raw call through asyncio.to_thread
        instead of calling it inline on the event loop."""
        mock_deps.get_messages_raw.return_value = {
            "result": "success",
            "messages": [],
            "anchor": 1,
        }

        with patch("asyncio.to_thread", wraps=asyncio.to_thread) as mock_to_thread:
            result = await search_messages(stream="test-stream")

        assert result["status"] == "success"
        assert any(
            call.args and call.args[0] is mock_deps.get_messages_raw
            for call in mock_to_thread.call_args_list
        )

    @pytest.mark.asyncio
    async def test_search_messages_runs_window_walk_off_event_loop(self, mock_deps):
        """The time-filtered backward-walk path must dispatch
        _walk_messages_for_window through asyncio.to_thread instead of
        running its (possibly many) blocking calls inline."""
        mock_deps.get_messages_raw.return_value = {
            "result": "success",
            "messages": [],
            "anchor": 1,
        }

        with patch("asyncio.to_thread", wraps=asyncio.to_thread) as mock_to_thread:
            result = await search_messages(stream="test-stream", last_hours=1)

        assert result["status"] == "success"
        assert any(
            call.args and call.args[0] is _walk_messages_for_window
            for call in mock_to_thread.call_args_list
        )

    @pytest.mark.asyncio
    async def test_resolve_user_identifier_runs_get_users_off_event_loop(
        self, mock_client
    ):
        """resolve_user_identifier's client.get_users() calls (the
        exact-email check and the fuzzy-match fetch) are blocking network
        calls and must be dispatched through asyncio.to_thread, same as
        search_messages's fetch calls. Uses an "@" identifier with no exact
        match so both call sites run, and requires every get_users call to
        be routed - not just the first one found."""
        with patch("asyncio.to_thread", wraps=asyncio.to_thread) as mock_to_thread:
            with pytest.raises(UserNotFoundError):
                await resolve_user_identifier("nobody@example.com", mock_client)

        routed = sum(
            1
            for call in mock_to_thread.call_args_list
            if call.args and call.args[0] is mock_client.get_users
        )
        assert routed == mock_client.get_users.call_count >= 1

    @pytest.mark.asyncio
    async def test_time_filter_post_fetch(self, mock_deps):
        """Test that time filtering happens after fetch (Bug Regression).

        When no narrow filter is provided, the fallback strategy uses
        anchor='newest' to avoid Zulip server timeouts, then filters
        client-side by timestamp.
        """
        now = datetime.now()
        ts_now = now.timestamp()
        ts_old = (now - timedelta(hours=2)).timestamp()

        mock_deps.get_messages_raw.return_value = {
            "result": "success",
            "messages": [
                {
                    "id": 1,
                    "sender_full_name": "U",
                    "sender_email": "e",
                    "timestamp": ts_now,
                    "content": "recent",
                    "type": "s",
                },
                {
                    "id": 2,
                    "sender_full_name": "U",
                    "sender_email": "e",
                    "timestamp": ts_old,
                    "content": "old",
                    "type": "s",
                },
            ],
        }

        # Search last 1 hour (no narrow = fallback to anchor="newest")
        result = await search_messages(last_hours=1)

        assert result["status"] == "success"
        assert len(result["messages"]) == 1
        assert result["messages"][0]["content"] == "recent"

        # Without a narrow, fallback uses anchor="newest" (avoids server timeout)
        args = mock_deps.get_messages_raw.call_args[1]
        assert args["anchor"] == "newest"

    @pytest.mark.asyncio
    async def test_time_filter_with_stream_avoids_anchor_date(self, mock_deps):
        """A stream narrow + time filter must not use anchor="date".

        anchor="date" needs feature level 445 (Zulip 12.0+). A server older
        than that rejects it with "Invalid anchor" on every call, so
        search_messages must position via anchor="newest" and filter by
        timestamp client-side instead, same as the no-narrow path - and the
        filter must actually drop messages outside the window.
        """
        now = datetime.now()
        ts_now = now.timestamp()
        ts_old = (now - timedelta(hours=5)).timestamp()

        def message(msg_id: int, timestamp: float, content: str) -> dict[str, Any]:
            return {
                "id": msg_id,
                "sender_full_name": "U",
                "sender_email": "e",
                "timestamp": timestamp,
                "content": content,
                "type": "stream",
                "display_recipient": "test-stream",
                "subject": "topic",
            }

        def get_messages_raw(**kwargs: object) -> dict[str, object]:
            if kwargs.get("anchor") == "date":
                # Mirrors the real Zulip server's response on a version that
                # predates feature level 445.
                return {"result": "error", "msg": "Invalid anchor"}
            return {
                "result": "success",
                "messages": [
                    message(1, ts_old, "old"),
                    message(2, ts_now, "recent"),
                ],
            }

        mock_deps.get_messages_raw.side_effect = get_messages_raw

        result = await search_messages(stream="test-stream", last_hours=1)

        assert result["status"] == "success"
        assert len(result["messages"]) == 1
        assert result["messages"][0]["content"] == "recent"

        args = mock_deps.get_messages_raw.call_args[1]
        assert args["anchor"] == "newest"

    @pytest.mark.asyncio
    async def test_time_filter_with_stream_trims_to_limit(self, mock_deps):
        """The limit*2 over-fetch (to compensate for cutoff filtering) must
        be trimmed back down to `limit` after filtering, and a window that
        fits in one page must not trigger a second fetch."""
        now = datetime.now()
        # Only 3 messages exist in total - fewer than page_size (limit*2=4),
        # so the first page must come back short and end the walk.
        all_messages = [
            _message(i, (now - timedelta(minutes=2 - i)).timestamp()) for i in range(3)
        ]
        mock_deps.get_messages_raw.side_effect = _paginated_get_messages_raw(
            all_messages
        )

        result = await search_messages(stream="test-stream", last_hours=1, limit=2)

        assert result["status"] == "success"
        assert len(result["messages"]) == 2
        assert result["window_complete"] is True
        assert mock_deps.get_messages_raw.call_count == 1

    @pytest.mark.asyncio
    async def test_time_filter_with_before_time_walks_past_recent_noise(
        self, mock_deps
    ):
        """last_hours combined with before_time must not lose messages that
        sit behind a wall of more-recent, non-matching traffic.

        A single anchor="newest" fetch always re-anchors at "now" and only
        ever reaches the newest `limit * 2` matching messages. If more than
        that many messages have landed since `before_time`, the entire
        target window (older than before_time, newer than the last_hours
        cutoff) is behind that wall and never gets fetched at all - the
        single-page fetch returns zero results even though matching
        messages exist. Walking the anchor backward page by page must reach
        past the noise to find them.
        """
        now: datetime = datetime.now()
        # Target window: 80-100 hours old - older than before_time (72h
        # ago) but within the last_hours cutoff (168h).
        all_messages: list[dict[str, Any]] = _noise_wall(now)
        mock_deps.get_messages_raw.side_effect = _paginated_get_messages_raw(
            all_messages
        )

        result = await search_messages(
            stream="test-stream",
            last_hours=168,
            before_time=(now - timedelta(hours=72)).isoformat(),
            limit=10,
        )

        assert result["status"] == "success"
        ids = sorted(m["id"] for m in result["messages"])
        assert ids == [1, 2, 3, 4, 5]

    @pytest.mark.asyncio
    async def test_before_time_alone_walks_past_recent_noise(self, mock_deps):
        """`before_time` used with no last_hours/last_days/after_time lower
        bound must also walk past more-recent noise, not just return the
        newest page and filter it down to nothing.

        Without a lower bound there's no cutoff timestamp to walk toward,
        so the stopping rule is different: keep walking until at least
        `limit` messages pass the before_time filter (or the narrow's
        history runs out, or the page cap is hit) - same underlying "anchor
        never moves off 'now'" bug as the last_hours+before_time case.
        """
        now: datetime = datetime.now()
        all_messages: list[dict[str, Any]] = _noise_wall(now)
        mock_deps.get_messages_raw.side_effect = _paginated_get_messages_raw(
            all_messages
        )

        result = await search_messages(
            stream="test-stream",
            before_time=(now - timedelta(hours=72)).isoformat(),
            limit=5,
        )

        assert result["status"] == "success"
        ids = sorted(m["id"] for m in result["messages"])
        assert ids == [1, 2, 3, 4, 5]

    @pytest.mark.asyncio
    async def test_both_bounds_stop_early_on_enough_matches_not_history(
        self, mock_deps
    ):
        """With both a lower and upper time bound set, and more in-window
        matches than `limit`, the walk must stop as soon as it has enough -
        not walk all the way to the cutoff or the narrow's history - and
        the trim afterward must keep the newest `limit` of those matches,
        not just whichever `limit` happened to be collected first.
        """
        now: datetime = datetime.now()
        # 100 hourly messages, ids ascending with age: id i is (99-i) hours
        # old. after_time=90h ago and before_time=30h ago bound a window of
        # ids 9..69 (61 messages) - far more than limit*2 per page.
        all_messages: list[dict[str, Any]] = [
            _message(i, (now - timedelta(hours=99 - i)).timestamp()) for i in range(100)
        ]
        mock_deps.get_messages_raw.side_effect = _paginated_get_messages_raw(
            all_messages
        )

        result = await search_messages(
            stream="test-stream",
            after_time=(now - timedelta(hours=90)).isoformat(),
            before_time=(now - timedelta(hours=30)).isoformat(),
            limit=5,
        )

        assert result["status"] == "success"
        assert result["window_complete"] is True
        # Stopped on "enough matches", not by exhausting the narrow's
        # history (100 messages at page_size=10 would take 10 calls).
        assert mock_deps.get_messages_raw.call_count < 10
        ids = sorted(m["id"] for m in result["messages"])
        assert ids == [65, 66, 67, 68, 69]

    @pytest.mark.asyncio
    async def test_cutoff_only_search_needs_single_page_when_it_covers_limit(
        self, mock_deps
    ):
        """A plain cutoff (last_hours/last_days/after_time, no before_time)
        with sort_by="newest"/"relevance" must not walk multiple pages just
        because the narrow's total history exceeds one page.

        The window's upper edge is always "now", so whenever the window
        holds more than `limit` matches, the single newest page already
        contains the top `limit` of them - walking further would only
        re-fetch older messages that get trimmed away anyway.
        """
        now: datetime = datetime.now()
        # 1000 messages, all within the last few minutes - comfortably
        # inside a 7-day cutoff, so nothing ever "crosses" it. Far more
        # than one page's worth (limit*2 with the default limit=50).
        all_messages: list[dict[str, Any]] = [
            _message(i, (now - timedelta(minutes=1000 - i)).timestamp())
            for i in range(1000)
        ]
        mock_deps.get_messages_raw.side_effect = _paginated_get_messages_raw(
            all_messages
        )

        result = await search_messages(stream="test-stream", last_days=7)

        assert result["status"] == "success"
        assert result["window_complete"] is True
        assert mock_deps.get_messages_raw.call_count == 1
        ids = sorted(m["id"] for m in result["messages"])
        assert ids == list(range(950, 1000))

    @pytest.mark.asyncio
    async def test_time_filter_oldest_sort_whole_window_page_size_independent_of_limit(
        self, mock_deps
    ):
        """A small `limit` with sort_by="oldest" must not force the
        whole-window walk into limit*2-sized pages - doing so risks
        exhausting _MAX_BACKWARD_PAGES before the window closes, when a
        single larger page would have covered it in one round-trip."""
        now: datetime = datetime.now()
        all_messages: list[dict[str, Any]] = [
            _message(i, (now - timedelta(minutes=50 - i)).timestamp())
            for i in range(50)
        ]
        mock_deps.get_messages_raw.side_effect = _paginated_get_messages_raw(
            all_messages
        )

        result = await search_messages(
            stream="test-stream", last_hours=1, limit=2, sort_by="oldest"
        )

        assert result["status"] == "success"
        assert result["window_complete"] is True
        assert mock_deps.get_messages_raw.call_count == 1

    @pytest.mark.asyncio
    async def test_time_filter_oldest_sort_uses_limit_times_two_above_floor(
        self, mock_deps
    ):
        """When `limit * 2` already exceeds the whole-window page-size
        floor, the walk's page size must be `limit * 2`, not the floor -
        pins that it's `max(limit * 2, floor)`, not a fixed page size."""
        now: datetime = datetime.now()
        all_messages: list[dict[str, Any]] = [
            _message(0, (now - timedelta(seconds=1)).timestamp())
        ]
        mock_deps.get_messages_raw.side_effect = _paginated_get_messages_raw(
            all_messages
        )

        result = await search_messages(
            stream="test-stream", last_hours=1, limit=_MAX_LIMIT, sort_by="oldest"
        )

        assert result["status"] == "success"
        assert mock_deps.get_messages_raw.call_args.kwargs["num_before"] == (
            _MAX_LIMIT * 2
        )

    @pytest.mark.asyncio
    async def test_time_filter_oldest_sort_spans_pages(self, mock_deps):
        """sort_by="oldest" with a cutoff spanning more than one page must
        return the narrow's true oldest-in-window messages, not just the
        oldest message from the single newest page.

        Unlike "newest"/"relevance" sort, "oldest" can't stop early once it
        has `limit` matches - the earliest messages collected while
        walking backward are the most recent ones, not the oldest - so it
        must walk until the cutoff is reached or the narrow's history
        runs out. Uses more than _WHOLE_WINDOW_PAGE_SIZE messages so a
        single page (sized independently of `limit` - see the whole-window
        page-size test) still can't cover the whole window in one
        round-trip.
        """
        now: datetime = datetime.now()
        message_count: int = _WHOLE_WINDOW_PAGE_SIZE + 5
        all_messages: list[dict[str, Any]] = [
            _message(i, (now - timedelta(seconds=message_count - i)).timestamp())
            for i in range(message_count)
        ]
        mock_deps.get_messages_raw.side_effect = _paginated_get_messages_raw(
            all_messages
        )

        result = await search_messages(
            stream="test-stream", last_hours=1, limit=2, sort_by="oldest"
        )

        assert result["status"] == "success"
        assert mock_deps.get_messages_raw.call_count > 1
        ids = sorted(m["id"] for m in result["messages"])
        assert ids == [0, 1]

    @pytest.mark.asyncio
    async def test_time_filter_cutoff_first_page_error_passthrough(self, mock_deps):
        """A failing first page on the cutoff-walk path must surface as an
        error, not be swallowed as an empty success."""
        mock_deps.get_messages_raw.return_value = {
            "result": "error",
            "msg": "Bad request",
        }

        result = await search_messages(stream="test-stream", last_hours=1)

        assert result["status"] == "error"
        assert result["error"] == "Bad request"
        assert mock_deps.get_messages_raw.call_count == 1

    @pytest.mark.asyncio
    async def test_time_filter_cutoff_later_page_error_returns_partial_results(
        self, mock_deps
    ):
        """If a later page fails after an earlier page already returned
        data, the walk keeps what it already has instead of discarding it -
        the failure is only visible through window_complete being False.

        Uses sort_by="oldest" so the walk can't stop early just because the
        first page already has enough matches for `limit` - it must always
        attempt a second page here, which is what fails. The first page
        returns exactly the requested page_size worth of messages (all
        within the cutoff window) so it doesn't look "exhausted" - the
        whole-window walk's page_size is decoupled from `limit` (see the
        whole-window page-size test), so this can't be a small fixed page
        anymore.
        """
        now: datetime = datetime.now()
        call_count: int = 0

        def get_messages_raw(**kwargs: object) -> dict[str, object]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                num_before: int = cast(int, kwargs["num_before"])
                page: list[dict[str, Any]] = _full_page_in_window(now, num_before)
                return {"result": "success", "messages": page}
            return {"result": "error", "msg": "Bad request"}

        mock_deps.get_messages_raw.side_effect = get_messages_raw

        result = await search_messages(
            stream="test-stream", last_hours=1, limit=1, sort_by="oldest"
        )

        assert result["status"] == "success"
        assert result["window_complete"] is False
        assert len(result["messages"]) == 1

    @pytest.mark.asyncio
    async def test_time_filter_cutoff_hits_page_cap_marks_incomplete(self, mock_deps):
        """If a narrow never lets a page fall short of page_size or cross
        the cutoff within the hard page cap, the walk must stop instead of
        looping forever, and must say so via window_complete.

        Uses sort_by="oldest" so the walk can't stop early on match count -
        only the cap can end it here. The mock always returns exactly the
        requested page_size worth of messages, all within the cutoff
        window, so it never looks "exhausted" or crosses the cutoff
        regardless of how large a page is requested (the whole-window
        walk's page_size is decoupled from `limit` - see the whole-window
        page-size test - so a small fixed page can no longer force this).
        """
        now: datetime = datetime.now()

        def get_messages_raw(**kwargs: object) -> dict[str, object]:
            num_before: int = cast(int, kwargs["num_before"])
            page: list[dict[str, Any]] = _full_page_in_window(now, num_before)
            return {"result": "success", "messages": page}

        mock_deps.get_messages_raw.side_effect = get_messages_raw

        result = await search_messages(
            stream="test-stream", last_hours=1, limit=2, sort_by="oldest"
        )

        assert result["status"] == "success"
        assert result["window_complete"] is False
        assert mock_deps.get_messages_raw.call_count == _MAX_BACKWARD_PAGES

    @pytest.mark.asyncio
    async def test_time_filter_cutoff_empty_first_page_returns_no_messages(
        self, mock_deps
    ):
        """An empty first page is a genuinely empty result, not an error or
        a sign that messages were missed."""
        mock_deps.get_messages_raw.return_value = {"result": "success", "messages": []}

        result = await search_messages(stream="test-stream", last_hours=1)

        assert result["status"] == "success"
        assert result["messages"] == []
        assert result["window_complete"] is True
        assert mock_deps.get_messages_raw.call_count == 1

    @pytest.mark.asyncio
    async def test_time_filter_oldest_sort_returns_window(self, mock_deps):
        """sort_by="oldest" combined with a time filter must return the
        oldest in-window messages, not an empty result.

        Regression test: fetching via anchor="oldest" with a cutoff set
        would fetch from the narrow's entire history and the cutoff filter
        would discard all of it, since those are the very oldest messages
        ever posted (predating the cutoff).
        """
        now = datetime.now()
        ts_in_window_old = (now - timedelta(minutes=50)).timestamp()
        ts_in_window_new = (now - timedelta(minutes=10)).timestamp()
        ts_outside_window = (now - timedelta(hours=5)).timestamp()

        mock_deps.get_messages_raw.return_value = {
            "result": "success",
            "messages": [
                {
                    "id": 1,
                    "sender_full_name": "U",
                    "sender_email": "e",
                    "timestamp": ts_outside_window,
                    "content": "too old",
                    "type": "stream",
                    "display_recipient": "test-stream",
                    "subject": "topic",
                },
                {
                    "id": 2,
                    "sender_full_name": "U",
                    "sender_email": "e",
                    "timestamp": ts_in_window_old,
                    "content": "oldest in window",
                    "type": "stream",
                    "display_recipient": "test-stream",
                    "subject": "topic",
                },
                {
                    "id": 3,
                    "sender_full_name": "U",
                    "sender_email": "e",
                    "timestamp": ts_in_window_new,
                    "content": "newest in window",
                    "type": "stream",
                    "display_recipient": "test-stream",
                    "subject": "topic",
                },
            ],
        }

        result = await search_messages(
            stream="test-stream", last_hours=1, sort_by="oldest", limit=1
        )

        assert result["status"] == "success"
        assert len(result["messages"]) == 1
        assert result["messages"][0]["content"] == "oldest in window"

    @pytest.mark.asyncio
    async def test_oldest_sort_without_time_filter_uses_anchor_oldest(self, mock_deps):
        """sort_by="oldest" with no time filter fetches via anchor="oldest"
        directly (no cutoff to worry about, so no client-side re-sort)."""
        result = await search_messages(sort_by="oldest", limit=5)

        assert result["status"] == "success"

        args = mock_deps.get_messages_raw.call_args[1]
        assert args["anchor"] == "oldest"
        assert args["num_before"] == 0
        assert args["num_after"] == 5

    @pytest.mark.asyncio
    async def test_search_messages_fuzzy_user(self, mock_deps):
        """Test search with sender name requiring resolution."""
        # 'Test User' resolves to 'user@example.com'
        await search_messages(sender="Test User")

        args = mock_deps.get_messages_raw.call_args[1]
        narrow = args["narrow"]
        # Should be resolved email
        assert {"operator": "sender", "operand": "user@example.com"} in narrow

    @pytest.mark.asyncio
    async def test_advanced_search_aggregations(self, mock_deps):
        """Test advanced search aggregations."""
        mock_deps.get_messages_raw.return_value = {
            "result": "success",
            "messages": [
                {
                    "id": 1,
                    "sender_full_name": "U1",
                    "sender_email": "e1",
                    "timestamp": 100,
                    "content": "c",
                    "type": "stream",
                    "display_recipient": "s1",
                },
                {
                    "id": 2,
                    "sender_full_name": "U1",
                    "sender_email": "e1",
                    "timestamp": 100,
                    "content": "c",
                    "type": "stream",
                    "display_recipient": "s1",
                },
                {
                    "id": 3,
                    "sender_full_name": "U2",
                    "sender_email": "e2",
                    "timestamp": 100,
                    "content": "c",
                    "type": "stream",
                    "display_recipient": "s2",
                },
            ],
        }

        result = await advanced_search(
            query="",
            search_type=["messages"],
            aggregations=["count_by_user", "count_by_stream"],
        )

        assert result["status"] == "success"
        agg = result["results"]["aggregations"]
        assert agg["count_by_user"]["U1"] == 2
        assert agg["count_by_user"]["U2"] == 1
        assert agg["count_by_stream"]["s1"] == 2

    @pytest.mark.asyncio
    async def test_advanced_search_rejects_out_of_range_limit(self, mock_deps):
        """advanced_search must reject an out-of-range `limit` at the top
        level, not report top-level success with the error buried inside
        results["messages"] while users/streams are still sliced with the
        unchecked value."""
        result = await advanced_search(
            query="test", search_type=["messages", "users"], limit=0
        )

        assert result["status"] == "error"
        assert result["error"]["code"] == "INVALID_LIMIT"
        assert mock_deps.get_messages_raw.call_count == 0
        assert mock_deps.get_users.call_count == 0

    @pytest.mark.asyncio
    async def test_advanced_search_runs_get_users_off_event_loop(self, mock_deps):
        """advanced_search's client.get_users() call (search_type=["users"])
        is a blocking network call and must be dispatched through
        asyncio.to_thread instead of running inline on the event loop."""
        with patch("asyncio.to_thread", wraps=asyncio.to_thread) as mock_to_thread:
            result = await advanced_search(query="test", search_type=["users"])

        assert result["status"] == "success"
        assert any(
            call.args and call.args[0] is mock_deps.get_users
            for call in mock_to_thread.call_args_list
        )

    @pytest.mark.asyncio
    async def test_advanced_search_runs_get_streams_off_event_loop(self, mock_deps):
        """advanced_search's client.get_streams() call
        (search_type=["streams"]) is a blocking network call and must be
        dispatched through asyncio.to_thread instead of running inline on
        the event loop."""
        with patch("asyncio.to_thread", wraps=asyncio.to_thread) as mock_to_thread:
            result = await advanced_search(query="test", search_type=["streams"])

        assert result["status"] == "success"
        assert any(
            call.args and call.args[0] is mock_deps.get_streams
            for call in mock_to_thread.call_args_list
        )

    @pytest.mark.asyncio
    async def test_construct_narrow(self):
        """Test narrow construction."""
        result = await construct_narrow(
            stream="general", has_image=True, is_private=False
        )
        assert result["status"] == "success"
        narrow = result["narrow"]

        assert {"operator": "stream", "operand": "general"} in narrow
        assert {"operator": "has", "operand": "image"} in narrow
        assert {"operator": "is", "operand": "private", "negated": True} in narrow

    @pytest.mark.asyncio
    async def test_check_messages_match_narrow(self, mock_deps):
        """Test check_messages_match_narrow."""
        result = await check_messages_match_narrow(
            msg_ids=[1, 2], narrow=[{"operator": "stream", "operand": "general"}]
        )

        assert result["status"] == "success"
        assert result["total_checked"] == 2
        assert result["matching_count"] == 1  # Based on mock return {"1": {}}
        assert result["non_matching_count"] == 1
