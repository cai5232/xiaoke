# Continuity and window identity

## The rule for shared continuity

Clients share a Timeline only when they all send requests to the **same xiaoke deployment**: the same gateway base URL, the same gateway API key, and therefore the same SQLite Timeline database. Source labels do not create separate Timelines and do not require separate keys.

A source label is optional metadata. If operators want reliable source names in the Timeline, a reverse proxy can add `X-Xiaoke-Source` independently for each client path. This label is useful for display and diagnostics, but it is not the basis of cross-client continuity.

## Rolling context is not a one-time import

When xiaoke detects a new window or a missing shared-history anchor, it selects recent eligible records from the Timeline. Selection is bounded by `MAX_HANDOFF_RECORDS` and `MAX_HANDOFF_CHARS`.

Those bounds affect only what is injected on a particular request. They never erase older Timeline records. On later turns, xiaoke selects from the latest Timeline again and retains the identified window's continuity baseline. This prevents the common failure where a new window remembers the first handoff reply but loses the shared background on its next reply.

## Window identification

A frontend should send a stable per-conversation identifier whenever possible.

- `X-Conversation-ID` is used when a client provides it.
- `X-Xiaoke-Session-ID` can be supplied by a proxy or client as an explicit stable session key.
- Without a stable identifier, xiaoke uses a conservative history-based fallback. It can recognize some continuations, but it cannot be perfectly reliable when multiple windows have similar or repeated messages.

For reliable behavior, integrate a stable window ID in the client or at its reverse proxy. Do not depend on source labels alone to identify a conversation window.

## What is recorded

xiaoke records successful eligible user and assistant text turns, plus explicit application events. A completed user/assistant turn is committed together. An interrupted upstream stream, failed request, empty turn, system prompt, credential, request header, raw attachment, or incomplete tool-call chain is not eligible for normal Timeline storage or handoff.

System prompts and personas remain owned by each frontend. xiaoke does not turn the Timeline into a replacement system prompt; selected Timeline records remain role-preserving chat messages.
