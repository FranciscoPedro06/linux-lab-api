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
| GET | `/api/modules` | Published modules with their published missions and the user's progress on each |
| GET | `/api/missions/{slug}` | Mission detail with the user's progress: at the version of the user's active lab for this mission if there is one, otherwise at its current version |
| GET | `/api/labs/current` | The user's active lab, or `null` |
| GET | `/api/labs` | The user's recent labs |
| GET | `/api/labs/{id}` | One of the user's labs, in any state |
| POST | `/api/labs` | Create a lab for a mission. Body: `mission_slug` (`replace` from increment 10) |
| POST | `/api/labs/{id}/reset` | Recreate the lab for the same mission and return the new one |
| POST | `/api/labs/{id}/validate` | Validate the current state. Optional body: `answer` |
| DELETE | `/api/labs/{id}` | End the lab |
| GET | `/api/health` | Database and lab runtime status |
| WS | `/ws/labs/{id}/terminal` | Terminal |

Every route requires a session except `signup`, `login`, `logout` and `health`.

Implemented so far: `health`, the four `auth` routes, the two catalog routes, the lab routes except `reset`, and the terminal. `reset` is the target contract.

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
  "ended_at": "2026-10-01T12:20:03Z",
  "mission": { "slug": "secure-deploy-script", "title": "Proteja o script de deploy", "version": 2 }
}
```

- `status`: `provisioning`, `ready`, `terminating`, `terminated` or `failed` ([architecture.md](architecture.md#labs)).
- `end_reason`: `null` while the lab runs; otherwise `user`, `logout`, `no_terminal`, `no_input`, `max_lifetime`, `oom`, `container_lost`, `provisioning_failed` or `provisioning_timeout`.
- `expires_at`: the maximum lifetime, 2 hours after creation.
- `mission`: the mission version the lab was created for, with the title of that version; it does not change when the mission gets a new version or is archived. `null` only for labs created before increment 07.

Container ids, container names, the owner, the mission's parameters and other internals are never returned. The client cannot choose the user, the container, the image, the runtime, the user inside the lab, the mission version, its parameters or setup, or any limit: the body of `POST /api/labs` holds only `mission_slug`, and extra fields are refused.

### `GET /api/labs/current`

The caller's lab in `provisioning`, `ready` or `terminating`, or `null`.

### `GET /api/labs`

The caller's 20 most recent labs, newest first, in any state.

### `GET /api/labs/{id}`

The lab, in any state, including after it ended, so the client can show why.

### `POST /api/labs`

```json
{ "mission_slug": "secure-deploy-script" }
```

Creates a lab for the mission's current version: the API pins that version, generates the mission's parameters, starts the container and runs the mission's setup inside it ([missions.md](missions.md#setup)). Creation is synchronous: the response comes when the lab is ready or has failed, which can take up to about 70 seconds when setup is slow. Clients should show that the lab is being prepared and not repeat the request meanwhile; a repeated request gets `409 lab_provisioning`.

| Situation | Response |
|---|---|
| Published mission, no active lab | `201` with the new lab in `ready` |
| Missing `mission_slug`, a value that is not a string, or any other field | `422 invalid_request` |
| No such mission, or it is a draft, archived or in a module that is not published (including an empty catalog) | `404 mission_not_found`; nothing is created |
| A ready lab for the same mission already exists | `200` with that lab, unchanged: no new container, parameters or setup |
| An active lab for another mission (or one created before missions) exists | `409 active_lab_for_different_mission`; that lab is not touched |
| A lab is being started for the same mission | `409 lab_provisioning` |
| The previous lab is still being ended | `409 lab_terminating` |
| More than 10 creations in 10 minutes by this user | `429 rate_limited`, with `Retry-After` |
| `LAB_CAPACITY` labs active across all users | `503 lab_capacity_reached` |
| The mission's version cannot be used (its parameters could not be generated) | `500 internal_error`; nothing is created |
| The container could not be started, or `labctl init` or setup failed or timed out | `503 lab_start_failed`; the lab ends as `provisioning_failed` |

If the lab is ended while it is being created (logout, delete), the response is `201` with the lab in its ended state.

The rate limit and the default capacity are provisional values chosen in increment 05 ([architecture.md](architecture.md#values-chosen-in-increment-05)).

There is no way to replace the active lab with one for another mission yet: the body gains `replace` in increment 10 (mission switching). Until then the user ends the current lab first.

### `DELETE /api/labs/{id}`

Body `{}`. Ends the lab with `end_reason = user`: closes its terminal (`4410`), removes the container and returns `200` with the lab, normally `terminated`. If the container could not be removed yet the lab is returned `terminating` and the reaper finishes it. Ending an ended lab returns it unchanged; concurrent requests all succeed.

### `POST /api/labs/{id}/validate`

Implemented in increment 08. Checks whether the lab's current state meets the conditions of the mission version the lab was created for ([architecture.md](architecture.md#validation)). How the student reached that state does not matter, and command history plays no part.

```json
{}
```

```json
{ "answer": "lantern" }
```

The body holds at most `answer`: a string of up to 1 KiB in UTF-8, compared by the API with the mission's `answer` condition. It is never stored, logged or sent to the lab. Missions without an `answer` condition ignore it. Any other field is refused: the client cannot choose the mission version, conditions, paths, patterns, commands, the container or any limit.

```json
{
  "run_id": 8812,
  "outcome": "failed",
  "newly_completed": false,
  "mission": { "slug": "secure-deploy-script", "version": 2 },
  "progress": { "status": "not_completed", "completed_version": null, "completed_at": null },
  "result": {
    "kind": "all",
    "status": "failed",
    "message": null,
    "children": [
      { "kind": "check", "id": "exists", "status": "passed", "message": "O arquivo existe." },
      { "kind": "check", "id": "mode", "status": "failed", "message": "As permissões ainda não estão corretas." }
    ]
  }
}
```

- `outcome` is the status of the root: `passed` (the mission's conditions are met), `failed` (not yet) or `error` (the check itself could not run: the probe timed out or failed, the container stopped, or the pinned version cannot be read). `error` does not count as an attempt. Every outcome is stored in `validation_runs` and returned as `200`.
- `mission` is the lab's pinned version, which a newer version or archiving the mission does not change.
- `progress` is the user's progress on the mission once the run is stored ([Progress](#progress)), and `newly_completed` is `true` only for the validation that recorded the user's first completion of the mission. Only a `passed` run can record it, at the lab's pinned version, with the run's end time as `completed_at`; later passes, on this lab or another, on this version or another, return `false` and leave the completion as it is. `failed` and `error` never record or remove a completion.
- Operator nodes have `kind` set to `all`, `any` or `not`, a `message` (their `fail_message` when they failed, otherwise `null`, or the error message) and `children`. Leaves have `kind` set to `check`, their `id`, `status` and `message`.
- Messages come from the mission or are fixed defaults (`Condição atendida.`, `Condição ainda não atendida.`, `Envie uma resposta para verificar esta condição.`, `Não foi possível verificar esta condição.`). They never carry expected or observed values, file contents or parameters.
- `result` is `null` only when the pinned version cannot be read.

| Situation | Response |
|---|---|
| The lab is `ready` and its state was checked | `200` with the run |
| Body with another field, `answer` not a string or longer than 1 KiB | `422 invalid_request` |
| No such lab, or another user's | `404 lab_not_found` |
| The lab is not `ready` (provisioning, ending, ended), was created before missions, or its container is gone (the lab is then ended) | `409 lab_not_ready` |
| Another validation of this lab has not finished | `409 validation_in_progress`; nothing is stored for this request |
| More than 20 validations by this user in 60 seconds | `429 rate_limited`, with `Retry-After` |
| The run or the completion could not be stored | `500 internal_error`; neither is stored |

Validation never changes the lab, its parameters or its version, never runs setup again, and does not count as terminal activity. Repeating it creates a new run each time, and the first passed run records the completion. Clients should not validate automatically or retry in a loop: the request is made when the student asks.

The run and the completion are committed in one transaction. A response lost after that commit (a dropped connection) does not undo it: a client that did not get the response should read the progress again from the catalog rather than assume nothing was stored. Validating again then returns `newly_completed: false`.

## Catalog

Implemented in increment 06, with the user's progress since increment 09. Both routes require a session and return only published content: a mission is visible when it is `published` and listed in a `published` module, read at its current version. Content comes from `content/` through `linuxlab content sync` ([architecture.md](architecture.md#missions-and-versioning)). Each mission carries the progress of the user making the request, and never anyone else's.

Mission card, as listed in a module:

```json
{
  "slug": "secure-deploy-script",
  "title": "Proteja o script de deploy",
  "summary": "Qualquer usuário consegue ler um script que contém um token.",
  "difficulty": 1,
  "estimated_minutes": 5,
  "tags": ["permissions", "chmod"],
  "version": 2,
  "requires_answer": false,
  "progress": { "status": "completed", "completed_version": 1, "completed_at": "2026-10-17T14:03:12Z" }
}
```

- `version`: the mission's current version.
- `progress`: the user's progress on the mission ([Progress](#progress)). Here it was completed on version 1, before version 2 was published, and it stays completed.
- `requires_answer`: validation expects the `answer` field, because the mission's conditions include an `answer` condition. Derived from the stored specification; nothing is evaluated.

Setup, parameters, conditions, solutions, counterexamples and the explanation are never returned.

### `GET /api/modules`

Published modules that have at least one visible mission, ordered by slug. Missions follow the order of the module file.

```json
[
  {
    "slug": "fundamentals",
    "title": "Fundamentos",
    "description": "Navegar, criar e organizar arquivos.",
    "missions": [ { "slug": "secure-deploy-script", "...": "mission card" } ]
  }
]
```

An empty catalog is `[]`.

### `GET /api/missions/{slug}`

The mission card plus:

```json
{
  "current_version": 2,
  "module": { "slug": "fundamentals", "title": "Fundamentos" },
  "briefing": "# Proteja o script de deploy\n\n...",
  "objectives": ["`~/deploy.sh` continua existindo, com o conteúdo original."],
  "hints": ["Veja as permissões atuais com `ls -l`."]
}
```

`briefing` is Markdown. The client renders it with raw HTML disabled.

A mission that does not exist, is `draft` or `archived`, or is listed in a module that is not published returns `404 mission_not_found`; all look the same.

When the user has an active lab (`provisioning`, `ready` or `terminating`) for this mission, the detail is read at the lab's pinned version, so it matches the lab even after a newer version is published. This holds even if the mission was archived meanwhile: its owner can still read it while that lab is active. `version` is then the lab's version. Nobody else sees an archived mission, and the owner no longer does once the lab ends.

`current_version` is always the mission's current version in the catalog, so a client compares `progress.completed_version` with it, not with `version`, which can be an older lab's version.

## Progress

Implemented in increment 09. The same object appears in each mission card, in the mission detail and in the validation response, always for the user making the request:

```json
{ "status": "completed", "completed_version": 1, "completed_at": "2026-10-17T14:03:12Z" }
```

```json
{ "status": "not_completed", "completed_version": null, "completed_at": null }
```

- `status` is `completed` when a completion is recorded for the user and the mission, and `not_completed` otherwise. `not_completed` does not mean the mission was never started; labs record that.
- `completed_version` is the version the mission was first completed on, and `completed_at` when. Both are fixed by the first completion and never change.
- A newer version of the mission does not undo a completion and does not ask for a new one: `status` stays `completed` whatever `completed_version` is compared with the current version. A change that alters what a mission asks for gets a new slug ([architecture.md](architecture.md#missions-and-versioning)).

There is no route to read or change another user's progress, nor a route that takes a user id: progress is read from the session and recorded only by the validation of a lab the user owns.

## Missions (planned)

Releasing `explanation` and `solutions` to a user who completed the mission, in `GET /api/missions/{slug}` and in the validation response, is a candidate for increment 14, after the closed beta deployment (13). Until then the server never returns them. From increment 12 the validation response also carries the commands recorded in the lab.

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

