# API

Contract between `linux-lab-api` and `linux-lab-web`.

## Conventions

- HTTP routes live under `/api` and WebSocket routes under `/ws`, on the same origin as the frontend.
- Request and response bodies are JSON.
- Every request other than GET, HEAD and OPTIONS must send an `Origin` header whose value is listed in `ALLOWED_ORIGINS` (otherwise `403 origin_not_allowed`) and `Content-Type: application/json`, even with an empty body `{}` (otherwise `415 unsupported_media_type`). Both are checked before the body is read. WebSocket handshakes check `Origin` separately.
- Authentication uses the session cookie. The token never appears in a response body.
- A lab owned by another user and a lab that does not exist produce the same response (`404`).

Error format:

```json
{ "error": { "code": "lab_already_active", "message": "..." } }
```

User-facing messages are in Portuguese. Responses never include tracebacks, SQL, password hashes, tokens or other internal details.

General errors, shared by every route:

| Status | Code | When |
|---|---|---|
| 400 | `invalid_json` | The body is not valid JSON |
| 401 | `not_authenticated` | No session cookie, or the session is unknown or expired. The response also clears the cookie |
| 403 | `origin_not_allowed` | `Origin` missing or not allowed |
| 404 | `not_found` | Unknown route |
| 405 | `method_not_allowed` | Method not supported by the route |
| 415 | `unsupported_media_type` | Non-GET request without `Content-Type: application/json` |
| 422 | `invalid_request` | The body does not match the route's schema (missing or extra fields, wrong types) |
| 429 | `rate_limited` | Too many attempts; `Retry-After` gives the seconds to wait |
| 500 | `internal_error` | Unexpected failure |

## Endpoints

| Method | Route | Description |
|---|---|---|
| POST | `/api/auth/signup` | Create account and session. Body: `email`, `password`, `display_name`, `invite_code` |
| POST | `/api/auth/login` | Create session. Body: `email`, `password` |
| POST | `/api/auth/logout` | End the session. Ending the active lab as well comes with lab sessions (increment 05) |
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

Every route requires a session except `signup`, `login`, `logout` and `health`.

Implemented so far: `health` and the four `auth` routes. The others are the target contract.

## Authentication

The session token travels only in the `__Host-sid` cookie (`HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, no `Domain`, `Max-Age` 30 days). The client never reads or stores it; `fetch` calls send it with `credentials: 'same-origin'`. A session ends after 7 days without use or 30 days after it was created, whichever comes first. Details in [architecture.md](architecture.md#authentication).

User object, returned by sign-up, login and `me`:

```json
{ "id": "7d0e0c43-3f5a-4a53-9d1a-1a3b3f0c2e11", "email": "ana@example.com", "display_name": "Ana" }
```

### `POST /api/auth/signup`

```json
{ "email": "ana@example.com", "password": "...", "display_name": "Ana", "invite_code": "..." }
```

Returns `201` with the user and sets the session cookie. The email is stored stripped and lowercased.

| Status | Code | When |
|---|---|---|
| 403 | `signup_disabled` | No invite code is configured on the server |
| 403 | `invalid_invite_code` | Wrong invite code |
| 422 | `invalid_email` | Not a valid ASCII email address |
| 422 | `invalid_password` | Fewer than 12 or more than 128 characters |
| 422 | `invalid_display_name` | Empty, longer than 80 characters or containing control characters, after stripping |
| 409 | `email_taken` | An account already uses this email, in any letter case |
| 429 | `rate_limited` | More than 5 attempts in 15 minutes from the same address |

Checks run in that order, after the rate limit, so only a valid invite code reveals whether an email is taken.

### `POST /api/auth/login`

```json
{ "email": "ana@example.com", "password": "..." }
```

Returns `200` with the user and sets a new session cookie. An unknown email and a wrong password both return `401 invalid_credentials` with the same message. More than 5 attempts in 15 minutes from the same address return `429 rate_limited`.

### `POST /api/auth/logout`

Body `{}`. Deletes the session named by the cookie, if any, clears the cookie and returns `204`. Other sessions of the same user remain valid. Labs are not affected yet.

### `GET /api/auth/me`

Returns `200` with the user, or `401 not_authenticated`.

### `GET /api/health`

```json
{ "status": "ok", "database": "ok" }
```

Returns `200` when every dependency responds and `503` with `"unavailable"` otherwise. Docker status is added together with the lab runtime.

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

`WS /ws/labs/{id}/terminal`. The protocol, limits and connection lifecycle are described in [terminal.md](terminal.md).

In short: binary frames carry terminal bytes both ways; the client sends `init` (then `resize`) as JSON with `cols` and `rows`; the server answers `ready`, and sends `exit` or `error` before closing.

| Code | Meaning | Expected client behavior |
|---|---|---|
| HTTP 403 | `Origin` not allowed | None; the page is not served from an allowed origin |
| 1008, 1009 | Invalid or oversized message | Report the error; a client bug |
| 1011 | Runtime failure | Offer to reconnect |
| 4000 | Shell exited | Offer a new shell; do not reconnect automatically |
| 4404 | Lab unavailable | Show that the lab is not available |
| 4409 | Terminal opened by another connection | Tell the user and let them take it back; do not reconnect automatically |

Planned with lab sessions (increment 05), now that accounts exist: a session check on the handshake (`4401`), closing when the lab ends (`4410`), status and expiry messages, and automatic reconnection with backoff.
