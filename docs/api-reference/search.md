# Search API

## Core tool

- `search_messages(query=None, stream=None, topic=None, sender=None, ..., limit=50, sort_by="relevance")`

## Extended tools

- `advanced_search(query, search_type=None, stream=None, topic=None, sender=None, ..., aggregations=None)`
- `construct_narrow(...)`
- `check_messages_match_narrow(msg_ids, narrow)`

## Examples

Search recent messages from a stream:

```python
await search_messages(
    query="deploy",
    stream="engineering",
    last_hours=24,
    limit=30,
    sort_by="newest",
)
```

Build a narrow filter:

```python
await construct_narrow(
    stream="engineering",
    topic="deploy",
    is_unread=True,
    has_link=True,
)
```

Run multi-scope search:

```python
await advanced_search(
    query="incident",
    search_type=["messages", "users", "streams"],
    aggregations=["count_by_user", "count_by_stream"],
)
```

## Behavior notes

- `search_messages` resolves non-email sender values with fuzzy user lookup.
- `limit` must be between 1 and 1000 (inclusive). A value outside that range returns a structured error (`status="error"`, `error.code="INVALID_LIMIT"`) rather than being silently clamped or accepted.
- Time filters are applied with a mix of anchor strategy and post-filtering.
- When `last_hours`/`last_days`/`after_time` is set (with or without `before_time`) and `sort_by` is not `"oldest"`, matching messages are fetched by walking the anchor backward page by page - up to 10 pages of `limit * 2` messages each - stopping as soon as the result is known to be exact: once enough matches are found for `limit` (so a plain cutoff still needs only one page in the common case), a page's oldest message crosses the cutoff, or the narrow's history runs out. This means a `before_time` upper bound further back than one page's worth of newer traffic still gets found.
- `before_time` used *alone*, with no lower bound, walks the same way but stops once enough matches pass the `before_time` filter instead of crossing a cutoff - unless `sort_by="oldest"`, which has no window to walk at all in this case: it fetches the narrow's true oldest messages directly and filters them by `before_time`.
- With a lower bound set, `sort_by="oldest"` always walks the whole window instead of stopping at `limit`, since the earliest messages collected while walking backward are the most recent ones, not the oldest. This whole-window walk pages in chunks of at least 1000 messages (`max(limit * 2, 1000)`), decoupled from `limit`, so a small `limit` combined with a wide time window doesn't need far more pages than a large page size would.
- The response's `window_complete` flag is `False` if the 10-page cap was hit before the result could be confirmed exact, or if a later page in the walk failed after an earlier one already returned data (the failure itself isn't otherwise surfaced) - either way, signaling the result may be incomplete.
- `advanced_search` aggregates across messages/users/streams and can return basic counts.
