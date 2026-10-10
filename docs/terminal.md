# Terminal

The terminal gives the browser an interactive shell inside a lab. Nothing is emulated: keystrokes go to a real `bash` on a PTY in the lab container, and its output comes back as it is produced.

```
browser: xterm.js (linux-lab-web)
   | WebSocket, same origin (/ws)
API: router.py -> session, access.py, lifecycle.py -> relay.py
   | LabRuntime.open_terminal
DockerRuntime: docker exec with Tty=true, stdin attached, as the student
   |
lab container: bash --login, stdin/stdout on the exec's TTY
```

| Module | Responsibility |
|---|---|
| `labs/terminal/router.py` | WebSocket endpoint: origin, session and lab checks, init, logging, close codes |
| `labs/terminal/protocol.py` | Message format, limits and close codes |
| `labs/terminal/relay.py` | Moves bytes both ways; the terminal registry (one connection per lab, closing a lab's terminal, activity); output pacing |
| `labs/access.py` | Resolves a lab id for a user: only that user's own lab |
| `labs/lifecycle.py` | Lab state; checks the container and ends the lab when it died |
| `labs/runtime/docker.py` | `open_terminal`, `DockerTerminalSession` and process cleanup |

The API never parses or filters what is typed. The student can run anything the lab allows; isolation is the runtime's job (see [runtime.md](runtime.md) and [threat-model.md](threat-model.md)).

## Protocol

Endpoint: `/ws/labs/{lab_id}/terminal`.

Binary frames carry terminal bytes in both directions. Input is whatever xterm.js produces: text, control characters (`\x03` for Ctrl+C, `\x04` for Ctrl+D, `\x7f` for backspace) and escape sequences (`\x1b[A` for the up arrow). Output is sent as soon as Docker delivers it; nothing waits for a newline.

Text frames carry JSON control messages, and nothing else.

Client to server:

```json
{ "type": "init", "cols": 120, "rows": 32 }
{ "type": "resize", "cols": 140, "rows": 40 }
```

`init` must be the first message and arrive within 5 seconds. `resize` is accepted afterwards. Both must have exactly these three fields, with integer sizes from 1 to 500 columns and 1 to 200 rows.

Server to client:

```json
{ "type": "ready" }
{ "type": "exit", "code": 0 }
{ "type": "error", "message": "the terminal stopped unexpectedly" }
```

`ready` is sent once the shell is running; input before it is not accepted by the client. `exit` precedes close code 4000. `error` precedes close code 1011 and never contains internal details.

### Close codes

| Code | When |
|---|---|
| HTTP 403 | `Origin` is missing or not in `ALLOWED_ORIGINS` (the handshake is refused) |
| 1008 | Invalid message: first message not `init`, bad JSON, unknown type, extra fields, size out of range, `init` sent twice, or no `init` in time |
| 1009 | Input frame over 64 KiB or control message over 1 KiB |
| 1011 | The runtime failed while starting or running the terminal, or the session or lab could not be checked |
| 4000 | The shell exited (`exit`, Ctrl+D) |
| 4401 | No session cookie, or the session is unknown or expired |
| 4404 | The lab does not exist or belongs to another user |
| 4409 | Another connection opened a terminal on the same lab |
| 4410 | The lab is not ready (ended, ending or still provisioning), its container is no longer running, or it ended during the connection |

4401 is about the session and 4410 about the lab: a valid session on someone else's lab is 4404, never 4410, so a lab's state is only revealed to its owner. How the web client reacts to each code, including automatic reconnection, is in [api.md](api.md#terminal).

## Connection lifecycle

1. The `Origin` header is checked before the handshake is accepted. Browsers always send it on WebSocket handshakes, and it is the only protection against a third-party page opening a socket.
2. The handshake is accepted, so that every later refusal can be a close code the page sees.
3. The session cookie sent with the handshake is resolved like any API request: missing, unknown or expired closes with 4401.
4. `owned_lab(user, lab_id)` returns the lab only if that user owns it (4404 otherwise). The lab must be `ready` (4410 otherwise), and its container must be running and carry this deployment's labels. A container found stopped or missing ends the lab (`oom` when Docker reports an OOM kill, `container_lost` otherwise) and closes with 4410. The container is never chosen from anything else the client sends.
5. The client sends `init` with the size xterm.js computed for its container.
6. The connection claims the lab in the terminal registry. If the lab already has a terminal connection, that one is closed with 4409 and its shell is ended. The lab's state is read again: if it ended between step 4 and the claim, the connection closes with 4410. From the claim on, ending the lab stops this connection.
7. The runtime starts `bash --login` as the student on a PTY of the requested size, and the server sends `ready`.
8. Input and output run as two asyncio tasks, with a third waiting for the claim to be stopped (replaced, or the lab ended). When one finishes, the others are cancelled and awaited.
9. Whatever ended the connection, the terminal session is closed (see below) before the WebSocket is closed with the matching code. If the shell ended or the runtime failed, the container is checked: if it died, the lab is ended and the close code is 4410 rather than 4000 or 1011.

Keepalive pings are handled by uvicorn at the WebSocket protocol level (every 20 seconds by default), so a connection whose network disappeared is detected and ends like any other disconnect.

Connecting, disconnecting and every input frame are recorded as activity for the lab's idle timeouts ([architecture.md](architecture.md#timeouts)); output is not. Recording is an in-memory update; the reaper stores it.

## PTY

`open_terminal` runs `docker exec` with `Tty=true` and stdin attached, as uid 1000 in `/home/student`, with `TERM=xterm-256color`. The user is fixed; the protocol has no way to choose it, pass exec parameters or change the container.

With a TTY, Docker streams raw bytes instead of multiplexed stdout and stderr, so both arrive interleaved as a real terminal shows them. The PTY size is set right after the exec starts and on every `resize`, through Docker's exec resize call. How the terminal device appears inside the lab differs between runc and runsc; see [Known behavior](#known-behavior).

Each terminal gets a random token in its environment (`LINUXLAB_TERMINAL`). Nothing reads it except the cleanup below.

## Disconnection and cleanup

Closing the connection to a TTY exec does not end anything in the container: the shell, the foreground command and background jobs keep running, and Docker keeps reporting the exec as running. This was checked before implementing the cleanup.

So when a terminal ends for any reason (tab closed, network lost, client closed, shell exited, connection replaced, protocol error), the session is closed explicitly:

1. The exec stream is closed.
2. A cleanup runs inside the lab **as the student**: it sends SIGHUP, then SIGKILL after a second, to every process in the shell's session and every process that carries the terminal's token, including processes moved to a new session with `setsid`. Running as the student means it can only ever reach the student's own processes.
3. If the shell had exited on its own, its exit code is read from Docker.

The effect is that of a hangup: everything started from that terminal ends with it, including `nohup` jobs. A process survives only if it left the session and removed the token from its environment. Other terminals on the same lab are not affected. If the lab itself has stopped (for example after an OOM), there is nothing left to clean up.

Anything detached inside a lab lives until the lab is removed, which the reaper does once the lab times out ([architecture.md](architecture.md#timeouts)).

If the lab is killed or removed while the cleanup runs, the cleanup exec dies with it (exit 137 under runc, 128 or 137 under runsc), and Docker keeps reporting the container as running for some tens of milliseconds. A failed cleanup therefore waits up to two seconds for the container to stop or disappear; only a cleanup that fails on a lab that keeps running is reported as an error. This removed a false `terminal cleanup failed: exit 128` error logged under runsc, and both cases are covered by tests under runc and runsc.

## Limits

| Limit | Value |
|---|---|
| Connections per lab | 1; a new one replaces the old |
| Input frame | 64 KiB (also the uvicorn `--ws-max-size`) |
| Control message | 1 KiB |
| Terminal size | 1 to 500 columns, 1 to 200 rows |
| Time to send `init` | 5 s |
| Docker control calls (start, write, resize) | 5 s |
| Cleanup exec | 10 s |
| Output rate | about 256 KiB/s per connection, with a 256 KiB burst |

Output is never truncated or dropped. Output frames are at most 64 KiB. The rate limit only slows the relay down: while it waits it stops reading from the PTY, the PTY buffer fills and the writing process blocks, as it would on a slow terminal. `Ctrl+C` still reaches the shell because input has its own task. CPU, memory and process limits belong to the lab runtime.

## Access and authentication

The terminal belongs to lab sessions ([architecture.md](architecture.md#labs)). A connection needs a valid session cookie and a ready lab owned by that session's user. A lab is ready only after its mission's setup has finished, so the shell always starts in a prepared home, with the files of `/etc/skel` and whatever setup created. Knowing a lab id, or a container name, grants nothing. There is no development mode: locally, sign up with the invite code, start a lab from a mission's page and open its terminal.

When a lab ends (delete, logout, a timeout, the container dying), its terminal is closed with 4410 and its cleanup finishes before the container is removed, so the order is always lab, terminal, exec cleanup, container. Ending a lab only ever closes that lab's connection.

## Development

With the Compose environment running (see the README), build the lab image once and publish missions; `content/` holds none until increment 11, so use the synthetic missions of the tests (see the README). Then sign up at http://localhost:5173/signup with the `SIGNUP_INVITE_CODE` Compose was started with, open a mission from the home page and start its lab:

```sh
docker build --tag linuxlab/lab-base:dev lab-image
```

## Logging

The API logs, for each connection, a short random connection id and the lab id, with:

- `terminal opened`: terminal size;
- `terminal closed`: reason (`shell_exited`, `client_closed`, `replaced`, `lab_ended`, or the error type), exit code, duration, bytes in and out;
- `terminal rejected`: origin, `not_authenticated`, `lab_unavailable`, the lab's state (`lab_terminated`, `lab_provisioning`, ...), `lab_gone`, or a protocol error;
- failures to start, run or clean up the terminal.

Terminal content, typed commands, session tokens and the terminal token are never logged.

## Known behavior

- Input that arrives before bash has set up line editing can be discarded by bash itself, for example a Ctrl+D sent before the first prompt. People type after seeing the prompt; the tests wait for it.
- In development, React's Strict Mode mounts the page twice, so the browser opens a terminal, closes it and opens another. The first one is cleaned up normally.
- A reconnect starts a new shell: files and detached processes remain, but the working directory, variables and history of the old shell do not.
- The terminal device differs between runtimes. Under runc the shell's terminal is a traditional pseudo-terminal, `/dev/pts/N`. Under runsc (gVisor) it is a host terminal passed into the sandbox and no `/dev/pts` device is exposed for it, so `tty` prints `not a tty` and programs that need `ttyname()` fail. In both runtimes the exec's TTY provides the terminal behavior the product needs, and the tests check it under both: line editing, history, window size and resize, Ctrl+C, Ctrl+D, `isatty` and interactive input. There is no additional PTY or intermediate shell.
