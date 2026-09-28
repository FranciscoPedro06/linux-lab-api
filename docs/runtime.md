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

`/home/student` is a tmpfs, so it starts empty; populating it is left to `labctl init`, which does not exist yet. Docker mounts tmpfs `noexec` by default, so the configuration sets `exec` on `/home/student` and `/tmp`, where students run their own scripts, and keeps `noexec` on `/run/lab`.

## Differences between runc and runsc

The same configuration gives different observable behavior under the two runtimes. The tests encode the expected behavior for each one.

| Area | runc | runsc |
|---|---|---|
| Process limit | `pids_limit=128` on the container cgroup; `fork` fails with EAGAIN | The cgroup limit covers the sandbox's host threads (about two per student process plus ~30), and reaching it kills the sandbox; it is set to 512 as host protection. The limit students hit is `nproc=128` (RLIMIT_NPROC), enforced by gVisor per sandbox; `fork` fails with EAGAIN |
| `nproc` | Not used: it would count uid 1000 across every lab on the host | Counted inside each sandbox; two labs have independent budgets |
| Memory limit exceeded | The OOM killer kills the offending process; the lab keeps running | The memory cgroup covers the whole sandbox; the host OOM killer ends it and the lab stops (`OOMKilled=true`) |
| Memory per forked process | Copy-on-write; 100 forked Python processes use ~32 MB | ~5 MB each; 100 forked Python processes use ~500 MB, so a Python fork loop runs out of memory before `nproc` |
| CPU accounting inside the lab | `process_time()` matches the host | gVisor's accounting does not see host throttling (~0.9 for a throttled loop); only host cgroup statistics are reliable |
| Network with `NetworkDisabled` | `lo` still present | No interfaces at all; the configuration uses only `NetworkMode: none`, which keeps `lo` |
| tmpfs `nodev` | Reported in `/proc/mounts` | Not reported; device creation is still refused because no capability allows `mknod` |
| Seccomp in `/proc/self/status` | 2 (Docker default profile) | 0; syscalls go to the gVisor kernel, which is filtered on the host by gVisor |

In both runtimes the effect of exhausting a resource stays inside the lab that did it; the tests check that another lab keeps working.

The per-process memory and host thread figures were measured with runsc in a local Docker-in-Docker setup used for diagnosis; the behaviors themselves are covered by the `Runtime` workflow.

## Environment

- Any Docker Engine runs the tests under `runc`.
- gVisor requires Linux with a native Docker Engine. Docker Desktop cannot register `runsc`, so on Windows and macOS gVisor is only tested in CI.
- `aiodocker` picks the active Docker context before `DOCKER_HOST`, unlike the Docker CLI. When pointing the API or the tests at another daemon, set `DOCKER_CONTEXT=default` together with `DOCKER_HOST`, or make sure no other context is active.

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

`LINUXLAB_LAB_IMAGE` selects another image tag. The pytest header shows the runtime and image in use (`lab runtime: runsc, lab image: ...`). Tests marked `runsc` are skipped, with the reason shown, unless the runtime is `runsc`.

The `Runtime` workflow installs gVisor on an Ubuntu runner and runs the last command. It runs on pushes to `main`, on pull requests that touch the runtime, the lab image or the workflow, and on demand. It is not a required check yet.

## Verification status

Every check runs commands inside a real lab and asserts what was allowed, not what was configured. runc results are from Docker Desktop (WSL2 kernel 6.18); runsc results are from the `Runtime` workflow on GitHub Actions (Ubuntu 24.04 runner, gVisor `release-20260921.0`, default platform).

| Check | runc | runsc |
|---|---|---|
| Runtime in use | `HostConfig.Runtime=runc` | `HostConfig.Runtime=runsc`; `dmesg` shows `Starting gVisor...` |
| Student is uid 1000, PID 1 is `tini` running as the student | passed | passed |
| Student `CapInh/CapPrm/CapEff/CapAmb` = 0, `NoNewPrivs` = 1 | passed | passed |
| Root exec has exactly `CHOWN, DAC_OVERRIDE, FOWNER, KILL` (effective and bounding) | passed | passed |
| Each of the four capabilities is required for its operation (dropped: denied; kept: allowed) | passed | passed |
| No setuid/setgid binaries, no `sudo`, `su` fails | passed | passed |
| Rootfs read-only, including for root | passed | passed |
| Student writes only to `/home/student`, `/tmp`, `/run/lab`; `/opt/labctl` closed | passed | passed |
| tmpfs uid, gid, mode and size; home capped at 64 MB | passed | passed |
| tmpfs `rw,nosuid`; `exec` on home and `/tmp`, `noexec` on `/run/lab` | passed | passed |
| tmpfs `nodev` in mount options | passed | not reported (see differences) |
| Scripts run from home and `/tmp`, refused from `/run/lab` | passed | passed |
| `mknod` refused for root and student in home and `/tmp` | passed | passed |
| Only `lo`; localhost TCP works; no TCP to public addresses or metadata endpoint; no DNS | passed | passed |
| No Docker socket, no host mounts, only expected mount points | passed | passed |
| `/etc/hosts`, `/etc/hostname`, `/etc/resolv.conf` not writable, even by root | passed | passed |
| Process limit: `fork` fails with EAGAIN below 128, lab keeps working | passed (cgroup PID limit) | passed (`nproc`) |
| A second lab has its own process budget | passed | passed |
| Python fork loop contained, other lab unaffected | EAGAIN at 124 children, lab running | memory exhausted first, lab OOM-killed (exit 137) |
| 256 MB allocation succeeds; 768 MB exceeds the limit; other lab unaffected | process killed (137), lab running | lab OOM-killed (`OOMKilled=true`) |
| CPU quota, from host cgroup statistics during a 5 s busy loop | 0.51 CPU, 50 throttled periods | 0.51 CPU, 50 throttled periods (in-lab ratio 0.90, not used) |
| Seccomp in `/proc/self/status` | 2 | 0, reported and not asserted |
| Create and start / exec latency | 0.19 s / 0.06 s | 0.12 s / 0.09 s |

## Known limitations

- Under gVisor the Docker seccomp profile is not what filters the student's syscalls; the test records the reported value instead of asserting it.
- Under gVisor a lab that exceeds its memory stops, including when many heavy processes are forked. The host and other labs are unaffected, but the student loses the environment. Detecting this and telling the student is left for the lab lifecycle work (increment 05).
- The process limit under gVisor relies on `nproc` being reached before the sandbox's 512 host threads. This holds for 128 student processes (about 290 host threads observed), with margin for platform execs.
- The CPU check measures one busy thread for 5 seconds. It shows the quota is applied, not how it behaves under contention.
- What happens to an exec'd process when its client disconnects is left for the terminal (increment 03), which uses TTY execs.
