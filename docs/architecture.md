# Architecture

This document covers Linux Lab's components, main flows and the decisions behind them. Detailed contracts are in [api.md](api.md), [missions.md](missions.md) and [threat-model.md](threat-model.md).

## Principle

A mission is approved based only on the final state of the lab, or on an answer submitted by the student. How the student got there is not evaluated.

Command history exists so that students can look back at and reuse what they typed. It takes no part in validation or progress, and neither module depends on the history module.

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
| `catalog` | Read access to published modules and missions, with the user's progress on each |
| `content` | Parsing, schema validation and sync of missions from the repository |
| `progress` | `user_missions`: recording and reading the first completion of each mission |
| `labs` | Lab lifecycle, container configuration, reaper |
| `terminal` | WebSocket bridge to `docker exec` |
| `validation` | Condition tree, validators, fact collection |
| `history` | Command history: markers in the terminal output, recording and reading |

## Labs

Implemented in increment 05 (`src/linuxlab/labs/`): `models.py` (lab sessions), `lifecycle.py` (creation, ending, reconciliation), `reaper.py`, `access.py` (ownership) and `router.py` (HTTP). A lab is an ephemeral container owned by one user. Since increment 07 every lab is created for a published mission and pinned to one version of it ([Mission version and parameters](#mission-version-and-parameters)).

```
provisioning --> ready --> terminating --> terminated
      |  '-------------------^                 ^
      '----------> failed ---------------------'
```

| State | Meaning |
|---|---|
| `provisioning` | The session exists; the container is being created, started and prepared (`labctl init`, then the mission's setup) |
| `ready` | The container runs and setup completed; the terminal may be used |
| `terminating` | The lab has ended (`end_reason` says why); its terminal and container are being removed |
| `failed` | Provisioning did not complete; the container may still exist |
| `terminated` | The container has been removed. Final |

`provisioning -> terminating` covers a lab ended (logout, delete) before it was ready. The database refuses every other transition, and any change to the owner or to a recorded end reason, with a trigger; the first reason recorded is the one kept.

End reasons: `user` (`DELETE`), `logout`, `no_terminal`, `no_input`, `max_lifetime`, `oom`, `container_lost`, `provisioning_failed`, `provisioning_timeout`, `reset` and `mission_switch` ([Replacing a lab](#replacing-a-lab)).

Rules:

- A user has at most one lab in `provisioning`, `ready` or `terminating`, enforced by the partial unique index `one_active_lab_per_user`. Concurrent creations by one user produce one lab; the others get the existing lab or `409`.
- At most `LAB_CAPACITY` labs (default 10) are active across all users. Creations are serialized by a transaction-scoped advisory lock, so the cap cannot be raced. Each user may start at most 10 labs per 10 minutes. Both values are provisional; see [Values chosen in increment 05](#values-chosen-in-increment-05).
- Labs are only created by an explicit user action, for a published mission.
- A user's active lab is for one mission. Asking `POST /api/labs` for the same mission returns that lab; asking for another is refused until the lab ends. Resetting the lab, or switching it to another mission, replaces it with a new lab ([Replacing a lab](#replacing-a-lab)). Progress is kept when a lab ends or is removed ([Progress](#progress)).

### Consistency between PostgreSQL and Docker

There is no transaction spanning the database and Docker. PostgreSQL is the source of truth for ownership and lifecycle; Docker is the source of truth for whether a container exists and runs. Every step is ordered so that repeating it, or running it concurrently with another actor (a request, the reaper, an API that crashed and restarted), is safe:

- Creation commits the `provisioning` row before creating the container, so every lab container has a row by the time it can be listed.
- Ending commits `terminating` before touching the terminal or the container, so a lab is never reported usable while it is being removed.
- Only a confirmed removal moves a lab to `terminated`. If Docker fails, the lab stays `terminating` (or `failed`) and the reaper retries.
- Every status change is a conditional `UPDATE ... WHERE status IN (...)` from the expected state. Two actors ending the same lab both succeed, and a lab ended while provisioning never becomes `ready`: the creation's own update fails and it removes the container it made.

Containers are found by their deterministic name, `ll-lab-<lab id hex>`, and accepted only if their labels say they are this deployment's container for that lab (`linuxlab.managed=true`, `linuxlab.lab_id`, `linuxlab.deployment`). A container with the name but other labels is never touched. `LAB_DEPLOYMENT` keeps API instances that share a Docker Engine, including the test suite, from removing each other's labs.

### Creation

1. Check the session and the per-user rate limit.
2. In one transaction, under the creation lock:
   1. if the user has an active lab, return it when it is for the same mission or is terminating, and refuse otherwise;
   2. resolve the mission: published, in a published module; read its current version and that version's specification;
   3. generate the parameters;
   4. check global capacity;
   5. insert `lab_sessions` with status `provisioning`, the mission, the version and the parameters, and commit.
3. Outside any transaction, within 2 minutes: create and start the container, run `labctl init` as root, then the version's setup script ([missions.md](missions.md#setup)).
4. Mark the lab `ready`, starting its idle timer.

Nothing touches Docker before the row is committed, and a mission that is not published, or a version whose parameters cannot be generated, leaves no row and no container. A failure after the commit (Docker, `labctl init`, setup exiting non-zero or timing out) marks the lab `failed`, removes the container and marks it `terminated` (`provisioning_failed`); if the removal fails, the lab stays `failed` and the reaper finishes it. If the lab was ended meanwhile, the container is removed and the lab is returned as it is: the conditional update to `ready` fails, so a lab never becomes ready before, or without, its setup.

Creation is synchronous: the request returns when the lab is ready or has failed. With setup it can take up to about 70 seconds (setup may run for 60).

Setup runs once per lab, in the request that inserted the row. Nothing retries it: if the API stops during provisioning, the lab stays `provisioning` until the reaper fails it (`provisioning_timeout`) and removes the container.

### Mission version and parameters

A lab stores the mission version it was created for, `(mission_id, mission_version)`, with a composite foreign key to `mission_versions`, and the generated parameters in `params`. All three are set in the insert and never change (database trigger), and the version itself is immutable. Everything that needs the mission for a lab (setup, and validation from increment 08) reads that version, never the mission's current one:

- a sync that publishes a new version does not change labs created before it;
- a sync that archives the mission does not end its labs: the owner of an active lab keeps reading the pinned version (`GET /api/missions/{slug}`) until the lab ends, while nobody can start a new lab for it.

The client sends only `mission_slug`. The version, parameters, setup, image (the `base` alias, which is `LAB_IMAGE`), runtime, Linux user and limits come from the catalog and the server's configuration.

### Ending

`DELETE /api/labs/{id}`, logout, the reaper and a replacement all end a lab the same way:

1. `ready` or `provisioning` → `terminating`, with the end reason and time.
2. The lab's terminal connection is closed with `4410`, and ending waits (up to 15 seconds) for the terminal's cleanup exec to finish.
3. The container is removed. A removal already in progress (by another actor) is waited for.
4. `terminating` → `terminated`.

### Logout and lab creation

A lab asked for through a session (`POST /api/labs`, or the successor of `POST /api/labs/{id}/replace`) is inserted only while that session exists. The transaction that inserts the lab, under the creation lock, locks the session's `auth_sessions` row (`FOR KEY SHARE`) and refuses with `401 not_authenticated` if the row is gone. Logout:

1. ends the user's active lab;
2. deletes the session. The `DELETE` waits for an insert that holds the row's lock, and every insert after it finds no row;
3. ends the user's active lab again, which catches a lab inserted between steps 1 and 2.

So once logout has answered, no lab asked for through that session is left active: one inserted before the session was deleted has been ended by step 3 (`end_reason = logout`), and one asked for afterwards is refused. A lab ended while it was being prepared never becomes `ready`, and its creation removes the container it made ([Ending](#ending)). The order comes from PostgreSQL row locks, not from process memory, and no transaction is held while Docker works: the lock lasts only for the insert's transaction.

A replacement that is refused this way has already ended the old lab and created no successor. The same replacement asked again from a new session resumes it, as after any interruption between steps 5 and 6 of [Replacing a lab](#replacing-a-lab).

Other sessions of the user are not deleted. A lab they create after the logout's step 2 is theirs and stays active.

### Replacing a lab

Implemented in increment 10 (`Labs.replace` in `lifecycle.py`, `POST /api/labs/{id}/replace`). A reset (same mission) and a mission switch are one operation: the lab is ended and a new lab is created, a new row with its own id, container and freshly generated parameters. Creation (`POST /api/labs`) never ends a lab, and a replacement always does. The old lab is never reused, so nothing it held (files, processes, temporary state, parameters) reaches the new one, and since it ends in a final state its terminal can never be opened again.

The new row names the lab it replaced in `previous_lab_id`, which the client never sees:

| Invariant | Enforced by |
|---|---|
| The previous lab belongs to the same user | Database: composite foreign key `(previous_lab_id, user_id)` to `(id, user_id)` |
| A lab is replaced at most once | Database: partial unique index `one_successor_per_lab` |
| A lab is not its own previous lab | Database: check constraint |
| The link never changes, and a lab created normally never gains one | Database: the lifecycle trigger |
| The old and the new lab are never active together | Database: `one_active_lab_per_user`; the new lab is inserted only once the old one is `terminated` |

There is no new lifecycle state. A replacement is a saga over the existing lifecycle, and the rows of the two labs say how far it got:

1. Ownership (`404`), then the existing successor: if there is one it is the answer (below), and nothing else happens.
2. Conflicts refuse with `409 lab_not_current`: another active lab, an old lab ended by a logout, or an old lab already ended by a replacement of the other kind.
3. The creation rate limit, unless a replacement already ended this lab: resuming creates no more labs than the first attempt counted.
4. Pre-check, with the old lab untouched: the mission must be published; its current version is selected, its specification read and its parameters generated; capacity is checked counting every active lab except this one.
5. The old lab is ended with `reset` or `mission_switch` (a reason recorded earlier is kept), as in [Ending](#ending), and its container removed. Unless it is then `terminated`, the request answers `503 previous_lab_not_removed` and the reaper keeps retrying the removal.
6. Under the creation lock, the successor is inserted with the selected version and parameters, then provisioned as in [Creation](#creation).

No transaction is open while Docker works. Each step is conditional or idempotent, so a crash, a lost response or a concurrent request at any point leaves a state that asking again resumes or answers:

| Interrupted | State left | The same request, asked again |
|---|---|---|
| Before step 5 | Nothing changed | Starts over |
| In step 5, container not removed (Docker error, crash) | Old lab `terminating`; the reaper finishes it | Resumes at step 4 |
| Between steps 5 and 6 (crash, database error, capacity taken) | Old lab `terminated`, no successor | Resumes at step 4 |
| In step 6, during provisioning (crash) | Successor `provisioning`; the reaper fails it after 2 minutes | `409 lab_provisioning`, then `409 lab_not_current` |
| In step 6, provisioning failed | Successor `terminated` (`provisioning_failed`) | `409 lab_not_current` |
| Done | Successor `ready` | `200` with the successor |

A successor that failed is never replaced by another one for the same old lab: that replacement happened, and its result failed. The student starts a new lab with `POST /api/labs`, which the client offers once it has read the current state.

The version created is the one selected in step 4, even if a sync publishes another version, or archives the mission, before step 6. Versions are immutable, so what was checked is what is provisioned, as for a lab that was already active.

Concurrency relies on PostgreSQL, not on process memory:

- Two replacements of one lab both end it (the first reason is kept) and both wait for the removal. The creation lock and `one_successor_per_lab` let one of them insert the successor; the other finds it and answers `200`, `409 lab_provisioning` or, for another mission, `409 lab_not_current`. When a reset and a switch race, the one that ended the lab first proceeds and the other answers `409 lab_not_current` without waiting.
- `DELETE`, logout and the reaper end the old lab through the same path, and whoever records the reason first keeps it. The replacement then still creates the successor, except when a logout ended the old lab.
- A logout of the session that asked for the replacement, at any point, leaves no successor active ([Logout and lab creation](#logout-and-lab-creation)): a successor inserted before the session was deleted is ended by the logout, and one not inserted yet is refused with `401 not_authenticated`.
- A lab created with `POST /api/labs` between steps 5 and 6 takes the user's one active place, and the replacement answers `409 lab_not_current`.
- The old lab's terminal is closed with `4410` in step 5, and reconnecting to it is refused with `4410`. The successor's container has its own name and labels, and every removal of the old lab targets only the old lab's name and labels, so it cannot reach the new container.
- A validation of the old lab already running when it is replaced is stored against the old lab if its probe completed, or ends as `error`. It never involves the successor.

Accepted risks:

- **Capacity taken after the check.** Another user can take the last place between the end of the old lab and the insert of the new one. The student is then left without a lab (`503 lab_capacity_reached`); asking again resumes the replacement once a place is free. The old lab is not restored: its container is gone.
- **Duration.** A replacement can take up to about 100 seconds: up to 15 s for the terminal, 10 s for a removal already in progress, then up to 70 s of provisioning. The reverse proxy of increment 13 has to allow it.
- **One process.** Closing the old lab's terminal relies on the in-process terminal registry, as every way of ending a lab does. With several API processes, a terminal opened on another process would close only when the container is removed.

### Timeouts

| Condition | Limit | End reason |
|---|---|---|
| No terminal connected | 15 min | `no_terminal` |
| Terminal connected, no user input | 30 min | `no_input` |
| Maximum lifetime, from creation | 2 h | `max_lifetime` |
| Stuck in `provisioning` (creation, start, `labctl init` and setup) | 2 min | `provisioning_timeout` |
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

Implemented in increment 08 (`src/linuxlab/validation/`): `plan.py` (pinned tree and parameters to facts), `probe.py` (the `labctl probe` protocol), `evaluate.py` (pure evaluation), `service.py` (one validation, end to end), `models.py` (`validation_runs`) and `router.py` (`POST /api/labs/{id}/validate`).

1. The lab is read again and must be `ready`, with a pinned version. That version's condition tree is read from `mission_versions` (never the mission's current version) and checked again with the rules the sync applied: structure, RE2 syntax, canonical paths and limits. A version that fails them ends the run as `error` (`invalid_spec`).
2. Placeholders are replaced in memory with the lab's parameters, escaped in RE2 patterns. Each condition declares the fact it needs (`stat` of a path, the content of a file, the process list); identical facts are asked once.
3. The API checks that the container runs, then collects every fact with a single `labctl probe` call inside the container, through `docker exec` as root, with JSON on stdin and stdout. A tree made only of `answer` conditions needs no probe.
4. Each validator evaluates the facts it received. Evaluation is a pure function with no I/O.
5. The tree combines the results, and the run is inserted, final, in `validation_runs`.

Validators have no way to execute anything in the lab. They can only request facts from a closed catalog (`stat`, `read`, `processes`), so a buggy validator can at worst produce a wrong result. The command line of the probe is fixed; paths travel only inside the JSON request, which is built from the pinned version and the lab's parameters. The client sends at most an answer.

`labctl` is part of the lab image, which has a read-only rootfs, and only root can run it. The student runs as uid 1000 and cannot modify it. The probe only reads, and never follows a symbolic link the student may have created: every directory on the way is opened with `O_NOFOLLOW`, and a file is read only if it is a regular file, opened non-blocking (`O_NONBLOCK`, so a FIFO cannot hang it). `follow_symlinks: true` on `file_exists` and `directory_exists` follows links to report the target's type, never its content. The protocol is described in `lab-image/labctl`.

The probe's answer is untrusted input. It is parsed by strict models and must answer exactly the facts asked, in order, within 1 MiB and 10 seconds. A timeout, a non-zero exit status, truncated or invalid output, or a wrong protocol version fails the whole probe: every condition that needed a fact is then `error`. A partial answer is never evaluated.

Each node in the tree ends up `passed`, `failed` or `error`.

| Operator | Result |
|---|---|
| `all` | `failed` if any child failed; otherwise `error` if any child errored; otherwise `passed` |
| `any` | `passed` if any child passed; otherwise `error` if any child errored; otherwise `failed` |
| `not` | swaps `passed` and `failed`; `error` stays `error` |

A leaf fails, with a reason, when the lab's state does not match: `not_found` (missing path), `wrong_type` (a directory, FIFO or symbolic link where a regular file is expected, or a symbolic link on the way), `too_large` (a file over 64 KiB), `not_utf8`, `mismatch` (the comparison does not hold) and `no_answer`. These are states the student caused, not failures of the validator. `error` means the check itself could not run (timeout, stopped container, invalid probe output, a fact the probe could not read) and does not count as an attempt. Every leaf is evaluated, with no short-circuiting, so the student gets complete feedback.

The response sent to the client carries the status and message of each condition. Messages come from the mission (`pass_message`, `fail_message`), or are fixed defaults; they cannot contain parameters and never derive from the lab's state. Expected values, observed values, file contents, command lines, the answer and the parameters are neither returned nor stored: `validation_runs` keeps statuses and reasons only.

### Concurrency and consistency

PostgreSQL and Docker share no transaction. Validation reads the lab and changes nothing: no row, parameter, version or file is modified, and setup does not run again. The run is inserted once the result is known, as a final row; there is no `running` state, and no transaction is open during the probe. A passed run records the user's completion in the same transaction ([Progress](#progress)). A validation interrupted midway (a cancelled request, a crash) leaves no row, and a validation whose run or completion cannot be stored answers `500` and stores neither.

At most one validation runs per lab at a time; a second request gets `409 validation_in_progress` instead of waiting. Validations of different labs run independently. The set of labs being validated lives in memory in the API process, like the terminal registry, and is released however the validation ends, including cancellation; running the API as several workers requires moving it. Each user may request 20 validations per 60 seconds, in memory as well. If the container is gone when a validation starts, the lab ends (`container_lost` or `oom`) and the request gets `409 lab_not_ready`; if it stops during the probe, the run ends as `error` (`container_gone`).

## Progress

Implemented in increment 09 (`src/linuxlab/progress/`). `user_missions` holds, per user and mission, the first completion: the version it was on and when. Progress belongs to the mission, not to a version, and outlives the labs and runs it came from: nothing in `user_missions` refers to them, and ending or reaping a lab leaves it.

Recording a completion:

1. `POST /api/labs/{id}/validate` checks that the user owns the lab, then validates it as described in [Validation](#validation). Nothing is written and no transaction is open while the lab is probed.
2. In one transaction, the run is inserted in `validation_runs`; if it `passed`, `INSERT ... ON CONFLICT (user_id, mission_id) DO NOTHING RETURNING` records the completion with the lab's owner, its mission and its pinned version (never the mission's current version, which is not read), and the run's `finished_at` as `completed_at`. The progress is then read back in the same transaction, and the transaction commits.
3. The response carries `progress` and `newly_completed`, which is true only when this insert added the row.

`failed` and `error` runs never touch `user_missions`. A completion is kept when later runs fail, when the mission gets a new version, and when it is archived; a lab created before the mission was archived or changed still completes it, at its pinned version. Taking a new version as a new obligation would make editorial changes (a hint, a typo) undo progress; a change in what a mission asks for gets a new slug instead.

Concurrency and failures:

- Two transactions recording the same completion are settled by the primary key in PostgreSQL, not by the in-memory validation lock: the second insert waits for the first transaction, does nothing if it commits, and inserts if it rolls back. The read that follows sees the committed row, so the request that lost returns the stored completion with `newly_completed: false`.
- The run and the completion commit together or not at all. A failed write, or a request cancelled before the commit, stores neither.
- A response lost after the commit does not undo it; the next read of the catalog shows the completion, and validating again returns `newly_completed: false`.

| Invariant | Enforced by |
|---|---|
| One completion per user and mission | Database: primary key `(user_id, mission_id)` |
| The completed version exists and belongs to the mission | Database: foreign key `(mission_id, completed_version)` to `mission_versions` |
| A completion never changes once recorded | Database: trigger rejecting `UPDATE` |
| Deleting a user deletes their progress | Database: `ON DELETE CASCADE` on `user_id` |
| Only a passed run of a lab the user owns records a completion, at the lab's pinned version and the run's end time | Service: `validation/service.py` and `progress/service.py`, within the run's transaction |
| A completion is never removed by the application | Service: no code path deletes from `user_missions`; `DELETE` stays possible in the database so that deleting a user cascades |
| Progress is only read for the user of the session | Service: catalog and validation routes take the user from the session; no route takes a user id |

No backfill was done when the table was created: neither a lab nor a stored passed run is taken as a completion.

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
| Completed on version 1, version 2 released | Stays completed; `completed_version = 1` |
| Completed on version 1, then passed on version 2 | Stays as it was: `completed_version = 1`, `newly_completed: false` |
| Lab active on version 1 | Stays on version 1 until it ends, even if the mission is archived |
| Reset, mission switch or new lab | Uses the current version; a replacement selects it before ending the previous lab |

Students cannot pick an older version. A change that alters what the mission asks for should use a new slug.

The catalog (`GET /api/modules`, `GET /api/missions/{slug}`) shows a mission only when it is `published` and listed in a `published` module, and reads it at its current version. A module appears only with at least one such mission. The one exception is the detail of the mission of the user's active lab, read at the lab's pinned version, whatever the mission's current state.

The format is described in [missions.md](missions.md).

## Command history

Implemented in increment 12, in `src/linuxlab/history/` and `lab-image/history.bash`. A lab's history is the list of command lines its interactive bash accepted, as the terminal relay observed them. It is best-effort and informational: it is not a record of everything that ran, not an audit log, and not evidence of anything.

```
bash in the lab: PS0 hook writes a marker to the terminal
   | PTY output
relay output task: MarkerFilter takes markers out --> output to the browser
   | command line
HistoryRecorder (rate limit, bounded queue) --> writer task --> command_history
```

### What is recorded

An entry is a line that interactive bash accepted for execution. The hook reads it from bash's own history, from `PS0`, which bash expands after reading a line and before running it. An entry therefore does not mean that the command ran, succeeded or finished, and no exit code is recorded.

- One entry per accepted line. `a; b && c || d`, pipelines and redirections are one entry. A multi-line command (a loop, a quoted string over several lines, a heredoc with its body) is one entry holding its lines.
- The line is recorded as bash keeps it in its history: aliases as typed, history expansion (`!!`) expanded, a line recalled with the up arrow once more.
- Repeated lines are separate entries. The image's default `~/.bashrc` sets `HISTCONTROL=ignorespace` instead of Ubuntu's `ignoreboth`.
- Not recorded: lines that start with a space, empty lines, lines cancelled with Ctrl+C, lines with a syntax error, and lines over 4096 bytes, which are left out whole rather than cut.
- Not recorded: input read by programs instead of by bash at its prompt, such as a password typed into `read -s` or `passwd`, input to `python`, editors and pagers.
- Not recorded: commands run by scripts, `bash -c`, `sh` or any other non-interactive shell. An interactive `bash` started from the terminal loads the hook too, so its lines are recorded.
- Nothing else is stored: no output, environment, file contents, working directory, exit codes, mission parameters, probe results or answers.

### Marker and relay

The hook is `/etc/linuxlab/history.bash`, owned by root and read-only, sourced at the end of `/etc/bash.bashrc` and therefore only by interactive bash. For each new history entry it writes a marker to the terminal, `ESC ] 7771 ; <base64 of the line in UTF-8> BEL`, using only bash builtins and `/usr/bin/base64`; the line is passed as data and never run or expanded. The format is defined in `history/marker.py` and checked against the hook by a test.

The relay's output task passes every chunk through `MarkerFilter` before pacing and sending it. Complete markers are taken out, and every other byte is passed on unchanged and in order, however the output is split into chunks. A chunk that ends in what may be the start of a marker is held until the next chunk, or for at most 200 ms when no output follows, so the bytes held are bounded by one marker (about 5.5 KiB) and output is never held for long. A complete marker whose payload is not valid base64 or UTF-8, is empty, is over 4096 bytes or contains a control character other than tab and newline is taken out and counted, never stored. The browser never receives a marker.

### Recording

The output task never waits for the database. Each terminal connection has a `HistoryRecorder`, bound to the lab the connection was authorized for, which queues lines for its own writer task or drops them:

| Limit | Value |
|---|---|
| Command line | 4096 bytes in UTF-8 |
| Entries per lab | 2000; later lines are dropped |
| Rate per connection | 20 lines per second, with a burst of 20 |
| Queue per connection | 100 lines |
| Writing what is queued when the connection ends | 2 s, then the writer is cancelled |

A line dropped by a limit, by a failed insert (the database is unavailable) or by the end of the connection is lost; the terminal is never slowed down, blocked or closed because of the history. Each connection logs one line with the number of entries recorded, dropped (by reason) and rejected (by reason), and the type of the first failed insert. Commands and payloads are never logged.

The writer inserts each entry in its own short transaction. `seq` is assigned by the insert itself, one more than the lab's highest, under a transaction-level advisory lock keyed by the lab, so writers of the same lab (a connection being replaced while the next one starts, or two API processes) are serialized: `seq` starts at 1, has no gaps and follows the commit order, and paging with `after` never misses an entry. No other lock is held, and nothing touches the lab's row or its container.

The recorder starts before the terminal and drains after the terminal is released, so ending a lab (logout, reset, mission switch, the reaper) never waits for the history; entries observed before the end are still written within the drain time. A reconnect starts a new shell and a new recorder that continue the same lab's history; nothing is replayed, so nothing is duplicated. A connection only ever writes to its own lab: the history of a replaced lab stays on it, and its successor starts empty.

### Privacy

The history stores what the student types at the prompt, as typed, including secrets: tokens and passwords passed as arguments (`mysql -psecret`, `curl -H "Authorization: Bearer ..."`, `export TOKEN=...`), credentials in URLs and heredoc bodies. There is no redaction: no pattern tells a secret from an ordinary argument, and a redacted line could not be reused. A line that starts with a space is left out by bash itself; this holds only while `HISTCONTROL` keeps `ignorespace` and is not a guarantee. Mission parameters never reach the terminal's environment, but a value the student found in the lab and typed, the answer to a mission included, is stored like any other text. The web page tells the student all of this next to the terminal.

Entries belong to the lab (`command_history.lab_session_id`) and are read only by the lab's owner, through `GET /api/labs/{id}/history`, which resolves the lab with `owned_lab` like every lab route ([api.md](api.md#get-apilabsidhistory)). They cannot be changed, and they are kept as long as the lab's row, which is removed only with its user; no retention policy exists for labs yet.

### Integrity

The hook runs in the student's shell. The student can turn it off (`PS0=`, `set +o history`, redirecting bash's standard error), change `HISTCONTROL`, or write markers themselves: a valid marker printed by any program in the lab is recorded as an entry. Forged entries only reach the student's own history, which serves nothing but the student.

## Authentication

Implemented in increment 04, in `src/linuxlab/auth/`. Server-side sessions with an opaque cookie:

- 32-byte random token (`secrets.token_urlsafe`); the database stores only its SHA-256, and the token reaches the client only in the cookie;
- `__Host-sid` cookie with `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/` and no `Domain`, and a `Max-Age` of 30 days;
- a session is valid while it has been used in the last 7 days (idle expiry) and is less than 30 days old (absolute expiry, fixed at creation and never extended). `last_seen_at` is updated at most once every 5 minutes, so authenticated requests do not each write to the database. An expired session is deleted when it is next presented;
- logout first ends the user's active lab (`end_reason = logout`), then deletes the current session and clears the cookie, then ends the user's active lab again, for a lab inserted meanwhile ([Logout and lab creation](#logout-and-lab-creation)). If ending the lab fails, the session is kept so the client can retry; if only removing the container fails, the lab stays `terminating` for the reaper and logout completes. Other sessions of the same user stay valid, but the lab belonged to the user and has ended;
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

`users`, `auth_sessions`, `lab_sessions` (migrations for increments 04 and 05), `modules`, `missions` and `mission_versions` (increment 06), `validation_runs` (increment 08) and `user_missions` (increment 09) exist; the other tables are the target design.

| Table | Contents |
|---|---|
| `users` | Account, password hash, display name |
| `auth_sessions` | Sessions: SHA-256 of the token (unique), creation, last activity and absolute expiry |
| `modules` | Modules synced from `content/`: slug (unique), title, description, status |
| `missions` | Stable mission identity (slug, unique), module, position in the module (unique per module, `NULL` once removed from `content/`), status, current version |
| `mission_versions` | Immutable specification per version: content hash and the complete specification as `JSONB`; PK `(mission_id, version)` |
| `user_missions` | The first completion of a mission by a user: the version it was on and when; PK `(user_id, mission_id)` |
| `lab_sessions` | Labs: owner, status, end reason, creation, last activity, maximum lifetime, end time, the pinned mission version and the generated parameters |
| `validation_runs` | Every finished validation: the lab, its pinned version, the outcome (`passed`, `failed`, `error`), an error code for `error`, the tree of statuses and reasons, whether an answer was sent, start and end times |
| `command_history` | Command lines accepted by a lab's interactive shell: the lab, a sequence number per lab, the line, when the API observed it; PK `(lab_session_id, seq)` |

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

- `user_missions`: a missing row means no completion is recorded, not that the mission was never started (labs record that). `completed_version` and `completed_at` are `NOT NULL`, set by the insert and never changed: a trigger rejects `UPDATE`, and `DELETE` stays possible so that deleting a user cascades. A composite foreign key `(mission_id, completed_version)` references `mission_versions`, so the version exists and belongs to the mission. See [Progress](#progress) for what the database and the services each guarantee.
- `lab_sessions.params` is a JSON object of strings, the parameters generated for the lab, including answers for discovery missions. It is never sent to the client.
- `lab_sessions` references `mission_versions` with a composite foreign key `(mission_id, mission_version)`, so the version exists and belongs to the mission. The mission, the version and `params` are all set or all `NULL` (labs created before increment 07), and the trigger refuses changing any of them.
- `mission_versions` has a trigger that rejects `UPDATE` and `DELETE`.
- `validation_runs` references `lab_sessions` with a composite foreign key `(lab_session_id, mission_id, mission_version)` to `(id, mission_id, mission_version)`, so a run records exactly its lab's pinned version, and `mission_versions` with `(mission_id, mission_version)`. `CHECK` constraints tie `error_code` (a closed list) to `outcome = 'error'` and allow a missing `result` only for `invalid_spec`. A trigger rejects `UPDATE`; `DELETE` stays possible so that deleting a user cascades. No retention policy is defined yet.
- `command_history` references `lab_sessions` with `ON DELETE CASCADE` and has no user column: its owner is the lab's owner. `CHECK` constraints keep `seq` positive and the line between 1 and 4096 bytes, without control characters other than tab and newline. A trigger rejects `UPDATE`. Nothing references it, and it references neither `validation_runs` nor `user_missions`.
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
| History | Bash hook writing a marker the relay takes out of the output; informational only | Only bash knows the line it accepted; no keystroke parsing, no exec into the lab, no change to the lab lifecycle; never affects approval |
