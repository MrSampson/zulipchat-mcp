"""Search tools for ZulipChat MCP v0.4.0.

Core search operations: search messages, advanced search, narrow construction.
Analytics moved to ai_analytics.py for LLM elicitation.
"""

from collections import Counter
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Literal, TypedDict, cast

from fastmcp import FastMCP

from ..config import get_client
from ..core.client import ZulipClientWrapper

NarrowOperand = str | int | list[str]


class NarrowFilterBase(TypedDict):
    operator: str
    operand: NarrowOperand


class NarrowFilter(NarrowFilterBase, total=False):
    negated: bool


class AmbiguousUserError(Exception):
    """Raised when user identifier matches multiple users."""

    def __init__(self, identifier: str, matches: list[dict[str, Any]]):
        self.identifier = identifier
        self.matches = matches
        match_strings = [f"{m.get('full_name')} ({m.get('email')})" for m in matches]
        super().__init__(
            f"Multiple matches for '{identifier}': {', '.join(match_strings[:5])}"
        )


class UserNotFoundError(Exception):
    """Raised when user identifier cannot be resolved."""

    def __init__(self, identifier: str):
        self.identifier = identifier
        super().__init__(f"No user matching '{identifier}'")


_MAX_BACKWARD_PAGES = 10


def _in_window(ts: float, cutoff_ts: float | None, before_ts: float | None) -> bool:
    return (cutoff_ts is None or ts >= cutoff_ts) and (
        before_ts is None or ts <= before_ts
    )


def _fetch_messages_until_cutoff(
    client: ZulipClientWrapper,
    narrow: list[dict[str, Any]],
    page_size: int,
    cutoff_ts: float | None = None,
    before_ts: float | None = None,
    min_matches: int | None = 0,
    max_pages: int = _MAX_BACKWARD_PAGES,
) -> dict[str, Any]:
    """Walk backward via message-ID anchor to cover a time-bounded window.

    A single anchor="newest" fetch only ever reaches the newest `page_size`
    messages. If a caller also narrows the top end with `before_time`, and
    more than `page_size` messages have landed since then, the actual
    target window sits entirely behind that wall and a single fetch returns
    nothing - not because the messages don't exist, but because the anchor
    never moves. Walking backward, using each page's oldest message id as
    the next anchor, reaches past that wall.

    Stops as soon as any of these holds: `min_matches` is not None and that
    many collected messages fall within [`cutoff_ts`, `before_ts`] (pass
    `min_matches=None` when the caller needs the whole window regardless of
    count, e.g. sort_by="oldest"); a page's oldest message crosses
    `cutoff_ts` (if set); a page comes back shorter than `page_size` (the
    narrow's history is exhausted); or `max_pages` is hit.

    Returns {"result": "success", "messages": [...], "window_complete":
    bool} - `window_complete` is False when the cap was hit before any of
    the other stop conditions confirmed the result is exact, or when a
    later page failed and only partial results could be returned - or the
    raw error dict from the first failing page.
    """
    anchor: str | int = "newest"
    include_anchor: bool = True
    messages: list[dict[str, Any]] = []
    window_complete: bool = False
    matches_in_window: int = 0

    for _ in range(max_pages):
        result: dict[str, Any] = client.get_messages_raw(
            anchor=anchor,
            narrow=narrow,
            num_before=page_size,
            num_after=0,
            include_anchor=include_anchor,
            client_gravatar=True,
            apply_markdown=True,
        )
        if result.get("result") != "success":
            if not messages:
                return result
            break

        page_messages: list[dict[str, Any]] = result.get("messages", [])
        if not page_messages:
            window_complete = True
            break

        messages = page_messages + messages
        matches_in_window += sum(
            1 for m in page_messages if _in_window(m["timestamp"], cutoff_ts, before_ts)
        )
        oldest: dict[str, Any] = page_messages[0]

        crossed_cutoff: bool = cutoff_ts is not None and oldest["timestamp"] < cutoff_ts
        enough: bool = min_matches is not None and matches_in_window >= min_matches
        if crossed_cutoff or enough:
            window_complete = True
            break
        if len(page_messages) < page_size:
            window_complete = True
            break

        anchor = oldest["id"]
        include_anchor = False

    return {
        "result": "success",
        "messages": messages,
        "window_complete": window_complete,
    }


async def resolve_user_identifier(
    identifier: str, client: ZulipClientWrapper
) -> dict[str, Any]:
    """Resolve partial names, emails, or IDs to full user info."""
    try:
        # Try exact email match first
        if "@" in identifier:
            response = client.get_users()
            if response.get("result") == "success":
                users = response.get("members", [])
                exact_match = next(
                    (user for user in users if user.get("email") == identifier), None
                )
                if exact_match:
                    return exact_match

        # Get all users for fuzzy matching
        response = client.get_users()
        if response.get("result") != "success":
            raise Exception(
                f"Failed to fetch users: {response.get('msg', 'Unknown error')}"
            )

        users = response.get("members", [])

        # Try exact full name match first
        exact_matches = [
            user
            for user in users
            if user.get("full_name", "").lower() == identifier.lower()
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]
        elif len(exact_matches) > 1:
            raise AmbiguousUserError(identifier, exact_matches)

        # Fuzzy matching with similarity scoring
        partial_matches = []
        for user in users:
            full_name = user.get("full_name", "")
            if (
                identifier.lower() in full_name.lower()
                or SequenceMatcher(None, full_name.lower(), identifier.lower()).ratio()
                > 0.6
            ):
                score = SequenceMatcher(
                    None, full_name.lower(), identifier.lower()
                ).ratio()
                partial_matches.append((score, user))

        # Sort by similarity score
        partial_matches.sort(key=lambda x: x[0], reverse=True)

        if not partial_matches:
            raise UserNotFoundError(identifier)
        elif len(partial_matches) == 1:
            return partial_matches[0][1]
        else:
            # Return best match if significantly better than others
            best_score = partial_matches[0][0]
            close_matches = [
                user for score, user in partial_matches if score > best_score - 0.2
            ]
            if len(close_matches) == 1:
                return close_matches[0]
            else:
                raise AmbiguousUserError(identifier, close_matches[:5])

    except (AmbiguousUserError, UserNotFoundError):
        raise
    except Exception as e:
        raise Exception(f"Failed to resolve user '{identifier}': {str(e)}") from e


def build_narrow(
    stream: str | None = None,
    topic: str | None = None,
    sender: str | None = None,
    text: str | None = None,
    has_attachment: bool | None = None,
    has_link: bool | None = None,
    has_image: bool | None = None,
    is_private: bool | None = None,
    is_starred: bool | None = None,
    is_mentioned: bool | None = None,
    last_hours: int | str | None = None,
    last_days: int | str | None = None,
    after_time: datetime | str | None = None,
    before_time: datetime | str | None = None,
) -> list[NarrowFilter]:
    """Build comprehensive narrow filter for Zulip API."""
    narrow: list[NarrowFilter] = []

    # Basic filters
    if stream:
        narrow.append({"operator": "stream", "operand": stream})
    if topic:
        narrow.append({"operator": "topic", "operand": topic})
    if sender:
        narrow.append({"operator": "sender", "operand": sender})
    # Add search operator only for actual search terms
    # Skip if text is empty, "*", or whitespace-only (wildcard = return all)
    if text and text.strip() and text.strip() != "*":
        narrow.append({"operator": "search", "operand": text})

    # Content type filters
    if has_attachment is not None:
        if has_attachment:
            narrow.append({"operator": "has", "operand": "attachment"})
        else:
            narrow.append({"operator": "has", "operand": "attachment", "negated": True})

    if has_link is not None:
        if has_link:
            narrow.append({"operator": "has", "operand": "link"})
        else:
            narrow.append({"operator": "has", "operand": "link", "negated": True})

    if has_image is not None:
        if has_image:
            narrow.append({"operator": "has", "operand": "image"})
        else:
            narrow.append({"operator": "has", "operand": "image", "negated": True})

    # Message state filters
    if is_private is not None:
        if is_private:
            narrow.append({"operator": "is", "operand": "private"})
        else:
            narrow.append({"operator": "is", "operand": "private", "negated": True})

    if is_starred is not None:
        if is_starred:
            narrow.append({"operator": "is", "operand": "starred"})
        else:
            narrow.append({"operator": "is", "operand": "starred", "negated": True})

    if is_mentioned is not None:
        if is_mentioned:
            narrow.append({"operator": "is", "operand": "mentioned"})
        else:
            narrow.append({"operator": "is", "operand": "mentioned", "negated": True})

    # Note: Time filters are NOT added to narrow - Zulip's get_messages API
    # doesn't support search operator with after:/before: syntax.
    # Time filtering is done client-side in search_messages().

    return narrow


async def search_messages(
    # Basic search parameters
    query: str | None = None,
    stream: str | None = None,
    topic: str | None = None,
    sender: str | None = None,
    # Advanced content filters
    has_attachment: bool | None = None,
    has_link: bool | None = None,
    has_image: bool | None = None,
    is_private: bool | None = None,
    is_starred: bool | None = None,
    is_mentioned: bool | None = None,
    # Time filters
    last_hours: int | str | None = None,
    last_days: int | str | None = None,
    after_time: datetime | str | None = None,
    before_time: datetime | str | None = None,
    # Response control
    limit: int = 50,
    sort_by: Literal["newest", "oldest", "relevance"] = "relevance",
) -> dict[str, Any]:
    """Advanced search with fuzzy user resolution and comprehensive filtering."""
    client = get_client()

    try:
        # Resolve sender identifier to email if needed
        resolved_sender = sender
        if sender and "@" not in sender:
            try:
                user_info = await resolve_user_identifier(sender, client)
                resolved_sender = user_info.get("email")
            except AmbiguousUserError as e:
                return {
                    "status": "error",
                    "error": {
                        "code": "AMBIGUOUS_USER",
                        "message": str(e),
                        "suggestions": [
                            f"Did you mean: {m.get('full_name')} ({m.get('email')})?"
                            for m in e.matches[:3]
                        ],
                        "recovery": {
                            "tool": "get_users",
                            "hint": "List users to see all available options",
                        },
                    },
                }
            except UserNotFoundError as e:
                return {
                    "status": "error",
                    "error": {
                        "code": "USER_NOT_FOUND",
                        "message": str(e),
                        "suggestions": [
                            "Use full email address",
                            "Check spelling",
                            "Use get_users to see available users",
                        ],
                        "recovery": {
                            "tool": "get_users",
                            "hint": "Search users to find correct identifier",
                        },
                    },
                }
            except Exception as e:
                return {
                    "status": "error",
                    "error": {
                        "code": "USER_RESOLUTION_FAILED",
                        "message": f"Could not resolve user '{sender}': {str(e)}",
                        "suggestions": [
                            "Use full email address",
                            "Try a different identifier",
                        ],
                    },
                }

        # Build narrow filter (time is filtered client-side after the fetch)
        narrow = build_narrow(
            stream=stream,
            topic=topic,
            sender=resolved_sender,
            text=query,
            has_attachment=has_attachment,
            has_link=has_link,
            has_image=has_image,
            is_private=is_private,
            is_starred=is_starred,
            is_mentioned=is_mentioned,
        )

        # Determine anchor strategy based on time filters and sort
        anchor: str = "newest"
        num_before = limit
        num_after = 0

        # Zulip's anchor="date" + anchor_date would let the server position
        # the anchor at the cutoff directly, but it needs feature level 445
        # (Zulip 12.0+) - a server older than that rejects it outright with
        # "Invalid anchor". Fetch recent messages via anchor="newest" instead
        # and filter by timestamp client-side, which works on any server
        # version.
        cutoff_ts: float | None = None
        before_ts: float | None = None

        if last_hours or last_days or after_time:
            # Calculate cutoff time
            if last_hours:
                hours = int(last_hours) if isinstance(last_hours, str) else last_hours
                cutoff = datetime.now() - timedelta(hours=hours)
            elif last_days:
                days = int(last_days) if isinstance(last_days, str) else last_days
                cutoff = datetime.now() - timedelta(days=days)
            elif after_time:
                cutoff = (
                    after_time
                    if isinstance(after_time, datetime)
                    else datetime.fromisoformat(str(after_time))
                )
            else:
                cutoff = None

            if cutoff:
                cutoff_ts = cutoff.timestamp()

        if before_time:
            bt = (
                before_time
                if isinstance(before_time, datetime)
                else datetime.fromisoformat(str(before_time))
            )
            before_ts = bt.timestamp()

        time_filtered: bool = cutoff_ts is not None or before_ts is not None
        if time_filtered:
            num_before = limit * 2  # Fetch extra to account for filtering

        if sort_by == "oldest" and cutoff_ts is None:
            # Fetching the true oldest messages via anchor="oldest" only
            # makes sense with no cutoff - with one, it would fetch from the
            # start of the narrow's entire history and the cutoff filter
            # below would discard all of it. With a cutoff, anchor="newest"
            # plus client-side filtering (below) is used instead, and the
            # oldest-within-window messages are picked out after filtering.
            anchor = "oldest"
            num_before = 0
            num_after = limit

        # Execute search
        if anchor == "newest" and time_filtered:
            result = _fetch_messages_until_cutoff(
                client=client,
                narrow=cast(list[dict[str, Any]], narrow),
                page_size=num_before,
                cutoff_ts=cutoff_ts,
                before_ts=before_ts,
                # sort_by="oldest" needs the whole window regardless of how
                # many matches turn up early, since the earliest-collected
                # messages while walking backward are the most recent ones,
                # not the oldest.
                min_matches=None if sort_by == "oldest" else limit,
            )
        else:
            result = client.get_messages_raw(
                anchor=anchor,
                narrow=cast(list[dict[str, Any]], narrow),
                num_before=num_before,
                num_after=num_after,
                include_anchor=True,
                client_gravatar=True,
                apply_markdown=True,
            )

        if result.get("result") == "success":
            messages = result.get("messages", [])

            # Post-fetch time filtering (Zulip doesn't filter by cutoff server-side here)
            if cutoff_ts is not None:
                messages = [m for m in messages if m["timestamp"] >= cutoff_ts]
            if before_ts is not None:
                messages = [m for m in messages if m["timestamp"] <= before_ts]

            # Trim back down to `limit`, keeping the end of the fetched
            # window that matches the requested sort order. A fetch that
            # over-fetched to compensate for client-side filtering (above)
            # would otherwise return more than `limit` messages.
            if sort_by == "oldest":
                messages = sorted(messages, key=lambda m: m["timestamp"])[:limit]
            else:
                messages = messages[-limit:]

            # Process messages for response
            processed_messages = []
            for msg in messages:
                processed_messages.append(
                    {
                        "id": msg["id"],
                        "sender": msg["sender_full_name"],
                        "email": msg["sender_email"],
                        "timestamp": msg["timestamp"],
                        "content": (
                            msg["content"][:1000] + "..."
                            if len(msg["content"]) > 1000
                            else msg["content"]
                        ),
                        "type": msg["type"],
                        "stream": msg.get("display_recipient"),
                        "topic": msg.get("subject"),
                        "reactions": msg.get("reactions", []),
                        "flags": msg.get("flags", []),
                    }
                )

            return {
                "status": "success",
                "messages": processed_messages,
                "found": len(processed_messages),
                "anchor": result.get("anchor"),
                "narrow_applied": narrow,
                "sort_by": sort_by,
                "window_complete": result.get("window_complete", True),
            }
        else:
            return {"status": "error", "error": result.get("msg", "Search failed")}

    except Exception as e:
        return {"status": "error", "error": str(e)}


async def advanced_search(
    query: str,
    search_type: list[Literal["messages", "users", "streams", "topics"]] | None = None,
    # Filters
    stream: str | None = None,
    topic: str | None = None,
    sender: str | None = None,
    has_attachment: bool | None = None,
    has_link: bool | None = None,
    is_private: bool | None = None,
    is_starred: bool | None = None,
    # Time range
    last_hours: int | None = None,
    last_days: int | None = None,
    # Response control
    limit: int = 100,
    sort_by: Literal["newest", "oldest", "relevance"] = "relevance",
    # Basic aggregations only
    aggregations: list[str] | None = None,
) -> dict[str, Any]:
    """Multi-faceted search with basic aggregations."""
    client = get_client()

    search_type = search_type or ["messages"]
    results = {}

    try:
        # Search messages
        if "messages" in search_type:
            msg_result = await search_messages(
                query=query,
                stream=stream,
                topic=topic,
                sender=sender,
                has_attachment=has_attachment,
                has_link=has_link,
                is_private=is_private,
                is_starred=is_starred,
                last_hours=last_hours,
                last_days=last_days,
                limit=limit,
                sort_by=sort_by,
            )
            results["messages"] = msg_result

        # Search users
        if "users" in search_type:
            users_response = client.get_users()
            if users_response.get("result") == "success":
                users = users_response.get("members", [])
                matching_users = [
                    user
                    for user in users
                    if query.lower() in user.get("full_name", "").lower()
                    or query.lower() in user.get("email", "").lower()
                ][:limit]
                results["users"] = {
                    "status": "success",
                    "users": matching_users,
                    "count": len(matching_users),
                }

        # Search streams
        if "streams" in search_type:
            streams_response = client.get_streams()
            if streams_response.get("result") == "success":
                streams = streams_response.get("streams", [])
                matching_streams = [
                    stream
                    for stream in streams
                    if query.lower() in stream.get("name", "").lower()
                    or query.lower() in stream.get("description", "").lower()
                ][:limit]
                results["streams"] = {
                    "status": "success",
                    "streams": matching_streams,
                    "count": len(matching_streams),
                }

        # Basic aggregations only
        if (
            aggregations
            and "messages" in results
            and results["messages"].get("status") == "success"
        ):
            messages = results["messages"].get("messages", [])
            agg_results = {}

            if "count_by_user" in aggregations:
                user_counts = Counter(msg["sender"] for msg in messages)
                agg_results["count_by_user"] = dict(user_counts.most_common(10))

            if "count_by_stream" in aggregations:
                stream_counts = Counter(
                    msg["stream"] for msg in messages if msg["stream"]
                )
                agg_results["count_by_stream"] = dict(stream_counts.most_common(10))

            results["aggregations"] = agg_results

        return {
            "status": "success",
            "query": query,
            "search_types": search_type,
            "results": results,
            "timestamp": datetime.now().isoformat(),
        }

    except Exception as e:
        return {"status": "error", "error": str(e), "query": query}


async def construct_narrow(
    # Basic narrow operators
    stream: str | None = None,
    topic: str | None = None,
    sender: str | None = None,
    # Content filters
    search_text: str | None = None,
    has_attachment: bool | None = None,
    has_link: bool | None = None,
    has_image: bool | None = None,
    # Message state filters
    is_private: bool | None = None,
    is_starred: bool | None = None,
    is_mentioned: bool | None = None,
    is_unread: bool | None = None,
    is_muted: bool | None = None,
    is_followed: bool | None = None,
    # Time-based filters
    after_time: datetime | str | None = None,
    before_time: datetime | str | None = None,
    # ID-based filters
    message_id: int | None = None,
    near_message_id: int | None = None,
    # Advanced filters
    dm_with: str | list[str] | None = None,
    group_dm_with: str | list[str] | None = None,
) -> dict[str, Any]:
    """Construct narrow filter following Zulip API patterns."""
    try:
        narrow: list[NarrowFilter] = []

        # Basic operators
        if stream:
            narrow.append({"operator": "stream", "operand": stream})
        if topic:
            narrow.append({"operator": "topic", "operand": topic})
        if sender:
            narrow.append({"operator": "sender", "operand": sender})

        # Content search
        if search_text:
            narrow.append({"operator": "search", "operand": search_text})

        # Has filters
        if has_attachment is not None:
            if has_attachment:
                narrow.append({"operator": "has", "operand": "attachment"})
            else:
                narrow.append(
                    {"operator": "has", "operand": "attachment", "negated": True}
                )

        if has_link is not None:
            if has_link:
                narrow.append({"operator": "has", "operand": "link"})
            else:
                narrow.append({"operator": "has", "operand": "link", "negated": True})

        if has_image is not None:
            if has_image:
                narrow.append({"operator": "has", "operand": "image"})
            else:
                narrow.append({"operator": "has", "operand": "image", "negated": True})

        # Is filters
        if is_private is not None:
            if is_private:
                narrow.append({"operator": "is", "operand": "private"})
            else:
                narrow.append({"operator": "is", "operand": "private", "negated": True})

        if is_starred is not None:
            if is_starred:
                narrow.append({"operator": "is", "operand": "starred"})
            else:
                narrow.append({"operator": "is", "operand": "starred", "negated": True})

        if is_mentioned is not None:
            if is_mentioned:
                narrow.append({"operator": "is", "operand": "mentioned"})
            else:
                narrow.append(
                    {"operator": "is", "operand": "mentioned", "negated": True}
                )

        if is_unread is not None:
            if is_unread:
                narrow.append({"operator": "is", "operand": "unread"})
            else:
                narrow.append({"operator": "is", "operand": "unread", "negated": True})

        if is_muted is not None:
            if is_muted:
                narrow.append({"operator": "is", "operand": "muted"})
            else:
                narrow.append({"operator": "is", "operand": "muted", "negated": True})

        if is_followed is not None:
            if is_followed:
                narrow.append({"operator": "is", "operand": "followed"})
            else:
                narrow.append(
                    {"operator": "is", "operand": "followed", "negated": True}
                )

        # Time filters
        if after_time:
            time_str = (
                after_time.isoformat()
                if isinstance(after_time, datetime)
                else after_time
            )
            narrow.append({"operator": "search", "operand": f"after:{time_str}"})

        if before_time:
            time_str = (
                before_time.isoformat()
                if isinstance(before_time, datetime)
                else before_time
            )
            narrow.append({"operator": "search", "operand": f"before:{time_str}"})

        # ID-based filters
        if message_id:
            narrow.append({"operator": "id", "operand": message_id})

        if near_message_id:
            narrow.append({"operator": "near", "operand": near_message_id})

        # DM filters
        if dm_with:
            if isinstance(dm_with, str):
                narrow.append({"operator": "dm", "operand": dm_with})
            else:
                narrow.append({"operator": "dm", "operand": dm_with})

        if group_dm_with:
            narrow.append({"operator": "group-pm-with", "operand": group_dm_with})

        return {
            "status": "success",
            "narrow": narrow,
            "filter_count": len(narrow),
            "operators_used": [n["operator"] for n in narrow],
        }

    except Exception as e:
        return {"status": "error", "error": str(e)}


async def check_messages_match_narrow(
    msg_ids: list[int],
    narrow: list[dict[str, Any]],
) -> dict[str, Any]:
    """Check whether a set of messages match a narrow filter."""
    client = get_client()

    try:
        request_data = {
            "msg_ids": msg_ids,
            "narrow": narrow,
        }

        result = client.client.call_endpoint(
            "messages/matches_narrow", method="GET", request=request_data
        )

        if result.get("result") == "success":
            messages = result.get("messages", {})
            matching_ids = list(messages.keys())

            return {
                "status": "success",
                "total_checked": len(msg_ids),
                "matching_count": len(matching_ids),
                "matching_message_ids": [int(msg_id) for msg_id in matching_ids],
                "non_matching_count": len(msg_ids) - len(matching_ids),
                "messages": messages,
                "narrow_applied": narrow,
            }
        else:
            return {
                "status": "error",
                "error": result.get("msg", "Failed to check messages against narrow"),
            }

    except Exception as e:
        return {"status": "error", "error": str(e)}


def register_search_tools(mcp: FastMCP) -> None:
    """Register core search tools with the MCP server."""
    mcp.tool(
        name="search_messages",
        description="Advanced search with fuzzy user matching and comprehensive filtering",
    )(search_messages)
    mcp.tool(
        name="advanced_search",
        description="Multi-faceted search across messages, users, streams with basic aggregations",
    )(advanced_search)
    mcp.tool(
        name="construct_narrow",
        description="Construct narrow filter following Zulip API patterns",
    )(construct_narrow)
    mcp.tool(
        name="check_messages_match_narrow",
        description="Check whether messages match a narrow filter",
    )(check_messages_match_narrow)
