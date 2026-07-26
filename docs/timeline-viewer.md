# Timeline viewer

The Timeline viewer is a static frontend in `frontend/timeline`. It is optional; the gateway works without it.

## Browser-facing API

The viewer calls these relative paths:

- `GET /timeline/api/records`
- `GET /timeline/api/dates`
- `GET /timeline/api/day`
- `GET /timeline/api/search`
- `GET /timeline/api/favorites`
- `PUT /timeline/api/records/<id>/favorite`
- `DELETE /timeline/api/records/<id>`

The xiaoke backend deliberately exposes corresponding `/internal/timeline/*` endpoints. Do not expose those internal endpoints directly to an untrusted browser.

## Required protection

Place the static files and browser-facing API behind a reverse proxy that:

1. requires browser authentication;
2. rate-limits requests as appropriate;
3. forwards each public Timeline API path to its matching internal endpoint;
4. adds `Authorization: Bearer <XIAOKE_API_KEY>` on the server side only.

The browser must never receive the gateway API key. Use HTTPS in production. The example configuration is a starting point only; adapt paths, authentication, TLS, trusted proxy headers, and rate limits to your own environment.

## Data actions

Favorites are separate local database markers and do not alter conversation text. Deletion is permanent in xiaoke's Timeline and changes future cross-window injection. It does not erase a separate client application's own history. Keep backups before granting access to deletion controls.
