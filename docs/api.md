# API

Contract between `linux-lab-api` and `linux-lab-web`.

## Conventions

- HTTP routes live under `/api` and WebSocket routes under `/ws`, on the same origin as the frontend.
- Request and response bodies are JSON. Requests with a body must send `Content-Type: application/json`.
- Non-GET requests must send an `Origin` header whose value is listed in `ALLOWED_ORIGINS`.
- Authentication uses the session cookie. The token never appears in a response body.
- A lab owned by another user and a lab that does not exist produce the same response (`404`).

Error format:

```json
{ "error": { "code": "lab_already_active", "message": "..." } }
```

User-facing messages are in Portuguese.

## Endpoints

| Method | Route | Description |
|---|---|---|
| POST | `/api/auth/signup` | Create account and session. Body: `email`, `password`, `display_name`, `invite_code` |
| POST | `/api/auth/login` | Create session. Body: `email`, `password` |
| POST | `/api/auth/logout` | End the session and the active lab |
| GET | `/api/auth/me` | Current user |
| GET | `/api/modules` | Published modules with their missions and the user's progress |
| GET | `/api/missions/{slug}` | Mission detail. Uses the version of the active lab if there is one for this mission, otherwise the current version |
| GET | `/api/labs/current` | The user's active lab, or `null` |
| POST | `/api/labs` | Create a lab. Body: `mission_slug`, `replace` |
| POST | `/api/labs/{id}/reset` | Recreate the lab for the same mission and return the new one |
| POST | `/api/labs/{id}/validate` | Validate the current state. Optional body: `answer` |
| DELETE | `/api/labs/{id}` | End the lab |
| GET | `/api/health` | Database and Docker status |
| WS | `/ws/labs/{id}/terminal` | Terminal |

Every route requires a session except `signup`, `login` and `health`.

### `GET /api/missions/{slug}`

`explanation` and `solutions` are only included once the user has completed the mission. This is enforced by the server.

`requires_answer` tells the client that validation expects the `answer` field.

### `POST /api/labs`

| Situation | Response |
|---|---|
| No active lab | `201` with the lab in `ready` |
| Active lab for the same mission | `200` with the existing lab |
| Active lab for another mission, `replace` false | `409` with the current lab |
| Active lab for another mission, `replace` true | Ends the current lab and returns `201` |
| Global capacity reached | `503` |
| Concurrent creation in progress | `409` |

Creation is synchronous and takes a few seconds.

### `POST /api/labs/{id}/validate`

```json
{
  "run_id": 8812,
  "outcome": "failed",
  "mission": {
    "slug": "secure-deploy-script",
    "version": 2,
    "status": "in_progress",
    "newly_completed": false
  },
  "result": {
    "kind": "all",
    "status": "failed",
    "children": [
      { "kind": "check", "id": "exists", "status": "passed", "message": "O arquivo existe." },
      { "kind": "check", "id": "mode", "status": "failed", "message": "As permissões ainda não estão corretas." }
    ]
  }
}
```

- `outcome` is `passed`, `failed` or `error`. `error` means the check itself could not run and does not count as an attempt.
- Operator nodes have `kind` set to `all`, `any` or `not` and carry `children`. Leaves have `kind` set to `check`.
- When the mission is completed, the response also includes `explanation`, `solutions` and `commands` (commands recorded in this lab).

## Terminal

### Handshake

1. `Origin` must be listed in `ALLOWED_ORIGINS`, otherwise the handshake is rejected with HTTP 403.
2. The connection is accepted.
3. Invalid session: closed with `4401`.
4. Lab missing, owned by another user, or not `ready`: closed with `4404`.
5. The client sends `init` with the terminal size within 5 seconds.
6. If another connection exists for the lab, it is closed with `4409`.
7. The API starts the shell and begins streaming.

### Messages

Binary frames carry terminal bytes in both directions, up to 64 KB per message.

Text frames carry JSON control messages.

Client to server:

```json
{ "type": "init", "cols": 120, "rows": 32 }
{ "type": "resize", "cols": 140, "rows": 40 }
```

`cols` and `rows` accept values from 1 to 500.

Server to client:

```json
{ "type": "status", "state": "ready", "expires_at": "2026-09-27T18:00:00Z" }
{ "type": "warning", "kind": "expiring", "seconds": 300 }
{ "type": "exit", "code": 0 }
```

The server pings every 20 seconds and drops the connection after 60 seconds without a pong.

### Close codes

| Code | Meaning | Expected client behavior |
|---|---|---|
| 4000 | Shell exited | Offer a new shell; do not reconnect automatically |
| 4401 | Invalid session | Redirect to login |
| 4404 | Lab unavailable | Query `GET /api/labs/current` |
| 4409 | Terminal opened by another connection | Tell the user and let them take it back; do not reconnect automatically |
| 4410 | Lab ended (reset, expiry, mission switch) | Query `GET /api/labs/current` |
| 1006, 1011 | Connection lost or server error | Reconnect with backoff from 0.5 s to 8 s, up to 6 attempts |
