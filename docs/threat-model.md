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
| Password guessing | 5 login attempts per 15 minutes per client address, checked before Argon2 |
| CPU and memory exhaustion through Argon2 | Rate limit first; at most two hashes at a time, in worker threads |
| Account enumeration on login | Same status, code and message for unknown email and wrong password; an unknown email still runs one Argon2 verification |
| Unwanted sign-ups | Invite code compared in constant time; sign-up disabled when none is configured |
| Stored markup in names | Display names are stored as text, without control characters; the frontend renders them as text |

## Lab sessions

| Threat | Control |
|---|---|
| Using another user's lab by its id (IDOR) | Every lab route (validation included) and the terminal resolve the lab from the session's user and the id together. A missing lab and another user's lab return the same `404` / `4404`; a lab's state is only revealed to its owner |
| Guessing or reusing lab ids, container names or ids | Lab ids are random UUIDs, and knowing one grants nothing without the owner's session. Container names and ids are never returned and are not accepted as lab ids |
| Choosing the owner, container, image, runtime, user or limits | `POST /api/labs` and `POST /api/labs/{id}/replace` take only `mission_slug` and refuse extra fields. Image, runtime, deployment label and limits come from server configuration; the terminal runs as uid 1000, chosen by the API. No route forwards anything to the Docker API |
| Choosing the mission version, parameters or setup | The API pins the mission's current version itself and generates the parameters; setup and its user come only from that stored version. A slug that is not a published mission creates nothing |
| Starting labs for unpublished content | Drafts, archived missions and missions in unpublished modules answer `404 mission_not_found` before any row or container exists |
| Unauthenticated or expired terminal | The handshake resolves the session cookie like any request and closes with `4401` |
| Terminal kept open after its lab ended | Ending a lab closes its terminal (`4410`) before removing the container; reconnecting to an ended lab is refused with `4410` |
| Two labs for one user through concurrent requests | Partial unique index `one_active_lab_per_user`; creation also serializes on an advisory lock for the global cap |
| Reviving an ended lab | Conditional status updates and a database trigger that only allows the lifecycle's transitions |
| Replacing another user's lab, or linking to one | The replace route resolves the lab from the session's user and the id together (`404` otherwise); a composite foreign key requires `previous_lab_id` to be a lab of the same user |
| Reusing a replaced lab's container, files or parameters | A reset or switch always creates a new row, container and parameters; the old lab ends in a final state and its terminal is closed and refused (`4410`) |
| Repeated replacements multiplying labs | At most one successor per lab (`one_successor_per_lab`); a replacement that ends a lab and creates another counts against the creation rate limit |
| Concurrent delete, logout and reaper on one lab | All three take the same idempotent path; the first end reason is kept |
| Logout leaving a usable lab | Logout ends the lab before deleting the session, and keeps the session if ending the lab fails |
| A lab created or replaced for a session after its logout | Inserting a lab locks the session's row and requires it to exist; logout deletes the row, then ends the user's lab again. Covered by concurrency tests of logout against creation and replacement |
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
| Hidden mission data reaching students | Catalog and lab responses use explicit response models with presentation fields only; setup, parameters, conditions, solutions, counterexamples and the explanation stay on the server |
| Solutions readable before completion | Besides the API never returning them, mission content lives in a private repository; `content/` here holds no mission (enforced by a test) and the API image is checked to contain none ([missions.md](missions.md#content-source)) |
| Solutions and counterexamples running outside a lab | Only the mission tests run them, in a test lab, as the student, from standard input and without parameters; the sync and the API never run them, and the tests are not part of the production image |
| Pull request code reading the solutions | Public CI tests only synthetic missions and holds no credential for the private content. The real missions run in the private repository's own workflow, which needs no credential and accepts only commits on `main` or heads of pull requests opened by the owner from this repository, never forks |
| An unknown lab image | `environment.image` accepts only `base`, resolved to `LAB_IMAGE`; any other alias fails the sync |
| A running lab silently changing mission | A lab's mission, version and parameters are fixed by a database trigger; versions are immutable, so a later sync changes nothing for existing labs |
| Markup in briefings | The frontend renders Markdown with raw HTML disabled |

## Validation and setup

- Mission content is treated as data. The API never executes mission content on the host, and the sync executes nothing.
- The only mission code that runs is the setup script of the lab's pinned version, inside the lab's container, as the version's setup user (`student`, or `root` with the four platform capabilities and `no-new-privileges`), under the lab's limits and with no network. It is written to `bash`'s standard input and never stored in the container. It runs once, before the lab is ready, so the student's terminal cannot reach the lab while it runs.
- Setup output is discarded. Logs carry the lab id, the exit code and whether setup timed out, never the script, its output or the parameters.
- `labctl init` runs as root from `/opt/labctl`, which only root can open, on a read-only rootfs. It copies `/etc/skel` and never follows symbolic links.
- Validators are part of the API code and do not run commands. Facts come from `labctl probe`, which ships with the image and only reads: `stat` of a path, bounded reads of regular files (64 KiB), and the process list from `/proc` (command lines cut at 4 KiB). It has no operation that writes, executes, changes permissions or changes privileges.
- `labctl probe` runs as root with a fixed command line (`python3 -I -S /opt/labctl/labctl probe`) and a fixed environment. Its request is JSON on stdin (at most 64 KiB and 51 facts), built only from the lab's pinned version and parameters; the client can send nothing but an answer, which never reaches the container. Paths never go through a shell, and labctl refuses any path that is not absolute and canonical.
- The probe runs as root with `DAC_OVERRIDE`, so it never follows a symbolic link the student may have created: directories are opened one component at a time with `O_NOFOLLOW`, and content is read only from a regular file, opened with `O_NOFOLLOW | O_NONBLOCK` and checked with `fstat`. A link from `~/relatorio.txt` to a root-only file holding a parameter fails validation instead of passing it. FIFOs and devices are never read, so they cannot block the probe. `follow_symlinks` on `file_exists` and `directory_exists` reveals only the target's type.
- `labctl` output is capped at 1 MiB and 10 seconds, parsed by strict models and must answer exactly the facts asked, in order. Any anomaly results in `error`, never `passed`, and a partial answer is never evaluated. Its standard error and output are never logged.
- RE2 patterns from missions are compiled by the API with a 1 MiB memory budget and are at most 1024 characters; RE2 matches in linear time, so a pattern cannot cause catastrophic backtracking. There is no fallback to Python's `re`. Parameter values are escaped before they enter a pattern, and RE2's own error log is disabled, so neither a pattern nor a parameter reaches the logs.
- Validation results returned to the client and stored in `validation_runs` hold statuses, closed reasons and messages written in the mission, which cannot contain placeholders. Expected values, observed values, file contents, command lines, the submitted answer and the parameters are never returned, stored or logged.
- Validation changes nothing in the lab: no row, parameter, version or file. One validation per lab runs at a time, and each user may request 20 per minute, which bounds root execs started through validation.
- Root operations never execute files from locations the student can write to.
- Answers to discovery missions are compared by the API and never reach the container.
- Mission parameters are generated by the API with a restricted character set and reach setup only as environment variables. The runtime refuses any exec variable other than `LAB_PARAM_<NAME>` with a value in that set, so no other variable can be injected and nothing from the API's environment reaches the lab. A setup process left running keeps its environment, so missions start background processes with `env -i` ([missions.md](missions.md#setup)); the mission tests check, running as the student, that no process the student can read carries a `LAB_PARAM_` variable ([missions.md](missions.md#mission-tests)).

## Accepted risks

**API access to the Docker socket.** The API creates containers and therefore has root-equivalent access to the host. Remote code execution in the API compromises the host. A socket proxy does not help, since anything allowed to create containers can create a privileged one. The fix is to move lab control into a separate service (`lab-agent`) on another machine, with a narrow API. This is a prerequisite for opening public sign-up.

**Single machine.** During the closed beta, the API, database and labs share one VM dedicated to the project. A gVisor escape followed by a kernel escape would expose the database.

**Validation tampering.** The student cannot modify `labctl` or system binaries, but does control their home directory and their own processes: a process can be named to match a `comm` or `args_regex`, and a file can be written with the expected content by any means, which is the point of validating state. The probe never reads through the student's symbolic links. Hard links are not detected: in a mission that keeps a root-only file and checks content on the same filesystem, a hard link to it, where the kernel lets the student create one, would be read as a regular file. Cheating validation only hurts the student as long as there are no rankings or certificates. This must be revisited before adding either.

**Validation coordination in one process.** The one-validation-per-lab rule and the validation rate limit live in the API process, like the terminal registry, and are reset on restart. Running several workers requires moving them.

**Command history.** The history stores what the student types at the bash prompt, as typed, secrets and heredoc bodies included, with no redaction: no pattern can tell a secret from an ordinary argument. It never stores output, environments, file contents, parameters or answers sent for validation, and input read by programs (password prompts, `read -s`) never reaches it. Entries are readable only by the lab's owner and kept as long as the lab's row. Starting a line with a space keeps it out only while bash's `HISTCONTROL` keeps `ignorespace`. The hook runs in the student's shell, so the student can turn it off or forge entries, but only in their own history, which takes no part in validation or progress and proves nothing. Showing a history to anyone but its owner (teachers, analytics) would need this reassessed first ([architecture.md](architecture.md#command-history)).

**Email enumeration on sign-up.** Accepted until there is an email flow. Only someone with the invite code gets past the check that precedes it.

**Rate limiting by client address, in memory.** Limits are kept in the API process, reset on restart and are not shared between processes, which matches the single-process closed beta. They key on the client address: the TCP peer, or behind Caddy the address Caddy accepted the connection from, which uvicorn reads from `X-Forwarded-For` only on connections from Caddy's fixed address. One address can hold many users (NAT) and one user can hold many addresses (an IPv6 prefix in particular), so the limit slows guessing but does not identify people. The trust chain holds only while the API has no published port and nothing but Caddy shares its network; see [deployment.md](deployment.md#client-addresses).

**Lab state in one process.** Terminal connections and their activity live in the single API process; activity reaches the database at most 30 seconds late, and is lost if the API crashes in between, which can only shorten a lab's idle time. Running more than one API process requires moving lab control into its own service (`lab-agent`).

**Deployment label.** Reconciliation trusts the `linuxlab.deployment` label. Two deployments configured with the same `LAB_DEPLOYMENT` on one Docker Engine would remove each other's labs as orphans; each deployment must use its own value. Changing a deployment's value while it has labs leaves their containers outside its reconciliation: the labs end as `container_lost` and their containers must be removed by hand, so the value has to stay fixed for a deployment's lifetime.

**No account lockout or password breach check.** Password guessing is slowed by the rate limit only. Password reset and email verification do not exist yet, which also rules out public sign-up.

## Verification

Isolation is checked by tests that run commands inside a real lab, under both runc and runsc; results and the environment each was run in are in [runtime.md](runtime.md#verification-status).

A process started by `docker exec` keeps running when the client disconnects. Terminal sessions therefore end their shell and every process started from it when the connection ends, running the cleanup as the student ([terminal.md](terminal.md#disconnection-and-cleanup)).

A lab stopped by an OOM kill, the normal outcome of exceeding memory under runsc, is detected and reported to the student (`end_reason = oom`), checked by tests under runc and runsc.
