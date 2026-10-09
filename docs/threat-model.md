# Threat model

## Assumption

The student has a shell inside the lab and can run any program. The model assumes the worst case: a user trying to escape the container, exhaust server resources, reach other labs, or use the server to attack third parties.

Controls that rely on the student not being root are defense in depth, never the primary protection.

## Boundaries

| Boundary | Primary protection |
|---|---|
| Lab to host kernel | gVisor (`runsc`) |
| Lab to network | `network_mode: none` |
| Lab to other labs | Separate containers, no network, per-cgroup resource limits |
| Lab to API | No channel initiated by the lab; `labctl` output is treated as untrusted input |
| Browser to another user's lab | Ownership check, from the session and the lab id together, on every lab route and on the WebSocket handshake (see [Lab sessions](#lab-sessions)) |
| Third-party site to user session | `SameSite=Lax` `__Host-` cookie, `Origin` and `Content-Type: application/json` required on every non-GET request, checked before the body is read |
| Internet to accounts | Argon2id hashes, server-side sessions stored as SHA-256, rate limits on sign-up and login, invite code |

## Container configuration

Built by a single function and covered by a snapshot test. Any change requires an explicit change to the test.

| Setting | Value |
|---|---|
| Runtime | `runsc` in production: with `ENVIRONMENT=production`, the default, the API refuses to start unless `LAB_OCI_RUNTIME=runsc` and Docker has `runsc` registered. `runc` only with `ENVIRONMENT=development` |
| Network | `none` |
| Rootfs | Read-only |
| tmpfs | `/home/student` 64 MB and `/tmp` 32 MB with `exec`; `/run/lab` 1 MB with `noexec`; all `nosuid,nodev` |
| Memory | 512 MB, no swap. tmpfs counts toward this limit |
| CPU | 0.5 |
| Processes | runc: cgroup PID limit 128. runsc: RLIMIT_NPROC 128 inside the sandbox and cgroup PID limit 512 on the host |
| Capabilities | `drop ALL`; `add CHOWN, DAC_OVERRIDE, FOWNER, KILL` |
| `no-new-privileges` | Enabled |
| User | uid 1000 for the terminal; root only for platform execs |
| Seccomp | Default profile, never `unconfined` |
| Mounts | No bind mounts, devices or sockets |
| Namespaces | No `privileged`, `pid=host`, `ipc=host` or `userns=host` |
| ulimits | `nofile=1024`, `core=0`; `nproc=128` under runsc only |
| Init | tini; main process `sleep infinity` |

The added capabilities are there for `labctl init`, `labctl probe` and `labctl kill-shell`, which run as root. The student has no effective capabilities and no path to uid 0: the image has no `sudo` and no setuid or setgid binaries, and `no-new-privileges` blocks privilege escalation.

The process limit is set differently per runtime. Under runc, `nproc` would be counted per uid across the whole host, so uid 1000 in every lab would share one counter; the cgroup PID limit is used instead. Under runsc, the cgroup PID limit counts the sandbox's host threads rather than student processes, and reaching it kills the sandbox, so it only protects the host; the limit students hit is `nproc`, which gVisor counts inside each sandbox.

Under `runsc`, syscall filtering is done by gVisor itself.

Observed differences between the runtimes are listed in [runtime.md](runtime.md#differences-between-runc-and-runsc).

## Resource exhaustion

| Student action | Effect |
|---|---|
| Fork bomb | `fork` fails with EAGAIN at the process limit. Under runsc, a fork loop of heavy processes (about 5 MB of host memory each) exhausts memory first and the lab is OOM-killed. The host and other labs are unaffected in both cases |
| CPU loop | Capped at 0.5 CPU, measured from host cgroup statistics |
| Memory allocation | runc: the OOM killer ends the offending process and the lab keeps running. runsc: the whole sandbox is OOM-killed and the lab stops. The host and other labs are unaffected in both cases |
| Disk writes | `ENOSPC` once the tmpfs is full |
| Continuous terminal output | Paced at about 256 KiB/s per connection; the PTY buffer fills and the writing process blocks |
| Long-running processes | Killed with the container |
| Repeated lab creation | One active lab per user (constraint), 10 creations per user per 10 minutes, and a global cap (`LAB_CAPACITY`) |

Labs are destroyed after 15 minutes with no terminal connected, 30 minutes with no input, or 2 hours in total, by the reaper ([architecture.md](architecture.md#timeouts)).

## Authentication

| Threat | Control |
|---|---|
| Database leak | Passwords stored only as Argon2id hashes; sessions stored only as the SHA-256 of a 32-byte random token, so a leaked table cannot be replayed as cookies |
| Session theft from JavaScript | `HttpOnly` cookie; the frontend never handles the token |
| Cookie scoping | `__Host-` prefix: `Secure`, `Path=/`, no `Domain`, so no subdomain or plain-HTTP page can set or read it |
| Long-lived stolen session | 7-day idle expiry and 30-day absolute expiry that activity cannot extend; logout deletes the session on the server |
| CSRF | `SameSite=Lax`, required allowed `Origin` and JSON content type on every non-GET request |
| Password guessing | 5 login attempts per 15 minutes per peer address, checked before Argon2 |
| CPU and memory exhaustion through Argon2 | Rate limit first; at most two hashes at a time, in worker threads |
| Account enumeration on login | Same status, code and message for unknown email and wrong password; an unknown email still runs one Argon2 verification |
| Unwanted sign-ups | Invite code compared in constant time; sign-up disabled when none is configured |
| Stored markup in names | Display names are stored as text, without control characters; the frontend renders them as text |

## Lab sessions

| Threat | Control |
|---|---|
| Using another user's lab by its id (IDOR) | Every lab route and the terminal resolve the lab from the session's user and the id together. A missing lab and another user's lab return the same `404` / `4404`; a lab's state is only revealed to its owner |
| Guessing or reusing lab ids, container names or ids | Lab ids are random UUIDs, and knowing one grants nothing without the owner's session. Container names and ids are never returned and are not accepted as lab ids |
| Choosing the owner, container, image, runtime, user or limits | `POST /api/labs` takes `{}` and refuses extra fields. Image, runtime, deployment label and limits come from server configuration; the terminal runs as uid 1000, chosen by the API. No route forwards anything to the Docker API |
| Unauthenticated or expired terminal | The handshake resolves the session cookie like any request and closes with `4401` |
| Terminal kept open after its lab ended | Ending a lab closes its terminal (`4410`) before removing the container; reconnecting to an ended lab is refused with `4410` |
| Two labs for one user through concurrent requests | Partial unique index `one_active_lab_per_user`; creation also serializes on an advisory lock for the global cap |
| Reviving an ended lab | Conditional status updates and a database trigger that only allows the lifecycle's transitions |
| Concurrent delete, logout and reaper on one lab | All three take the same idempotent path; the first end reason is kept |
| Logout leaving a usable lab | Logout ends the lab before deleting the session, and keeps the session if ending the lab fails |
| Orphan containers (API crash, failed removal) | The reaper removes this deployment's lab containers with no unfinished lab and retries unfinished removals |
| Reaper removing containers that are not its own | Containers are selected by the `linuxlab.managed` and `linuxlab.deployment` labels and accepted only with the matching lab id and name. Containers without them, or of another deployment, are left alone |
| One user's lab affecting another's | The reaper, delete and logout act on one lab id at a time; ending a lab closes only that lab's terminal. Covered by tests for delete, logout and the reaper |
| Exhausting the host with labs | One active lab per user, at most 10 creations per user per 10 minutes, a global cap (`LAB_CAPACITY`), and idle and lifetime timeouts. The two numbers are provisional ([architecture.md](architecture.md#values-chosen-in-increment-05)) |
| A lab silently gone (OOM under gVisor, container removed) | The terminal and the reaper detect the stopped container; the lab ends with `oom` or `container_lost` and the student is told why |
| Information in the health check | `/api/health` returns only `ok` or `unavailable` per dependency |
| Labs under runc in production | `ENVIRONMENT=production` (the default) refuses to start unless `LAB_OCI_RUNTIME=runsc` and Docker has `runsc` registered |

## Mission content

Content in `content/` is reviewed in the repository, but the sync still treats it as untrusted input ([missions.md](missions.md#content-rules)).

| Threat | Control |
|---|---|
| Malformed or ambiguous YAML | Safe loader only; duplicate keys and aliases rejected; strict schema with no type coercion and no unknown fields |
| Oversized files or alias expansion exhausting memory | Byte limits per file and per mission, checked before parsing; aliases rejected |
| Reading files outside a mission | References are relative, without `..`; symbolic links anywhere under `content/` are rejected |
| Code in content running on the host | Nothing in a mission is executed, imported or compiled during the sync; `{{ }}` placeholders are plain names checked against the declared parameters |
| A published version changing after the fact | `mission_versions` rejects `UPDATE` and `DELETE`; a change always becomes a new version |
| A partial sync after an error | Content is validated completely before the transaction starts, and the write is a single transaction |
| Two syncs racing for the same version number | Transaction-scoped advisory lock taken before the current state is read |
| An empty or wrong directory archiving the whole catalog | Empty content is refused unless `--allow-empty` is given |
| Hidden mission data reaching students | Catalog responses use explicit response models with presentation fields only; setup, parameters, conditions, solutions, counterexamples and the explanation stay on the server |
| Markup in briefings | The frontend renders Markdown with raw HTML disabled |

## Validation and setup

- Mission content is treated as data. The API never executes mission content on the host.
- The only mission code that runs is `setup.sh`, inside the container.
- Validators are part of the API code and do not run commands. Facts come from `labctl`, which ships with the image and only reads: `lstat`, bounded reads of regular files, `/proc`.
- `labctl` receives JSON on stdin and runs with a fixed environment under `python3 -I -S`. Paths never go through a shell.
- `labctl` output is capped at 256 KB and 10 seconds. Any anomaly results in `error`, never `passed`.
- Root operations never execute files from locations the student can write to.
- Answers to discovery missions are compared by the API and never reach the container.
- Mission parameters are generated by the API with a restricted character set and reach setup only as environment variables.

## Accepted risks

**API access to the Docker socket.** The API creates containers and therefore has root-equivalent access to the host. Remote code execution in the API compromises the host. A socket proxy does not help, since anything allowed to create containers can create a privileged one. The fix is to move lab control into a separate service (`lab-agent`) on another machine, with a narrow API. This is a prerequisite for opening public sign-up.

**Single machine.** During the closed beta, the API, database and labs share one VM dedicated to the project. A gVisor escape followed by a kernel escape would expose the database.

**Validation tampering.** The student cannot modify `labctl` or system binaries, but does control their home directory and their own processes. Cheating validation only hurts the student as long as there are no rankings or certificates. This must be revisited before adding either.

**Command history.** Secret redaction is heuristic. The risk is low because labs have no network.

**Email enumeration on sign-up.** Accepted until there is an email flow. Only someone with the invite code gets past the check that precedes it.

**Rate limiting by peer address, in memory.** Limits are kept in the API process, reset on restart and are not shared between processes, which matches the single-process closed beta. They key on the TCP peer: behind a reverse proxy all clients share one address and one budget, and one address can hold many users (NAT) or one user many addresses. `X-Forwarded-For` is ignored until a trusted proxy is configured. This has to be revisited together with the Caddy deployment.

**Lab state in one process.** Terminal connections and their activity live in the single API process; activity reaches the database at most 30 seconds late, and is lost if the API crashes in between, which can only shorten a lab's idle time. Running more than one API process requires moving lab control into its own service (`lab-agent`).

**Deployment label.** Reconciliation trusts the `linuxlab.deployment` label. Two deployments configured with the same `LAB_DEPLOYMENT` on one Docker Engine would remove each other's labs as orphans; each deployment must use its own value. Changing a deployment's value while it has labs leaves their containers outside its reconciliation: the labs end as `container_lost` and their containers must be removed by hand, so the value has to stay fixed for a deployment's lifetime.

**No account lockout or password breach check.** Password guessing is slowed by the rate limit only. Password reset and email verification do not exist yet, which also rules out public sign-up.

## Verification

Isolation is checked by tests that run commands inside a real lab, under both runc and runsc; results and the environment each was run in are in [runtime.md](runtime.md#verification-status).

A process started by `docker exec` keeps running when the client disconnects. Terminal sessions therefore end their shell and every process started from it when the connection ends, running the cleanup as the student ([terminal.md](terminal.md#disconnection-and-cleanup)).

A lab stopped by an OOM kill, the normal outcome of exceeding memory under runsc, is detected and reported to the student (`end_reason = oom`), checked by tests under runc and runsc.
