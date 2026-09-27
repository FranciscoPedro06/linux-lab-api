# Lab runtime

The lab runtime creates and controls the containers students work in. It is not connected to the HTTP API yet; it is exercised by tests.

```
application code
   |
LabRuntime (protocol)          src/linuxlab/labs/runtime/base.py
   |-- DockerRuntime           aiodocker -> Docker Engine -> runsc (gVisor) -> container
   '-- FakeRuntime             in memory, for unit tests
```

## Interface

| Operation | Behavior |
|---|---|
| `ping()` | Fails with `RuntimeUnavailableError` if Docker is unreachable or the configured OCI runtime is not registered |
| `create(spec)` | Creates `ll-lab-<lab_id>` with the isolation settings from `spec.py`; does not start it |
| `start(id)`, `stop(id)` | Start, or stop with a 2 second grace period |
| `inspect(id)` | Name, running state and `linuxlab.*` labels |
| `exec(id, argv, user, time_limit)` | Runs argv without a shell as `student` or `root` |
| `remove(id)` | Force-removes the container; a missing container is not an error |

`exec` details:

- `argv` is passed to Docker as a list; nothing goes through a shell unless the caller runs one.
- The command runs under coreutils `timeout`, which sends SIGTERM to the whole process group at `time_limit` and SIGKILL 2 seconds later. The client stops waiting 5 seconds after that.
- `timed_out` is set when `timeout` reports expiry (exit code 124, or 137 after the deadline). A command that itself exits with 124 is indistinguishable.
- stdout and stderr are captured separately, up to 1 MiB each; `truncated` is set if more was produced.
- The environment is fixed (`PATH`, `LANG`). The working directory is `/home/student` for the student and `/` for root.

## Container configuration

Defined in one function, `build_container_config`, and covered by a snapshot test. The values and their reasons are in [threat-model.md](threat-model.md#container-configuration).

The lab image is built from `lab-image/`: Ubuntu 24.04 pinned by digest, `bash`, `python3`, `tini`, `procps`, `iproute2`, user `student` (uid 1000), no `sudo`, no setuid or setgid binaries, and `/opt/labctl` reserved for platform utilities (root only, currently empty). The entrypoint is `tini -- sleep infinity`.

`/home/student` is a tmpfs, so it starts empty; populating it is left to `labctl init`, which does not exist yet.

## Environment

- Any Docker Engine runs the tests under `runc`.
- gVisor requires Linux with a native Docker Engine. Docker Desktop cannot register `runsc`, so on Windows and macOS gVisor is only tested in CI.

Installing gVisor on a Linux host (the same steps as `.github/workflows/runtime.yml`):

```sh
release=20260921.0
curl -fsSLo gvisor.tar.bz2 \
  "https://storage.googleapis.com/gvisor/releases/release/${release}/x86_64/gvisor.tar.bz2"
curl -fsSL "https://storage.googleapis.com/gvisor/releases/release/${release}/x86_64/gvisor.tar.bz2.sha512" \
  | sha512sum --check
sudo tar -xjf gvisor.tar.bz2 -C /usr/local/bin runsc containerd-shim-runsc-v1 gvisor-bin
sudo runsc install
sudo systemctl restart docker
```

The archive includes `gvisor-bin/`, which `runsc` expects next to its own binary.

Checking that Docker really uses it:

```sh
docker info --format '{{range $name, $_ := .Runtimes}}{{$name}} {{end}}'   # lists runsc
docker run --rm --runtime=runsc --network=none ubuntu:24.04 dmesg         # gVisor boot messages
```

## Tests

```sh
docker build --tag linuxlab/lab-base:dev lab-image

uv run pytest                                   # unit tests, no Docker
uv run pytest -m docker                         # integration and isolation under runc
LINUXLAB_TEST_OCI_RUNTIME=runsc uv run pytest -m "docker or runsc"
```

`LINUXLAB_LAB_IMAGE` selects another image tag. Tests marked `runsc` are skipped, with the reason shown, unless the runtime is `runsc`.

The `Runtime` workflow installs gVisor on an Ubuntu runner and runs the last command. It runs on pushes to `main`, on pull requests that touch the runtime, the lab image or the workflow, and on demand. It is not a required check yet.

## Verification status

Every check runs commands inside a real lab and asserts what was allowed, not what was configured.

| Check | runc (Docker Desktop, WSL2 kernel 6.18) | runsc (GitHub Actions) |
|---|---|---|
| Student is uid 1000, PID 1 is `tini` running as the student | passed | not run yet |
| Student `CapInh/CapPrm/CapEff/CapAmb` = 0, `NoNewPrivs` = 1 | passed | not run yet |
| Root exec has exactly `CHOWN, DAC_OVERRIDE, FOWNER, KILL` (effective and bounding) | passed | not run yet |
| Each of the four capabilities is required for its operation (dropped: denied; kept: allowed) | passed | not run yet |
| No setuid/setgid binaries, no `sudo`, `su` fails | passed | not run yet |
| Rootfs read-only, including for root | passed | not run yet |
| Student writes only to `/home/student`, `/tmp`, `/run/lab`; `/opt/labctl` closed | passed | not run yet |
| tmpfs uid, gid, mode, size, `nosuid,nodev`; home capped at 64 MB | passed | not run yet |
| Only `lo`; no TCP to public addresses or metadata endpoint; no DNS | passed | not run yet |
| No Docker socket, no host mounts, only expected mount points | passed | not run yet |
| `/etc/hosts`, `/etc/hostname`, `/etc/resolv.conf` not writable, even by root | passed | not run yet |
| PID limit stops a fork loop below 128; container survives | passed | not run yet |
| 256 MB allocation succeeds, 768 MB is OOM-killed (137); container survives | passed | not run yet |
| Busy loop gets about 0.5 CPU | passed (0.50) | not run yet |
| Seccomp filter active | passed (mode 2) | reported, not asserted |
| Container runs under gVisor (`HostConfig.Runtime`, `dmesg`) | skipped | not run yet |
| Create and start / exec latency | 0.20 s / 0.06 s | not run yet |

## Known limitations

- Under gVisor the Docker seccomp profile is not what filters the student's syscalls; the test records the reported value instead of asserting it.
- The CPU check measures one busy thread for 3 seconds. It shows the quota is applied, not how it behaves under contention.
- What happens to an exec'd process when its client disconnects is left for the terminal (increment 03), which uses TTY execs.
