"""Tests for tools/search.py."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from src.zulipchat_mcp.tools.search import (
    AmbiguousUserError,
    UserNotFoundError,
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
        now = datetime.now()

        # Target window: 5 messages, 80-100 hours old - older than
        # before_time (72h ago) but within the last_hours cutoff (168h).
        target = [
            _message(i, (now - timedelta(hours=100 - i * 5)).timestamp())
            for i in range(1, 6)
        ]
        # Noise: 20 messages landing in the last 60 hours - all newer than
        # before_time, and exactly one page's worth (limit=10 -> page_size
        # 20), so a single fetch surfaces only noise.
        noise = [
            _message(100 + k, (now - timedelta(hours=60 - k * 3)).timestamp())
            for k in range(20)
        ]
        all_messages = target + noise  # ascending by id and by timestamp
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
        now = datetime.now()

        target = [
            _message(i, (now - timedelta(hours=100 - i * 5)).timestamp())
            for i in range(1, 6)
        ]
        noise = [
            _message(100 + k, (now - timedelta(hours=60 - k * 3)).timestamp())
            for k in range(20)
        ]
        all_messages = target + noise
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
        the failure is only visible through window_complete being False."""
        now = datetime.now()
        first_page = [
            _message(1, (now - timedelta(minutes=1)).timestamp()),
            _message(2, (now - timedelta(minutes=0)).timestamp()),
        ]
        call_count = 0

        def get_messages_raw(**kwargs: object) -> dict[str, object]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"result": "success", "messages": first_page}
            return {"result": "error", "msg": "Bad request"}

        mock_deps.get_messages_raw.side_effect = get_messages_raw

        result = await search_messages(stream="test-stream", last_hours=1, limit=1)

        assert result["status"] == "success"
        assert result["window_complete"] is False
        assert len(result["messages"]) == 1

    @pytest.mark.asyncio
    async def test_time_filter_cutoff_hits_page_cap_marks_incomplete(self, mock_deps):
        """If a narrow never lets a page fall short of page_size or cross
        the cutoff within the hard page cap, the walk must stop instead of
        looping forever, and must say so via window_complete."""
        now = datetime.now()
        page = [_message(i, (now - timedelta(minutes=i)).timestamp()) for i in range(4)]
        mock_deps.get_messages_raw.return_value = {
            "result": "success",
            "messages": page,
        }

        result = await search_messages(stream="test-stream", last_hours=1, limit=2)

        assert result["status"] == "success"
        assert result["window_complete"] is False
        assert mock_deps.get_messages_raw.call_count == 10

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
