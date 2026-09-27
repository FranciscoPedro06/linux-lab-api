# linux-lab-api

Backend for Linux Lab, a platform for learning Linux by solving problems in a real terminal, inside an isolated environment created for each student.

This repository holds the API, lab lifecycle management, the WebSocket terminal, mission validation, mission content, the lab image and the infrastructure. The web interface lives in `linux-lab-web`.

## Why

Introductory Linux courses tend to teach one command at a time: read about `mkdir`, type `mkdir`, move on. That trains syntax recall, not the ability to fix something on a real system.

In Linux Lab the student gets a problem ("the deploy script is readable by every user, fix it"), a real shell, and freedom to solve it however they want. The platform checks the result, not the commands that were typed.

## How it works

1. The student picks a mission and starts the lab.
2. The API creates an isolated container, runs the mission setup and leaves the environment in the problem's initial state.
3. The browser opens a terminal (xterm.js) connected over WebSocket to a `bash` shell inside the container.
4. The student solves the problem with whatever commands they prefer.
5. On validation, the API inspects the container state (files, permissions, processes) and compares it against the conditions declared by the mission.
6. Progress is stored. The environment is disposable and can be recreated at any time with Reset.

A mission declares conditions; it does not run validation code:

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

`chmod 700 deploy.sh`, `chmod u=rwx,go= deploy.sh`, or anything else that reaches the same state passes.

## Architecture

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
content/     modules and missions (YAML, Markdown, setup and test scripts)
lab-image/   lab Docker image and platform utilities
infra/       Docker Compose, Caddy, Docker daemon configuration
tests/       unit, integration, security and mission tests
docs/        architecture, API contract, mission format, threat model
```

Mission content (titles, briefings, hints, messages) is written in Portuguese. Code, identifiers and documentation are in English.

## Running

There is no code yet. Setup instructions will be added with the first increment.

Expected development requirements:

- Linux or WSL2 with a native Docker Engine (Docker Desktop does not allow installing gVisor)
- Python 3.12
- gVisor (`runsc`), required in production and optional in development

Configuration is done through environment variables; see [.env.example](.env.example).

## Tests

- **Unit:** validators, the validation tree, the mission parser, progress rules, authentication, and a snapshot of the container security configuration.
- **Integration:** lab creation, setup, validation, reset and terminal against real Docker and PostgreSQL.
- **Security:** access to another user's lab, unauthenticated WebSocket, invalid `Origin`, and checks run inside the container (non-root user, no capabilities, no network, no Docker socket, read-only rootfs, fork bomb containment).
- **Missions:** each mission declares solutions that must pass and counterexamples that must fail. A harness creates a fresh lab for each case.

## Status

The architecture is defined and implementation has not started. Planned order:

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
- [docs/threat-model.md](docs/threat-model.md): isolation, limits and accepted risks

## License

[MIT](LICENSE)
