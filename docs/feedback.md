---
title: User feedback
description: Thumbs, issue types and comments on one turn, stored and counted next to the turn's metrics.
---

# User feedback

A user rates **one turn**: thumbs up or down, optionally the kinds of problem it had and
a comment. Weave stores it, keyed by the turn's [`turn_id`](chat-channel.md#the-turn-id),
and counts it under the same dimensions as the turn — so a 👎 crosses with the model,
latency and tools of that exact answer.

What is yours: the list of issue types (per agent if you want), the HTTP field names and
where it is stored.

## Wire it

```python
import weave
from weave import (
    EmfTurnObserver, FeedbackPolicy, IssueType, S3FeedbackStore,
    lambda_feedback_handler, lambda_handler, routed_channel,
)

weave.configure(observers=[EmfTurnObserver("Acme/Agents", service="acme")])

POLICY = FeedbackPolicy(issue_types=(
    IssueType(id="wrong_info", label="Wrong information"),
    IssueType(id="incomplete", label="Incomplete answer"),
))

channel = routed_channel(
    resolve,
    feedback_store=S3FeedbackStore("my-bucket", prefix="feedback/"),
    feedback_policy=POLICY,          # or a function: target -> FeedbackPolicy
)

chat = lambda_handler(channel)
feedback = lambda_feedback_handler(channel)

def handler(event, context):
    path = event.get("rawPath") or event.get("resource") or ""
    return (feedback if path.endswith("/feedback") else chat)(event, context)
```

Weave does not route by path: each product owns its routes.

## The request

`POST` with a [`Feedback`](#the-model) body (or your own `parse`):

```json
{"session_id": "s1", "turn_id": "m-42", "rating": "down",
 "issues": ["wrong_info"], "comment": "That office closed last year"}
```

- **Two steps.** The usual UI sends the thumb first and the issue panel after. Both
  requests carry the same `turn_id`; the second completes the first. `rating` and
  `issues` are replaced (send the full set), a `null` comment keeps the stored one, and
  the turn keeps the target it was first rated under.
- **Never rejected for its content.** An issue id not in the target's policy is dropped
  with a warning; a comment over `max_comment` is cut. Losing the thumb is worse than
  losing an issue.
- **Routed like the chat.** The target comes from the channel's resolver with the
  feedback's `extras` (e.g. the same headers), so the UI does not need the agent name.
- **`session_id` is required**, even under `mint_if_absent`: a feedback without a session
  has no turn to refer to. The identity comes from the channel's session policy, so it
  matches the chat's (`anon_<session>` for anonymous visitors).
- The turn is not looked up in the history (that would read the session on every
  request): a feedback on an unknown turn is stored and crosses with nothing.

`GET` returns the panel for the request's target, so the issue list lives in your
config and not in the UI:

```json
{"target": "billing", "issue_types": [{"id": "wrong_info", "label": "Wrong information"}],
 "max_comment": 2000}
```

A routed channel needs the resolver's input: pass
`parse_options=lambda event: {"headers": ...}`.

## The model

| Type | What |
| --- | --- |
| `Feedback` | the parsed request: `session_id`, `turn_id`, `user_id`, `rating` (`up`/`down`), `issues`, `comment`, `extras` |
| `IssueType` | `id` (`^[a-z][a-z0-9_]{0,63}$`, stored and counted) and `label` (shown) |
| `FeedbackPolicy` | `issue_types` in display order, `max_comment` |
| `PolicyFor` | `target -> FeedbackPolicy`, called on every request |
| `FeedbackRecord` | the stored state, one per `(session_id, turn_id)`, last write wins |
| `FeedbackEvent` | what changed: `first`, `previous_rating`, `new_issues`, `comment_added` |

Renaming a `label` keeps metrics comparable; changing an `id` is a new issue.

## Storage

`FeedbackStore` is two methods, `get(session_id, turn_id)` and `put(record)`.

- `S3FeedbackStore(bucket, prefix="feedback/")` (`weave[aws]`): one JSON object per turn
  at `{prefix}{session_id}/{turn_id}.json`. Only `NoSuchKey` reads as "no feedback yet";
  any other error raises, so a stored feedback is never overwritten as if absent. No date
  partitions: the second step may arrive after midnight, and Athena reads the prefix.
- `InMemoryFeedbackStore` for tests.

## Metrics

`EmfTurnObserver` implements `on_feedback` with the same namespace and `dimensions` as
turns:

| Metric | When |
| --- | --- |
| `Feedback` | the turn's first feedback |
| `FeedbackPositive` / `FeedbackNegative` | the first rating (counted once: CloudWatch cannot subtract) |
| `FeedbackChanged` | a later request changed the rating |
| `FeedbackIssue` | once per newly added issue, with an extra `issue` dimension |
| `FeedbackComments` | a comment was added |

Rates come from metric math: `FeedbackNegative / Feedback`, coverage
`Feedback / Turns`, and `Regenerations / Turns` for the rejections nobody rated. The
comment text is never on a metric line or in the event; it only lives in the store.

To see a rated turn in Logs Insights, over the function's log group:

```
fields @timestamp, turn_id, rating, outcome, TurnDuration, model, error_type
| filter turn_id = "m-42"
```
