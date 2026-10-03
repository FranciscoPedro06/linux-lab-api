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
| POST | `/api/auth/logout` | End the user's active lab, then the session |
| GET | `/api/auth/me` | Current user |
| GET | `/api/modules` | Published modules with their missions and the user's progress |
| GET | `/api/missions/{slug}` | Mission detail. Uses the version of the active lab if there is one for this mission, otherwise the current version |
| GET | `/api/labs/current` | The user's active lab, or `null` |
| GET | `/api/labs` | The user's recent labs |
| GET | `/api/labs/{id}` | One of the user's labs, in any state |
| POST | `/api/labs` | Create a lab. Body: `{}` (with missions: `mission_slug`, `replace`) |
| POST | `/api/labs/{id}/reset` | Recreate the lab for the same mission and return the new one |
| POST | `/api/labs/{id}/validate` | Validate the current state. Optional body: `answer` |
| DELETE | `/api/labs/{id}` | End the lab |
| GET | `/api/health` | Database and lab runtime status |
| WS | `/ws/labs/{id}/terminal` | Terminal |

Every route requires a session except `signup`, `login`, `logout` and `health`.

Implemented so far: `health`, the four `auth` routes, the lab routes except `reset` and `validate`, and the terminal. The others are the target contract.

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

Body `{}`. Ends the user's active lab (`end_reason = logout`, container removed), then deletes the session named by the cookie, clears the cookie and returns `204`. Without a valid session no lab is touched. If ending the lab fails, the response is `500` and the session is kept, so the client can retry; if only the container removal fails, the lab is left `terminating` for the reaper and the response is `204`. Other sessions of the same user remain valid.

### `GET /api/auth/me`

Returns `200` with the user, or `401 not_authenticated`.

### `GET /api/health`

```json
{ "status": "ok", "database": "ok", "runtime": "ok" }
```

`database` is whether PostgreSQL answers; `runtime` is whether Docker answers and has the configured OCI runtime registered (`runsc` in production). Each is `ok` or `unavailable`, checked with a 3 second limit. Returns `200` when both are `ok`, otherwise `503` with `"status": "unavailable"`. The response carries states only: no versions, addresses, socket paths or error details.

## Labs

Implemented in increment 05. Every route requires a session and acts on the caller's own labs only: the lab id in the URL is never enough. A lab that does not exist and a lab owned by another user both return `404 lab_not_found` with the same body. Lab ids are UUIDs in canonical form; anything else is `404`.

Lab object:

```json
{
  "id": "3f1c2b9a-8d7e-4f60-a5b4-c3d2e1f0a9b8",
  "status": "terminated",
  "end_reason": "oom",
  "created_at": "2026-10-01T12:00:00Z",
  "expires_at": "2026-10-01T14:00:00Z",
  "ended_at": "2026-10-01T12:20:03Z"
}
```

- `status`: `provisioning`, `ready`, `terminating`, `terminated` or `failed` ([architecture.md](architecture.md#labs)).
- `end_reason`: `null` while the lab runs; otherwise `user`, `logout`, `no_terminal`, `no_input`, `max_lifetime`, `oom`, `container_lost`, `provisioning_failed` or `provisioning_timeout`.
- `expires_at`: the maximum lifetime, 2 hours after creation.

Container ids, container names, the owner and other internals are never returned. The client cannot choose the user, the container, the image, the runtime, the user inside the lab or any limit: the body of `POST /api/labs` is `{}` and extra fields are refused.

### `GET /api/labs/current`

The caller's lab in `provisioning`, `ready` or `terminating`, or `null`.

### `GET /api/labs`

The caller's 20 most recent labs, newest first, in any state.

### `GET /api/labs/{id}`

The lab, in any state, including after it ended, so the client can show why.

### `POST /api/labs`

Body `{}`. Creation is synchronous and takes about a second.

| Situation | Response |
|---|---|
| No active lab | `201` with the new lab in `ready` |
| A ready lab already exists | `200` with that lab |
| A lab is being started | `409 lab_provisioning` |
| The previous lab is still being ended | `409 lab_terminating` |
| More than 10 creations in 10 minutes by this user | `429 rate_limited`, with `Retry-After` |
| `LAB_CAPACITY` labs active across all users | `503 lab_capacity_reached` |
| The container could not be started | `503 lab_start_failed`; the lab ends as `provisioning_failed` |

If the lab is ended while it is being created (logout, delete), the response is `201` with the lab in its ended state.

The rate limit and the default capacity are provisional values chosen in increment 05 ([architecture.md](architecture.md#values-chosen-in-increment-05)).

With missions (increment 06) the body gains `mission_slug` and `replace`.

### `DELETE /api/labs/{id}`

Body `{}`. Ends the lab with `end_reason = user`: closes its terminal (`4410`), removes the container and returns `200` with the lab, normally `terminated`. If the container could not be removed yet the lab is returned `terminating` and the reaper finishes it. Ending an ended lab returns it unchanged; concurrent requests all succeed.

## Missions (planned)

### `GET /api/missions/{slug}`

`explanation` and `solutions` are only included once the user has completed the mission. This is enforced by the server.

`requires_answer` tells the client that validation expects the `answer` field.

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

In short: the handshake carries the session cookie; binary frames carry terminal bytes both ways; the client sends `init` (then `resize`) as JSON with `cols` and `rows`; the server answers `ready`, and sends `exit` or `error` before closing.

Browsers do not expose the HTTP status of a refused WebSocket handshake, so after the `Origin` check every refusal is a close code the page can act on.

| Code | Meaning | Client behavior |
|---|---|---|
| HTTP 403 | `Origin` not allowed | None; the page is not served from an allowed origin |
| 4401 | No session cookie, or the session is unknown or expired | Check the session; send the user to login. Never reconnect automatically |
| 4404 | No such lab, or it belongs to another user | Show that the lab was not found. Do not reconnect |
| 4410 | The lab is not ready: it ended (or is ending) or is still provisioning, including when it ends while connected | Read the lab from the API and show its state and `end_reason`. Do not reconnect while it is not `ready` |
| 4409 | Terminal opened by another connection | Tell the user and let them take it back; do not reconnect automatically |
| 4000 | Shell exited | Offer a new shell; do not reconnect automatically |
| 1008, 1009 | Invalid or oversized message | Report the error; a client bug |
| 1011 | Runtime failure | Reconnect automatically, as below |
| 1001, 1006, 1012 | Server going away, connection lost, server restart | Reconnect automatically, as below |

Automatic reconnection is for connection failures only (1001, 1006, 1011, 1012, 1013, 1014): up to 5 attempts after waiting 1, 2, 4, 8 and 16 seconds, each attempt preceded by `GET /api/labs/{id}` and made only if the lab is still `ready`. A successful connection resets the count. After the fifth failure the client stops and offers a manual reconnect.

