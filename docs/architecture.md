# Architecture

This document covers Linux Lab's components, main flows and the decisions behind them. Detailed contracts are in [api.md](api.md), [missions.md](missions.md) and [threat-model.md](threat-model.md).

## Principle

A mission is approved based only on the final state of the lab, or on an answer submitted by the student. How the student got there is not evaluated.

Command history exists for learning and analytics. It takes no part in validation, and the validation module does not depend on the history module.

## Components

```
Browser: React SPA + xterm.js (linux-lab-web)
   |  HTTPS (REST)  /  WSS (terminal)
Caddy: TLS, frontend static files, proxy for /api and /ws
   |
API: FastAPI, single process
   |  auth | catalog | content | progress | labs | terminal | validation | history
   |  reaper (background task)
   |  LabRuntime (interface)
   |
   |-- PostgreSQL
   '-- Docker Engine, runsc runtime
          '-- lab containers
```

Frontend and API are served from the same origin, so there is no CORS, and cookies and `Origin` checks stay simple.

The API runs as a single process in the MVP. WebSocket connections, rate limiting and the terminal registry live in memory. Scaling out requires moving lab control into its own service (`lab-agent`), which the `LabRuntime` interface already allows for.

| Module | Responsibility |
|---|---|
| `auth` | Sign-up, login, logout, sessions |
| `catalog` | Read access to published modules and missions; the user's progress is added in increment 09 |
| `content` | Parsing, schema validation and sync of missions from the repository |
| `progress` | `user_missions` rules |
| `labs` | Lab lifecycle, container configuration, reaper |
| `terminal` | WebSocket bridge to `docker exec` |
| `validation` | Condition tree, validators, fact collection |
| `history` | Command history collection and redaction |

## Labs

Implemented in increment 05 (`src/linuxlab/labs/`): `models.py` (lab sessions), `lifecycle.py` (creation, ending, reconciliation), `reaper.py`, `access.py` (ownership) and `router.py` (HTTP). A lab is an ephemeral container owned by one user. From increment 07 it will also be tied to a specific version of a mission; today it is a plain lab with no mission.

```
provisioning --> ready --> terminating --> terminated
      |  '-------------------^                 ^
      '----------> failed ---------------------'
```

| State | Meaning |
|---|---|
| `provisioning` | The session exists; the container is being created and started |
| `ready` | The container runs; the terminal may be used |
| `terminating` | The lab has ended (`end_reason` says why); its terminal and container are being removed |
| `failed` | Provisioning did not complete; the container may still exist |
| `terminated` | The container has been removed. Final |

`provisioning -> terminating` covers a lab ended (logout, delete) before it was ready. The database refuses every other transition, and any change to the owner or to a recorded end reason, with a trigger; the first reason recorded is the one kept.

End reasons: `user` (`DELETE`), `logout`, `no_terminal`, `no_input`, `max_lifetime`, `oom`, `container_lost`, `provisioning_failed`, `provisioning_timeout`.

Rules:

- A user has at most one lab in `provisioning`, `ready` or `terminating`, enforced by the partial unique index `one_active_lab_per_user`. Concurrent creations by one user produce one lab; the others get the existing lab or `409`.
- At most `LAB_CAPACITY` labs (default 10) are active across all users. Creations are serialized by a transaction-scoped advisory lock, so the cap cannot be raced. Each user may start at most 10 labs per 10 minutes. Both values are provisional; see [Values chosen in increment 05](#values-chosen-in-increment-05).
- Labs are only created by an explicit user action.
- Planned with missions: pinning `(mission_id, mission_version)` and the mission's parameters when the lab is created (increment 07), keeping progress when a lab is destroyed (increment 09), and ending the lab with `end_reason = mission_switch` when switching missions (increment 10).

### Consistency between PostgreSQL and Docker

There is no transaction spanning the database and Docker. PostgreSQL is the source of truth for ownership and lifecycle; Docker is the source of truth for whether a container exists and runs. Every step is ordered so that repeating it, or running it concurrently with another actor (a request, the reaper, an API that crashed and restarted), is safe:

- Creation commits the `provisioning` row before creating the container, so every lab container has a row by the time it can be listed.
- Ending commits `terminating` before touching the terminal or the container, so a lab is never reported usable while it is being removed.
- Only a confirmed removal moves a lab to `terminated`. If Docker fails, the lab stays `terminating` (or `failed`) and the reaper retries.
- Every status change is a conditional `UPDATE ... WHERE status IN (...)` from the expected state. Two actors ending the same lab both succeed, and a lab ended while provisioning never becomes `ready`: the creation's own update fails and it removes the container it made.

Containers are found by their deterministic name, `ll-lab-<lab id hex>`, and accepted only if their labels say they are this deployment's container for that lab (`linuxlab.managed=true`, `linuxlab.lab_id`, `linuxlab.deployment`). A container with the name but other labels is never touched. `LAB_DEPLOYMENT` keeps API instances that share a Docker Engine, including the test suite, from removing each other's labs.

### Creation

1. Check the session and the per-user rate limit.
2. Under the creation lock: return the user's active lab if there is one, check global capacity, insert `lab_sessions` with status `provisioning`.
3. Outside any transaction: create and start the container (at most 2 minutes).
4. Mark the lab `ready`, starting its idle timer.

A failure marks the lab `failed`, removes the container and marks it `terminated` (`provisioning_failed`). If the lab was ended meanwhile, the container is removed and the lab is returned as it is.

### Ending

`DELETE /api/labs/{id}`, logout and the reaper all end a lab the same way:

1. `ready` or `provisioning` → `terminating`, with the end reason and time.
2. The lab's terminal connection is closed with `4410`, and ending waits (up to 15 seconds) for the terminal's cleanup exec to finish.
3. The container is removed. A removal already in progress (by another actor) is waited for.
4. `terminating` → `terminated`.

### Timeouts

| Condition | Limit | End reason |
|---|---|---|
| No terminal connected | 15 min | `no_terminal` |
| Terminal connected, no user input | 30 min | `no_input` |
| Maximum lifetime, from creation | 2 h | `max_lifetime` |
| Stuck in `provisioning` | 2 min | `provisioning_timeout` |
| Left in `terminating` or `failed` | 1 min (chosen in increment 05, see below), then the reaper finishes it | (kept) |

Activity is connecting, disconnecting and typing in the terminal. Running processes and terminal output do not count. Activity is recorded in memory by the terminal registry, with no I/O on the terminal's path, and stored in `last_activity_at` by the reaper at the start of each pass, so it is at most 30 seconds stale.

### Values chosen in increment 05

The design already called for a global capacity check and a rate limit on lab creation (creation step 1, `503` when capacity is reached, and the threat model's control against repeated creation), and for the reaper to finish interrupted removals, but did not give numbers. Increment 05 picked these, and they are decisions to revisit, not measured limits:

| Value | Where | Purpose | Status |
|---|---|---|---|
| `LAB_CAPACITY` = 10 active labs, all users | Setting (`LAB_CAPACITY`) | Bounds what labs can take from the single host: each may use 512 MB and half a CPU, so 10 labs reserve up to 5 GB and 5 CPUs | Provisional. To be sized against the real VM before the closed beta (increment 13); configurable without code changes |
| 10 creations per user per 10 minutes | Constant in `labs/router.py` | Bounds container churn (create, start, remove) by one account; the one-lab rule already prevents parallel labs, so this only limits repetition | Provisional. In memory, per process, reset on restart, like the authentication limits |
| 1 minute before the reaper finishes a lab left in `terminating` or `failed` | Constant `TERMINATION_GRACE` in `labs/lifecycle.py` | Lets the request that ended the lab finish its own removal (it waits up to 15 s for the terminal and up to 10 s for a removal in progress) before the reaper repeats it. Repeating it would be harmless, since every step is idempotent; the grace only avoids duplicate work and log noise. The startup pass ignores it | Lifecycle decision |

### Reaper and reconciliation

The reaper (`reaper.py`) runs at API startup and every 30 seconds. Each pass:

1. stores terminal activity recorded since the last pass;
2. lists this deployment's lab containers by label, then reads the unfinished sessions (in that order: a container is created only after its row is committed, so every listed container has a visible row);
3. ends `ready` labs past a timeout;
4. ends `ready` labs whose container stopped or disappeared: `oom` when Docker reports an OOM kill, otherwise `container_lost`. A container missing from the listing is inspected directly first, since it may have been created after the listing;
5. fails labs stuck in `provisioning`;
6. finishes labs left in `terminating` or `failed` after the grace period, immediately on the startup pass, when no request can still be working on them;
7. removes this deployment's lab containers with no unfinished lab (no row, or a terminated one).

Errors are handled per lab, so one lab cannot stop the pass, and the startup pass is repeated until one completes. Restarting the API does not destroy labs: a ready lab with a running container keeps running, and its idle timer continues from the stored activity.

### Out of memory

Under gVisor the memory cgroup covers the whole sandbox, so a lab that exceeds 512 MB is stopped with `OOMKilled=true` (see [runtime.md](runtime.md#differences-between-runc-and-runsc)). The API notices it in two places: when the terminal's shell ends because the container died, and in the next reaper pass. Either way the lab ends with `end_reason = oom`, the terminal closes with `4410`, and the client reads the reason from the API. Under runc only the offending process is killed and the lab keeps running.

## Terminal

```
xterm.js <-> WebSocket <-> API <-> Docker API (exec, tty) <-> PTY in container <-> bash
```

- The handshake checks `Origin`, then the session cookie (`4401`), then that the lab belongs to the session's user (`4404`) and is ready with a running container (`4410`).
- The API does not interpret commands. It forwards bytes.
- Each connection starts a new shell. Files and background processes survive reconnects; the working directory and shell variables do not.
- There is at most one connection per lab. A new connection closes the previous one.
- On disconnect the API ends the shell and everything started from it with a cleanup exec run as the student, because a process started through `docker exec` can outlive its client. When the lab ends, its terminal is closed and cleaned up before the container is removed.
- Output is capped at about 256 KB/s per terminal. The sender awaits the WebSocket before reading the next chunk, with no unbounded queue in between. `yes` or `cat /dev/urandom` cannot grow API memory, and `Ctrl+C` still gets through.

Handshake, messages and close codes are in [api.md](api.md#terminal) and [terminal.md](terminal.md).

## Validation

Validation runs in two phases:

1. Each condition in the tree declares the facts it needs (`stat` of a path, file contents, process list).
2. The API batches those requests into a single `labctl probe` call inside the container, through `docker exec` as root, with JSON on stdin and stdout.
3. Each validator evaluates the facts it received. Evaluation is a pure function with no I/O.
4. The tree combines the results.

Validators have no way to execute anything in the lab. They can only request facts from a closed catalog, so a buggy validator can at worst produce a wrong result.

`labctl` is part of the lab image, which has a read-only rootfs, and only root can run it. The student runs as uid 1000 and cannot modify it.

Each node in the tree ends up `passed`, `failed` or `error`.

| Operator | Result |
|---|---|
| `all` | `failed` if any child failed; otherwise `error` if any child errored; otherwise `passed` |
| `any` | `passed` if any child passed; otherwise `error` if any child errored; otherwise `failed` |
| `not` | swaps `passed` and `failed`; `error` stays `error` |

`error` means an infrastructure failure (timeout, stopped container) and does not count as an attempt. Every leaf is evaluated, with no short-circuiting, so the student gets complete feedback.

The response sent to the client carries the status and message of each condition. Expected and actual values are kept only in `validation_runs`, since they can give away the solution.

## Missions and versioning

Implemented in increment 06 (`src/linuxlab/content/` and `src/linuxlab/catalog/`). Missions live in `content/` as YAML, Markdown and scripts; that directory is the source of truth, and the database is its queryable copy plus every version ever published. At runtime the API reads only from the database.

`linuxlab content sync` (run on deploy, and by hand in development) works in two phases:

1. **Read and validate, without touching the database.** Every module and mission file is parsed and checked against the schema, file sizes and references, and the links between modules and missions ([missions.md](missions.md#content-rules)). Nothing in the content is executed. Any problem stops the sync with every error listed, each with its file and field.
2. **Write, in one transaction.** The sync takes a transaction-scoped advisory lock before reading the current state, so concurrent syncs run one after the other and each decides version numbers on what the previous one committed. Any failure rolls the whole sync back.

Within the transaction:

- Modules and missions are matched by slug, created when new and updated in place. Title, description, status, module and position never create a version.
- Each mission's specification (every field of `mission.yaml` except `status`, with the referenced Markdown and scripts inlined, defaults made explicit) is hashed: SHA-256 of its canonical JSON (keys sorted, UTF-8, no insignificant whitespace), with line endings in every file normalized to LF first. Hidden fields such as setup, parameters, conditions, solutions and counterexamples are part of it.
- When the hash differs from the current version's, a row is inserted in `mission_versions` with the mission's highest version plus one, and `missions.current_version` points to it. Content that returns to an earlier state also gets a new number; there is no uniqueness on the hash.
- Modules and missions no longer in `content/` are marked `archived`, never deleted, and keep their versions. An archived mission has no position. A slug that comes back reuses its row; it gets a new version only if its content differs from its current version.
- With an empty `content/` (no modules and no missions) the sync refuses to run and changes nothing, unless `--allow-empty` asks for every module and mission to be archived. Deploys and CI never pass it.

Running the sync again on the same content writes nothing.

Versions are immutable: a trigger rejects `UPDATE` and `DELETE` on `mission_versions`, and they hold the complete specification, including setup and Markdown.

| Situation | Behavior |
|---|---|
| Completed on version 1, version 2 released | Stays completed; `completed_version = 1` (increment 09) |
| Lab active on version 1 | Stays on version 1 until it ends (increment 07) |
| Reset or new lab | Uses the current version |

Students cannot pick an older version. A change that alters what the mission asks for should use a new slug.

The catalog (`GET /api/modules`, `GET /api/missions/{slug}`) shows a mission only when it is `published` and listed in a `published` module, and reads it at its current version. A module appears only with at least one such mission. Until labs are tied to missions (increment 07), the detail always uses the current version.

The format is described in [missions.md](missions.md).

## Command history

The lab image configures `bash` to append events to `/run/lab/history.log`:

- `PS0` records the start of a command, before it runs;
- `PROMPT_COMMAND` records the end, with exit code and working directory.

Command and directory are stored base64-encoded to avoid escaping issues. The API collects the file on validation, reset, mission switch and lab shutdown, and writes it with an upsert on `(lab_session_id, seq)`, so collecting twice is harmless.

Known limitations: commands inside scripts and what happens inside programs such as `vim` are not recorded, the student can disable the hook, and entries can be forged. None of this affects validation.

Before storing, the API redacts common secret patterns (`password=`, `token=`, `Authorization:`, `-p<password>`) and truncates each command to 2 KB. History is visible only to its owner.

## Authentication

Implemented in increment 04, in `src/linuxlab/auth/`. Server-side sessions with an opaque cookie:

- 32-byte random token (`secrets.token_urlsafe`); the database stores only its SHA-256, and the token reaches the client only in the cookie;
- `__Host-sid` cookie with `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/` and no `Domain`, and a `Max-Age` of 30 days;
- a session is valid while it has been used in the last 7 days (idle expiry) and is less than 30 days old (absolute expiry, fixed at creation and never extended). `last_seen_at` is updated at most once every 5 minutes, so authenticated requests do not each write to the database. An expired session is deleted when it is next presented;
- logout first ends the user's active lab (`end_reason = logout`), then deletes the current session and clears the cookie. If ending the lab fails, the session is kept so the client can retry; if only removing the container fails, the lab stays `terminating` for the reaper and logout completes. Other sessions of the same user stay valid, but the lab belonged to the user and has ended;
- Argon2id password hashing (`argon2-cffi` defaults: RFC 9106 low-memory parameters) in worker threads, with at most two hashes running at once, so it neither blocks the event loop nor takes unbounded CPU and memory;
- a login for an unknown email verifies the password against a dummy hash, so its timing and response match a wrong password;
- passwords of 12 to 128 characters with no composition rules; emails stripped and lowercased, ASCII only, unique case-insensitively through a unique index on `lower(email)`; display names stripped, 1 to 80 characters, without control characters, stored as plain text;
- in-memory rate limits on sign-up and login: 5 attempts per 15 minutes per endpoint and peer address, checked before any Argon2 work;
- sign-up gated by a single invite code from `SIGNUP_INVITE_CODE`, compared in constant time. Without it, sign-up is disabled.

The rate limit uses the address of the TCP peer; `X-Forwarded-For` is ignored and uvicorn runs with `--no-proxy-headers`. Behind a reverse proxy every client would share the proxy's address, so the key has to change when Caddy is deployed. State lives in the single API process and resets on restart.

CSRF protection: `SameSite=Lax`, a required `Origin` header matching the allowlist on every non-GET request, and a JSON-only API. Both are checked for every HTTP route under `/api` before the body is read (`ApiRoute` in `src/linuxlab/api.py`). The WebSocket checks `Origin` before accepting the connection.

The terminal WebSocket checks the session cookie on the handshake (`4401`) and that the lab belongs to the session's user (see [Terminal](#terminal)). There is no development bypass: labs are created through the API in every environment.

OAuth, password reset and email verification are out of scope for the MVP. The last two are required before opening public sign-up.

## Data model

`users`, `auth_sessions`, `lab_sessions` (migrations for increments 04 and 05), `modules`, `missions` and `mission_versions` (increment 06) exist; the other tables are the target design.

| Table | Contents |
|---|---|
| `users` | Account, password hash, display name |
| `auth_sessions` | Sessions: SHA-256 of the token (unique), creation, last activity and absolute expiry |
| `modules` | Modules synced from `content/`: slug (unique), title, description, status |
| `missions` | Stable mission identity (slug, unique), module, position in the module (unique per module, `NULL` once removed from `content/`), status, current version |
| `mission_versions` | Immutable specification per version: content hash and the complete specification as `JSONB`; PK `(mission_id, version)` |
| `user_missions` | Progress per user and mission; PK `(user_id, mission_id)` |
| `lab_sessions` | Labs: owner, status, end reason, creation, last activity, maximum lifetime, end time. The pinned mission version and parameters are added with missions |
| `validation_runs` | Every validation, with the full result and the version used |
| `command_history` | Collected commands; unique on `(lab_session_id, seq)` |

```
modules 1-N missions 1-N mission_versions
                 |              |
users 1-N user_missions --------'  (completed_version)
  |
  |-- 1-N auth_sessions
  '-- 1-N lab_sessions N-1 mission_versions
             |-- 1-N validation_runs
             '-- 1-N command_history
```

Active lab constraint:

```sql
CREATE UNIQUE INDEX one_active_lab_per_user
  ON lab_sessions (user_id)
  WHERE status IN ('provisioning', 'ready', 'terminating');
```

Other rules:

- `user_missions`: a missing row means the mission was never started. `completed_at` and `completed_version` are set together and never cleared.
- `lab_sessions.params` holds answers for discovery missions and is never sent to the client.
- `mission_versions` has a trigger that rejects `UPDATE` and `DELETE`.
- `missions` has a foreign key `(id, current_version)` to `mission_versions`, so the current version always exists and belongs to the mission. It and the uniqueness of `(module_id, position)` are checked at commit, which lets a sync insert a mission with its first version and reorder a module in one transaction.
- `modules` and `missions` have `CHECK` constraints on the status (`draft`, `published`, `archived`) and the slug format; a mission without a position must be `archived`.
- States and reasons are `text` columns with `CHECK` constraints rather than `ENUM`, which keeps schema changes simple.
- `lab_sessions` has `CHECK` constraints tying `end_reason` and `ended_at` to the ended states, and a trigger that allows only the lifecycle's transitions and never changes the owner, the creation or expiry time, or a recorded end reason.
- No container id is stored: the container name is derived from the lab id, so reconciliation works even if the API crashed between creating the container and recording anything about it.

Organizations and classes, when they exist, will be new tables. Content and progress tables stay as they are.

## Decisions

| Decision | Choice | Reason |
|---|---|---|
| Structure | Modular monolith, single process | Less coordination; lab memory is the bottleneck, not the API |
| Database | PostgreSQL, no Redis | Constraints replace locks; one datastore |
| Authentication | Server-side session, HttpOnly cookie | Revocable, no token reachable from JavaScript |
| Runtime | Docker with gVisor | Students run arbitrary code; the host kernel cannot be the only barrier |
| Lab network | None | Prevents abuse from the server and access to internal services |
| Lab | Ephemeral, one per user, pinned version | Reproducible initial state; resource limit enforced by the database |
| Terminal | `docker exec` with TTY, one shell per connection | PTY lives in the container; reconnects are simple |
| Missions | Content versioned in the repository | Review, diffs and automated tests |
| Versioning | Automatic, from a content hash | Does not rely on someone remembering to bump a number |
| Validation | Declarative conditions over facts collected by a platform utility | No validation code from content; testable without Docker |
| History | Bash hook, informational only | Cleaner data than keystroke capture; never affects approval |
