# Deployment

The closed beta runs on one Linux host with a native Docker Engine and gVisor: Caddy, the API and PostgreSQL in Docker Compose (`infra/compose.prod.yml`), and the labs, which the API creates through the host's Docker Engine under `runsc`. How to install, update, back up and recover a deployment is in [operations.md](operations.md).

## Topology

```
Internet
   |  TCP 80, 443
   v
[public]  caddy  (TLS, static frontend, proxy for /api and /ws)
   |
[edge, internal]  caddy 172.30.1.2  ->  api 172.30.1.3:8000  (one uvicorn process)
                                          |              |
[db, internal]                        postgres:5432   /var/run/docker.sock
                                                         |
                                       Docker Engine -> runsc -> lab containers
                                                         (NetworkMode none)
```

| Network | Members | Internet | Published ports |
|---|---|---|---|
| `public` | Caddy | Yes (ACME) | 80 and 443 on the host |
| `edge` | Caddy, API | No (`internal`) | None |
| `db` | API, PostgreSQL, one-off tasks | No (`internal`) | None |

Labs are on no network at all (`NetworkMode: none`, [threat-model.md](threat-model.md)): they reach neither the API, PostgreSQL, Caddy, each other nor the internet. The API has no route to the internet either; it only talks to PostgreSQL and to the Docker socket.

| Persistent | Where | Notes |
|---|---|---|
| `postgres-data` | PostgreSQL data directory | Accounts, sessions, labs, progress, validation runs, command history. Backed up ([operations.md](operations.md#backups)) |
| `caddy-data` | Caddy's `/data` | Certificates and their private keys. Not a cache: losing it means new certificates and possibly hitting ACME rate limits |
| `caddy-config` | Caddy's `/config` | Last loaded configuration; can be lost |

Everything else is ephemeral: lab containers and their tmpfs, and the API's memory (terminal connections, the history writers' order, rate limits, the one-validation-per-lab rule).

Privileges: the API mounts the Docker socket, which is root on the host ([threat-model.md](threat-model.md#accepted-risks)); it runs as uid 10001 with the socket's group added, a read-only root filesystem, no capabilities and `no-new-privileges`. Caddy keeps only `NET_BIND_SERVICE`. Nothing else mounts the socket, and the one-off tasks have no access to it.

### One API process

Terminal connections, the order of history writers, rate limits and the reaper live in the API process ([architecture.md](architecture.md#components)). The Compose file starts it with `--workers 1` (which also overrides `WEB_CONCURRENCY`), a fixed container name and a fixed address, so `docker compose up --scale api=2` fails instead of starting a second process.

## Client addresses

The authentication rate limits key on the client address ([architecture.md](architecture.md#authentication)). Behind Caddy, every connection to the API comes from Caddy, so the address has to be carried, and a client must not be able to choose it.

1. Caddy accepts connections straight from clients and has no `trusted_proxies`: it ignores any `X-Forwarded-For` a client sends, and `{client_ip}` is the peer of the connection.
2. Caddy sends the API `X-Forwarded-For: {client_ip}`, replacing whatever the request carried (`header_up` in `infra/caddy/linuxlab.caddy`).
3. uvicorn runs with `--proxy-headers --forwarded-allow-ips=<Caddy's edge address>`: it reads `X-Forwarded-For` only on connections from that one address, and takes the last entry that is not a trusted address. Any other peer keeps its own address. `*` is never used.
4. The API has no published port and shares the `edge` network with Caddy only, so no other peer can present Caddy's address.

`tests/auth/test_client_address.py` checks the API side with uvicorn's middleware configured this way; `infra/tests/e2e.sh` checks the whole chain through Caddy from clients with distinct addresses, including forged headers ([Checking a release](#checking-a-release)).

IPv6: Docker forwards connections to a published port from the host's IPv6 addresses to an IPv4-only container network through its userland proxy, and Caddy would then see the proxy, not the client. Until IPv6 is checked end to end on the host, the domain should have no `AAAA` record.

## Building a release

A release is three images built from exact commits of `linux-lab-api` and `linux-lab-web`:

```sh
infra/release/build.sh --api-repo ~/src/linux-lab-api --api-sha <40-hex> \
  --web-repo ~/src/linux-lab-web --web-sha <40-hex> --out /var/lib/linuxlab/releases
```

| Image | Built from | Tag |
|---|---|---|
| API | `Dockerfile`, target `prod` | `linuxlab-api:<api sha>` |
| Lab | `lab-image/` | `linuxlab/lab-base:<api sha>` |
| Edge | `infra/caddy/Dockerfile`: the frontend built with `npm ci` and `npm run build`, on Caddy | `linuxlab-edge:<api sha>-<web sha>` |

Each tree is a `git archive` of its commit, so untracked and ignored files of the checkouts (a `.env`, mission content copied by mistake) never reach a build context. The script refuses a commit whose `content/` holds anything but `.gitkeep`, then runs `infra/release/check-images.sh` on the three images, and writes a release file with the commits, tags and image ids:

```
API_SHA=...
API_IMAGE=linuxlab-api:<api sha>
API_IMAGE_ID=sha256:...
...
```

The Compose file takes the images from it, and the operation scripts refuse to run when a tag no longer points to the recorded id. Base images are pinned by digest in `infra/caddy/Dockerfile` and for PostgreSQL in `infra/compose.prod.yml`; the API's `Dockerfile` uses tags, so the recorded ids are what identifies a release.

`check-images.sh` runs the images and checks what they contain, not how they were built:

- no file of the mission layout anywhere (`mission.yaml`, `briefing.md`, `explanation.md`, `solution-*.sh`, `counter-*.sh`, a `linux-lab-content` directory), and the API's `content/` holds only `.gitkeep`;
- no tests in the API image;
- no secret-like variable (`PASSWORD`, `SECRET`, `TOKEN`, `INVITE`, `DATABASE_URL`) in the environment or the build history;
- the API runs as uid 10001 and its command pins one worker;
- the lab image has no setuid or setgid file and runs as the student;
- the edge image holds the frontend and a valid Caddyfile.

`infra/tests/check-images-detects.sh` builds contaminated copies (mission content in `/app/content`, a private checkout elsewhere, a solution script, a secret in the environment, a setuid file) and checks that each is refused. CI runs both on every change.

Mission content never enters an image. It is synced from a checkout of the private repository into PostgreSQL by a one-off task ([operations.md](operations.md#syncing-mission-content)).

## Configuration

| File | Purpose |
|---|---|
| `infra/compose.prod.yml` | The stack |
| `infra/caddy/Caddyfile` | Entry with the deployment's own domain: ACME certificate, HTTP to HTTPS redirect, HSTS |
| `infra/caddy/linuxlab.caddy` | The site, shared by every entry: static frontend, `/api/*` and `/ws/*` to the API, headers, access log |
| `infra/production.env.example` | Settings and secrets, copied to `/etc/linuxlab/production.env` (root, mode 0600) |
| `infra/release/check-env.sh` | Checks that file without printing values |
| `infra/docker/daemon.json.example` | The Docker daemon settings the stack expects: the `runsc` runtime and log rotation |

`daemon.json` is not installed by any script: a host may already have one, and it must be merged by hand (`runsc install` writes the runtime entry itself, see [runtime.md](runtime.md)). The Compose file sets log rotation for its own containers either way.

### Routes

| Path | Goes to |
|---|---|
| `/api/*`, `/ws/*` | The API. Request bodies over 64 KB are refused by Caddy |
| `/assets/*` | Files named by content hash, cached for a year (`immutable`) |
| Anything else | The file if it exists, otherwise `index.html` (client-side routes), with `Cache-Control: no-cache` |

The API's own documentation routes (`/docs`, `/openapi.json`, `/redoc`) are outside `/api`, so they reach the frontend's entry page, not the API.

### Timeouts and shutdown

Caddy's proxy has no response or stream timeout: replacing a lab takes up to about 100 seconds ([architecture.md](architecture.md#replacing-a-lab)), and a terminal WebSocket lasts as long as its lab allows (idle and lifetime limits are the API's). uvicorn pings WebSockets every 20 seconds.

On `docker compose stop` or a restart, uvicorn stops accepting connections, closes terminal WebSockets with 1012, which the frontend reconnects after (docs/api.md, Terminal), and waits up to 120 seconds (`--timeout-graceful-shutdown`) for requests in progress; Docker waits 130 seconds (`stop_grace_period`) before killing it. A lab replacement in progress therefore completes. Reloading Caddy's configuration closes WebSockets as well, with the same reconnection.

### Headers

Every response carries `Content-Security-Policy`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, `Cross-Origin-Opener-Policy: same-origin` and a restrictive `Permissions-Policy`, without a `Server` header; the domain entry adds `Strict-Transport-Security`.

```
default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:;
font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self';
frame-ancestors 'none'
```

Scripts come only from the site. Styles allow inline: xterm.js adds `<style>` elements for the terminal, which `style-src 'self'` blocks. This was checked in Chromium through Caddy with the whole student flow (sign-up, lab, terminal, validation, history, reset, end, logout): no violation with the policy above, and `style-src-elem` violations from the terminal with `style-src 'self'`.

### Logs

Caddy writes a JSON access log to standard output, with `Cookie`, `Set-Cookie`, `Authorization` and `Proxy-Authorization` logged as `REDACTED` and `client_ip` the address used for the rate limits. The API logs to standard output without request bodies, tokens or mission parameters ([architecture.md](architecture.md)). Docker keeps 5 files of 10 MB per container.

## Checking a release

`infra/tests/e2e.sh <release file>` starts the stack from the release's images as a disposable project (`linuxlab-e2e`, its own networks, volumes and lab label), with the synthetic missions of `tests/fixtures/content`, and checks it from clients on its public network, each with its own address, through Caddy only:

- the production Compose file: only Caddy publishes ports (80 and 443), the API has one worker and trusts only Caddy's address, `edge` and `db` are internal, only the API mounts the Docker socket;
- the site: entry page, headers, caching, client routes, no API documentation, the body limit, the HTTP redirect;
- the rate limit: separate budgets for separate clients, and a forged `X-Forwarded-For` (any address, another client's, Caddy's) neither escapes a limit nor charges another client; Caddy's access log shows the real addresses only;
- reachability: from the public network, neither the API (by name or address), PostgreSQL nor Caddy's admin endpoint; from the API, PostgreSQL but not the internet;
- one process in the API container, no `WEB_CONCURRENCY`, and a second replica refused;
- a student's session through Caddy: lab, terminal over `wss`, a command, validation, history, the `Origin` check and 4401 without a session, another user getting 404 and 4404, reset (with 4410 on the old terminal), mission switch, end, logout;
- the lab container: the configured runtime, no network, no mounts, read-only, not privileged; from inside, no Docker socket, only loopback, and none of the API, PostgreSQL, Caddy or the internet reachable;
- an API restart: the terminal is closed with 1012 and reopened on the same lab;
- logs: rotation, no invite code or database password in any log, cookies redacted;
- every lab container of the run removed by the API.

`--runtime runc` runs the labs under runc with `ENVIRONMENT=development`, for hosts without gVisor such as Docker Desktop; CI runs it under runsc (`Runtime` workflow).
