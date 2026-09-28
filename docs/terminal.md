# Terminal

The terminal gives the browser an interactive shell inside a lab. Nothing is emulated: keystrokes go to a real `bash` on a PTY in the lab container, and its output comes back as it is produced.

```
browser: xterm.js (linux-lab-web)
   | WebSocket, same origin (/ws)
API: router.py -> access.py -> relay.py
   | LabRuntime.open_terminal
DockerRuntime: docker exec with Tty=true, stdin attached, as the student
   |
lab container: bash --login on /dev/pts/N
```

| Module | Responsibility |
|---|---|
| `labs/terminal/router.py` | WebSocket endpoint: origin check, lab access, init, logging, close codes |
| `labs/terminal/protocol.py` | Message format, limits and close codes |
| `labs/terminal/relay.py` | Moves bytes both ways; one connection per lab; output pacing |
| `labs/access.py` | Decides which lab a request may use |
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
| 1011 | The runtime failed while starting or running the terminal |
| 4000 | The shell exited (`exit`, Ctrl+D) |
| 4404 | The lab does not exist, is not running, or may not be used |
| 4409 | Another connection opened a terminal on the same lab |

## Connection lifecycle

1. The `Origin` header is checked before the handshake is accepted. Browsers always send it on WebSocket handshakes, and it is the only protection against a third-party page opening a socket.
2. `lab_access.resolve(lab_id)` returns the lab's running container or refuses with 4404. The container is never chosen from anything else the client sends.
3. The client sends `init` with the size xterm.js computed for its container.
4. If the lab already has a terminal connection, that one is closed with 4409 and its shell is ended.
5. The runtime starts `bash --login` as the student on a PTY of the requested size, and the server sends `ready`.
6. Input and output run as two asyncio tasks, with a third waiting for a replacement. When one finishes, the others are cancelled and awaited.
7. Whatever ended the connection, the terminal session is closed (see below) before the WebSocket is closed with the matching code.

Keepalive pings are handled by uvicorn at the WebSocket protocol level (every 20 seconds by default), so a connection whose network disappeared is detected and ends like any other disconnect.

## PTY

`open_terminal` runs `docker exec` with `Tty=true` and stdin attached, as uid 1000 in `/home/student`, with `TERM=xterm-256color`. The user is fixed; the protocol has no way to choose it, pass exec parameters or change the container.

With a TTY, Docker streams raw bytes instead of multiplexed stdout and stderr, so both arrive interleaved as a real terminal shows them. The PTY size is set right after the exec starts and on every `resize`, through Docker's exec resize call.

Each terminal gets a random token in its environment (`LINUXLAB_TERMINAL`). Nothing reads it except the cleanup below.

## Disconnection and cleanup

Closing the connection to a TTY exec does not end anything in the container: the shell, the foreground command and background jobs keep running, and Docker keeps reporting the exec as running. This was checked before implementing the cleanup.

So when a terminal ends for any reason (tab closed, network lost, client closed, shell exited, connection replaced, protocol error), the session is closed explicitly:

1. The exec stream is closed.
2. A cleanup runs inside the lab **as the student**: it sends SIGHUP, then SIGKILL after a second, to every process in the shell's session and every process that carries the terminal's token, including processes moved to a new session with `setsid`. Running as the student means it can only ever reach the student's own processes.
3. If the shell had exited on its own, its exit code is read from Docker.

The effect is that of a hangup: everything started from that terminal ends with it, including `nohup` jobs. A process survives only if it left the session and removed the token from its environment. Other terminals on the same lab are not affected. If the lab itself has stopped (for example after an OOM), there is nothing left to clean up.

There is no global cleanup yet: labs, and anything detached inside them, live until the lab is removed.

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

`LabAccess.resolve(lab_id)` is the only way the terminal obtains a container. It is the point where authentication and ownership will be checked.

The current implementation, `DevelopmentLabAccess`, grants any running lab created by the platform to whoever knows its id (32 random hex characters). It is enabled only with `DEV_TERMINAL_ACCESS=true`; otherwise the terminal route does not exist. With user sessions, it is replaced by a lookup of the caller's own active lab, and the handshake gains a session check.

## Development

With the Compose environment running (see the README), create a lab and open the address it prints:

```sh
docker build --tag linuxlab/lab-base:dev lab-image
docker compose -f infra/compose.yml exec api python -m linuxlab.labs.devlab create
docker compose -f infra/compose.yml exec api python -m linuxlab.labs.devlab remove <lab-id>
```

## Logging

The API logs, for each connection, a short random connection id and the lab id, with:

- `terminal opened`: terminal size;
- `terminal closed`: reason (`shell_exited`, `client_closed`, `replaced`, or the error type), exit code, duration, bytes in and out;
- `terminal rejected`: origin, lab unavailable, or protocol error;
- failures to start, run or clean up the terminal.

Terminal content, typed commands and the terminal token are never logged.

## Known behavior

- Input that arrives before bash has set up line editing can be discarded by bash itself, for example a Ctrl+D sent before the first prompt. People type after seeing the prompt; the tests wait for it.
- In development, React's Strict Mode mounts the page twice, so the browser opens a terminal, closes it and opens another. The first one is cleaned up normally.
- A reconnect starts a new shell: files and detached processes remain, but the working directory, variables and history of the old shell do not.
