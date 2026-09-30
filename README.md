# linux-lab-api

Backend for Linux Lab, a platform for learning Linux by solving problems in a real terminal, inside an isolated environment created for each student.

This repository holds the API, the lab runtime, the WebSocket terminal, the lab image and the infrastructure. Mission content and validation will live here too. The web interface lives in [linux-lab-web](https://github.com/FranciscoPedro06/linux-lab-web).

## Why

Introductory Linux courses tend to teach one command at a time: read about `mkdir`, type `mkdir`, move on. That trains syntax recall, not the ability to fix something on a real system.

In Linux Lab the student gets a problem ("the deploy script is readable by every user, fix it"), a real shell, and freedom to solve it however they want. The platform checks the result, not the commands that were typed.

## How it works

What exists today:

1. A lab is an isolated container created from the lab image, with no network, a read-only root filesystem and CPU, memory and process limits. For now labs are created by hand with a development command.
2. The browser opens a terminal (xterm.js) connected over WebSocket to a `bash` shell inside that container, running as an unprivileged user.
3. The student works with whatever commands they prefer; nothing typed is parsed or filtered.
4. Accounts exist: sign-up with an invite code, login and logout, with a server-side session in an `HttpOnly` cookie. Labs are not tied to accounts yet.

Planned, not implemented yet: labs owned by users, missions with their initial state, validation of the final state, progress and resetting a lab.

Missions will declare conditions rather than run validation code:

```yaml
validation:
  all:
    - id: exists
      type: file_exists
      path: /home/student/deploy.sh
    - id: mode
      type: file_permissions
      path: /home/student/deploy.sh
      mode: "0700"
```

`chmod 700 deploy.sh`, `chmod u=rwx,go= deploy.sh`, or anything else that reaches the same state would pass.

## Architecture

This is the target design; the [Status](#status) section lists what is implemented.

```
Browser (linux-lab-web)
   | HTTPS / WSS
Caddy  (TLS, static files, proxy for /api and /ws)
   |
linux-lab-api  (FastAPI, single process)
   |-- PostgreSQL
   '-- Docker Engine (gVisor runtime)
          '-- one container per active lab
              no network, read-only rootfs, CPU/memory/PID limits
```

- Modular monolith. Auth, catalog, progress, labs, terminal, validation and history are modules of the same application.
- Each user has at most one active lab, enforced by a database constraint.
- The PTY is created inside the container through `docker exec` with a TTY. The API only forwards bytes.
- Validation runs in two phases. A platform utility baked into the read-only image collects facts inside the container, and the API evaluates those facts against the mission's condition tree.
- Missions are versioned in this repository and synced to the database as immutable versions.

Details in [docs/architecture.md](docs/architecture.md).

## Stack

| Area | Technology |
|---|---|
| API | Python 3.12, FastAPI, SQLAlchemy 2 (async), Alembic, Pydantic |
| Database | PostgreSQL 16 |
| Labs | Docker Engine, gVisor (`runsc`), `aiodocker` |
| Edge | Caddy |
| Passwords | Argon2id |
| Validation regex | RE2 |

No Redis, queue or orchestrator. PostgreSQL handles sessions, state and concurrency.

## Repository layout

```
src/linuxlab/  application package
migrations/    Alembic migrations
tests/         unit, integration, security and mission tests
content/       modules and missions (YAML, Markdown, setup and test scripts)
lab-image/     lab Docker image and platform utilities
infra/         Docker Compose, Caddy, Docker daemon configuration
docs/          architecture, API contract, mission format, threat model
```

Mission content (titles, briefings, hints, messages) is written in Portuguese. Code, identifiers and documentation are in English.

## Running

Requirements:

- Docker with Compose
- Python 3.12 and [uv](https://docs.astral.sh/uv/) for running tools outside containers

Labs run under gVisor (`runsc`) in production. Any Docker Engine, including Docker Desktop, is enough to develop and to run the lab tests under `runc`; testing under gVisor needs Linux with a native Docker Engine and is also done in CI. See [docs/runtime.md](docs/runtime.md).

### Full environment

Clone [linux-lab-web](https://github.com/FranciscoPedro06/linux-lab-web) next to this repository, then:

```sh
docker compose -f infra/compose.yml up --build
```

This starts PostgreSQL, the API with auto-reload and the Vite dev server, and applies migrations on startup. The API gets the Docker socket and the terminal without authentication (`DEV_TERMINAL_ACCESS`), which are for local development only.

Sign-up is disabled unless `SIGNUP_INVITE_CODE` is set in the environment that runs Compose, for example `SIGNUP_INVITE_CODE=<any value> docker compose -f infra/compose.yml up --build`; that value is then the invite code for the local sign-up page.

The session cookie is `Secure`. Browsers that treat `http://localhost` as a secure context, such as Chrome, Edge and Firefox, accept it there; a browser that does not will not keep the session on the local environment.

To open a terminal, build the lab image, create a lab and open the address it prints:

```sh
docker build --tag linuxlab/lab-base:dev lab-image
docker compose -f infra/compose.yml exec api python -m linuxlab.labs.devlab create
```

| Service | Address |
|---|---|
| Web | http://localhost:5173 |
| API | http://localhost:8000/api/health |
| PostgreSQL | `localhost:5432`, user, password and database `linuxlab` |

All ports are bound to `127.0.0.1`. If the web repository lives elsewhere, set `LINUXLAB_WEB_DIR`.

### API on the host

```sh
docker compose -f infra/compose.yml up -d postgres
cp .env.example .env
uv sync
uv run alembic upgrade head
uv run uvicorn --factory linuxlab.main:create_app --reload
```

Configuration comes from environment variables or `.env`; see [.env.example](.env.example).

## Tests

```sh
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest                  # unit tests
uv run pytest -m integration   # authentication, schema and migrations; requires PostgreSQL at DATABASE_URL
uv run pytest -m docker        # lab runtime, isolation and terminal, requires Docker and the lab image
```

Lab runtime tests, gVisor setup and the list of isolation checks are described in [docs/runtime.md](docs/runtime.md).

The integration tests empty the `users` and `auth_sessions` tables and run the migrations down and up again, so point `DATABASE_URL` at a development database. On Windows, use `127.0.0.1` rather than `localhost` in `DATABASE_URL`; each connection to `localhost` can wait about two seconds for IPv6 first.

CI runs the same checks, the integration tests against a PostgreSQL service, a migration downgrade and upgrade, and builds the API and lab images. A separate `Runtime` workflow runs the lab tests under gVisor.

Planned coverage as the project grows:

- **Unit:** validators, the validation tree, the mission parser, progress rules, authentication, and a snapshot of the container security configuration.
- **Integration:** lab creation, setup, validation, reset and terminal against real Docker and PostgreSQL.
- **Security:** access to another user's lab, unauthenticated WebSocket, invalid `Origin`, and checks run inside the container (non-root user, no capabilities, no network, no Docker socket, read-only rootfs, fork bomb containment).
- **Missions:** each mission declares solutions that must pass and counterexamples that must fail. A harness creates a fresh lab for each case.

## Status

Increments 01 to 04 are implemented: application skeleton, database connection, local environment and CI; the lab image and the lab runtime with isolation tests; the terminal, a WebSocket to a real shell in the lab; and accounts with server-side sessions. Lab sessions, ownership checks and lab cleanup come next (increment 05); until then the terminal is not tied to accounts and is only available in development. Planned order:

| # | Increment |
|---|---|
| 01 | Project scaffold and CI |
| 02 | Lab image and hardened Docker runtime |
| 03 | WebSocket terminal (development mode) |
| 04 | Database and authentication |
| 05 | Lab sessions, authorization and cleanup |
| 06 | Mission catalog and sync |
| 07 | Mission setup and parameters |
| 08 | Validation engine |
| 09 | Progress |
| 10 | Reset and mission switching |
| 11 | Mission test harness and first five missions |
| 12 | Command history |
| 13 | Closed beta deployment |

## Documentation

- [docs/architecture.md](docs/architecture.md): components, flows and decisions
- [docs/api.md](docs/api.md): HTTP endpoints and terminal protocol
- [docs/missions.md](docs/missions.md): mission format and authoring rules
- [docs/runtime.md](docs/runtime.md): lab runtime, gVisor setup and verification status
- [docs/terminal.md](docs/terminal.md): terminal protocol, lifecycle and limits
- [docs/threat-model.md](docs/threat-model.md): isolation, limits and accepted risks

## License

[MIT](LICENSE)
