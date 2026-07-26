# xiaoke

A self-hosted, OpenAI-compatible shared-context gateway.

xiaoke sits between one or more chat clients and an upstream model. It keeps a durable local conversation Timeline, then supplies an appropriate recent slice when a client opens a new window or otherwise lacks the shared context.

## What it is — and is not

xiaoke is shared infrastructure, not a persona or prompt manager.

- Frontends keep their own system prompt, persona, memory mechanism, and interface.
- xiaoke stores eligible completed text turns and manages cross-window context handoff.
- It does not provide a model or an upstream API key.
- Only requests that pass through this gateway can enter the shared Timeline. Existing history in another app is not imported automatically.

## Core behavior

- OpenAI-compatible `/v1/chat/completions` gateway.
- Local SQLite Timeline with monotonic ordering and durable storage.
- Rolling shared context: old records can leave the injection budget without being deleted from the database.
- A handoff remains continuous after the first message: later turns in the same identified window rebuild the needed baseline rather than forgetting it on the second or third reply.
- Successful user/assistant turns are committed together. Failed, disconnected, or incomplete streaming replies do not enter the Timeline.
- Unknown OpenAI-compatible request options, including tool schemas, are forwarded upstream. Tool-call chains are not written as normal Timeline conversation records.
- Optional static Timeline viewer in `frontend/timeline`.

Read [Continuity and window identity](docs/continuity-and-window-identity.md) before connecting multiple clients.

## Privacy boundary

The Timeline contains conversation content. Treat it as private application data.

xiaoke is designed not to store or inject system prompts, persona text, memory-injection blocks, request headers, credentials, raw attachments, or incomplete tool chains. Do not publish `data/`, `.env`, captured requests, backups, or a production reverse-proxy configuration.

## Quick start

Requires Python 3.11 or newer.

```sh
git clone <your-fork-url>
cd xiaoke
cp .env.example .env
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python xiaoke_app.py
```

Set a long random `XIAOKE_API_KEY` and configure your client to use the gateway's `/v1` base URL. Configure an OpenAI-compatible upstream in `.env`.

See [`.env.example`](.env.example) for all settings. In particular, `MAX_HANDOFF_RECORDS` and `MAX_HANDOFF_CHARS` limit context injection; they do not delete Timeline records.

## Shared Timeline viewer

`frontend/timeline` is an optional static browser viewer with calendar navigation, search, favorites, and irreversible record deletion.

It expects browser-facing routes under `/timeline/api/`. The backend exposes matching authenticated `/internal/timeline/*` routes. A reverse proxy must protect the browser viewer and inject the gateway Bearer key server-side. **Never put that key in browser JavaScript.**

The viewer's delete action permanently removes a record from xiaoke's Timeline, so it will no longer be injected into later handoffs. It does not delete any original copy that a separate frontend may keep. Protect the viewer with authentication, rate limiting, and backups.

See [Timeline viewer deployment](docs/timeline-viewer.md) and the intentionally incomplete [Nginx example](deploy/nginx.timeline.example.conf).

## Development checks

```sh
pip install -r requirements-dev.txt
for f in tests/test_*.py; do PYTHONPATH=. python "$f"; done
node --check frontend/timeline/app.js
```

## Security notes

- This repository contains no credentials, production database, captured requests, backups, or server-specific configuration.
- Use HTTPS and authenticate both the chat gateway and Timeline viewer.
- Restrict the gateway listener or firewall rules to the clients/reverse proxy that need it.
- Review the reverse-proxy example before deploying; it is a template, not a drop-in production configuration.

## Acknowledgements

Special thanks to [Dylan Heartbeat](https://github.com/callie0313/dylan-heartbeat), an AI residency runtime for Kelivo.

Dylan Heartbeat inspired us to think seriously about an AI companion's continuity across time: not only replying when spoken to, but keeping a coherent shared world of real conversations and autonomous events.

xiaoke is an independently implemented project focused on a different question: preserving shared conversation continuity across windows and frontends through a local Timeline and context gateway. It does not include Dylan Heartbeat code.

## License and contact

xiaoke is released under the [PolyForm Noncommercial License 1.0.0](LICENSE). Noncommercial self-hosting, study, modification, and redistribution are permitted under that license. Commercial use, paid deployment, and paid services based on xiaoke require prior written permission.

Maintainer / contact: Xiaohongshu [@Kismetobe_i](https://www.xiaohongshu.com/user/profile/Kismetobe_i)
