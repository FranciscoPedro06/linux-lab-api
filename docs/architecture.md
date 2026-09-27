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
| `catalog` | Read access to modules and missions, with the user's progress |
| `content` | Parsing, schema validation and sync of missions from the repository |
| `progress` | `user_missions` rules |
| `labs` | Lab lifecycle, container configuration, reaper |
| `terminal` | WebSocket bridge to `docker exec` |
| `validation` | Condition tree, validators, fact collection |
| `history` | Command history collection and redaction |

## Labs

A lab is an ephemeral container tied to a user and to a specific version of a mission.

```
provisioning --> ready --> terminating --> terminated
      |                                        ^
      '----------> failed ---------------------'
```

Rules:

- A user has at most one lab in `provisioning`, `ready` or `terminating`, enforced by a partial unique index. This also holds during a reset: the old container is removed before the new one is created.
- A lab pins `(mission_id, mission_version)` at creation. Briefing and validation use that version until the lab ends.
- Labs are only created by an explicit user action. Opening a mission page does not start a container.
- Destroying a lab does not touch progress. The environment is disposable; `user_missions` is permanent.
- Switching missions requires confirmation and ends the current lab with `end_reason = mission_switch`.

Creation:

1. Check authentication, rate limit, that the mission is published, and global capacity.
2. Generate mission parameters.
3. Insert `lab_sessions` with status `provisioning`. The constraint rejects a concurrent second creation.
4. Outside any transaction: create and start the container, run `labctl init` and the mission setup.
5. Mark the lab `ready` and create or update `user_missions`.

Any failure removes the container and marks the lab `failed`.

Timeouts:

| Condition | Limit |
|---|---|
| No terminal connected | 15 min |
| Terminal connected, no user input | 30 min |
| Maximum lifetime | 2 h |
| Stuck in `provisioning` | 2 min |

Running processes do not count as activity.

Reconciliation: the reaper runs every 30 seconds and at API startup. It destroys expired labs, finishes interrupted removals (`terminating`), removes containers labeled `linuxlab.managed=true` that have no active session in the database, and marks sessions whose container no longer exists as `terminated`. Container names are deterministic (`ll-lab-<lab_id>`), so reconciliation works even if the API crashed before storing the `container_id`. Restarting the API does not destroy labs.

## Terminal

```
xterm.js <-> WebSocket <-> API <-> Docker API (exec, tty) <-> PTY in container <-> bash
```

- The API does not interpret commands. It forwards bytes.
- Each connection starts a new shell. Files and background processes survive reconnects; the working directory and shell variables do not.
- There is at most one connection per lab. A new connection closes the previous one.
- On disconnect the API kills the shell explicitly (`labctl kill-shell`), because a process started through `docker exec` can outlive its client.
- Output is capped at about 256 KB/s per terminal. The sender awaits the WebSocket before reading the next chunk, with no unbounded queue in between. `yes` or `cat /dev/urandom` cannot grow API memory, and `Ctrl+C` still gets through.

Handshake, messages and close codes are in [api.md](api.md#terminal).

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

Missions live in `content/` as YAML, Markdown and scripts. `linuxlab content sync`, run on deploy:

1. validates every mission against the schema;
2. computes a `content_hash` over a canonical serialization of everything the student sees or runs;
3. inserts a new row in `mission_versions` when the hash changes and updates `missions.current_version`;
4. marks missions that were removed from the repository as `archived`.

Versions are immutable and hold the complete specification, including setup and Markdown. At runtime the API reads only from the database.

| Situation | Behavior |
|---|---|
| Completed on version 1, version 2 released | Stays completed; `completed_version = 1` |
| Lab active on version 1 | Stays on version 1 until it ends |
| Reset or new lab | Uses the current version |

Students cannot pick an older version. A change that alters what the mission asks for should use a new slug.

The format is described in [missions.md](missions.md).

## Command history

The lab image configures `bash` to append events to `/run/lab/history.log`:

- `PS0` records the start of a command, before it runs;
- `PROMPT_COMMAND` records the end, with exit code and working directory.

Command and directory are stored base64-encoded to avoid escaping issues. The API collects the file on validation, reset, mission switch and lab shutdown, and writes it with an upsert on `(lab_session_id, seq)`, so collecting twice is harmless.

Known limitations: commands inside scripts and what happens inside programs such as `vim` are not recorded, the student can disable the hook, and entries can be forged. None of this affects validation.

Before storing, the API redacts common secret patterns (`password=`, `token=`, `Authorization:`, `-p<password>`) and truncates each command to 2 KB. History is visible only to its owner.

## Authentication

Server-side sessions with an opaque cookie:

- 32-byte random token; the database stores only its SHA-256;
- `__Host-sid` cookie with `HttpOnly`, `Secure`, `SameSite=Lax` and `Path=/`;
- 7-day idle expiry and 30-day absolute expiry;
- Argon2id password hashing, run in a thread pool so it does not block the event loop;
- in-memory rate limits on login, sign-up, lab creation and validation;
- sign-up gated by an invite code during the closed beta.

CSRF protection: `SameSite=Lax`, a required `Origin` header matching the allowlist on every non-GET request, and a JSON-only API. The WebSocket checks `Origin` before accepting the connection.

OAuth, password reset and email verification are out of scope for the MVP. The last two are required before opening public sign-up.

## Data model

| Table | Contents |
|---|---|
| `users` | Account, password hash, display name |
| `auth_sessions` | Sessions; the primary key is the token hash |
| `modules` | Modules synced from `content/` |
| `missions` | Stable mission identity (slug), module, position, status, current version |
| `mission_versions` | Immutable specification per version; PK `(mission_id, version)` |
| `user_missions` | Progress per user and mission; PK `(user_id, mission_id)` |
| `lab_sessions` | Labs, with pinned version, parameters, state and end reason |
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
- `mission_versions` has a trigger that rejects `UPDATE`.
- States and reasons are `text` columns with `CHECK` constraints rather than `ENUM`, which keeps schema changes simple.

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
