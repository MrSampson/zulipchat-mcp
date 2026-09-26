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
- Time filters are applied with a mix of anchor strategy and post-filtering.
- When `last_hours`/`last_days`/`after_time` is set, matching messages are fetched by walking the anchor backward page by page until the cutoff is reached, so a `before_time` upper bound further back than one page's worth of newer traffic still gets found. The response's `window_complete` flag is `False` if a hard page cap was hit before the cutoff was reached, signaling the result may not cover the full requested window.
- `advanced_search` aggregates across messages/users/streams and can return basic counts.
