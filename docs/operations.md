# Operations

How to install, update, check, back up and recover the production stack described in [deployment.md](deployment.md). Every script takes the release file written by `infra/release/build.sh` and refuses to run if an image tag no longer points to the image id recorded in it. They read `/etc/linuxlab/production.env` (`LINUXLAB_ENV_FILE` overrides it) and work on the Compose project `linuxlab-prod` (`LINUXLAB_PROJECT`).

| Script | Does |
|---|---|
| `infra/release/build.sh` | Builds and checks the release images, writes the release file |
| `infra/release/check-env.sh` | Checks the environment file, without printing values |
| `infra/ops/migrate.sh` | Backup, restore test of that backup, then `alembic upgrade head` |
| `infra/ops/content-sync.sh` | Syncs mission content from a checkout of the private repository at a given commit |
| `infra/ops/backup.sh` | Database dump and Caddy's data, read back in full, copied off the host; removes nothing |
| `infra/ops/restore.sh` | Restores a dump into a new database; drops it afterwards unless `--keep` |
| `infra/ops/check-labs.sh` | Compares lab containers with the database, checks disk, memory and the API's log; changes nothing |
| `infra/tests/e2e.sh` | The end-to-end checks on a disposable project ([deployment.md](deployment.md#checking-a-release)) |

## Decisions before the first deployment

None of these is made yet, and none is assumed by the configuration:

| Decision | Constraint from the stack |
|---|---|
| Provider and VM | Linux, x86_64 (the gVisor release used in CI), native Docker Engine 28 or later (loopback port publishing is reachable from the local network before 28.0), memory of about `LAB_CAPACITY` x 512 MB plus 2 GB for PostgreSQL, the API, Caddy and the host, and half a CPU per lab |
| Domain and DNS | An `A` record for the site. No `AAAA` record until IPv6 has been checked on the host ([deployment.md](deployment.md#client-addresses)) |
| SSH access | Keys only, no password or root login, from known addresses where the provider allows it |
| Firewall | The provider's firewall: 80 and 443 open, SSH restricted, everything else closed. Docker's published ports bypass a host firewall such as ufw, so the host firewall alone is not enough |
| Backup destination | Somewhere off the host, encrypted, with its own credentials ([Backups](#backups)) |
| Access to the private content | How the host gets a checkout of `linux-lab-content` ([Syncing mission content](#syncing-mission-content)). The beta must not open before this is decided and tested |
| `LAB_CAPACITY` | Sized to the chosen VM |

## Preparing the host

1. Install Docker Engine from Docker's repository and gVisor as in [runtime.md](runtime.md#environment) (same release and SHA-512 as `.github/workflows/runtime.yml`). Record `uname -r`, `docker version` and `runsc --version`.
2. Merge `infra/docker/daemon.json.example` into `/etc/docker/daemon.json` by hand and restart Docker. Check: `docker info --format '{{json .Runtimes}}'` lists `runsc`; `docker run --rm --runtime=runsc --network=none ubuntu:24.04 true` works; `docker info --format '{{.LoggingDriver}}'` is `json-file`.
3. Create the directories, owned by root: `/etc/linuxlab` (0700), `/var/lib/linuxlab/releases`, `/var/lib/linuxlab/content` (0700, the private checkout), `/var/backups/linuxlab` (0700).
4. Copy `infra/production.env.example` to `/etc/linuxlab/production.env` (0600), fill it in (it says how to generate each secret), and run `infra/release/check-env.sh /etc/linuxlab/production.env`. Secrets go only in that file: never on a command line, in a shell history or in the repository.
5. Configure the provider's firewall and DNS as decided.

## Releasing

1. Choose the commits: `linux-lab-api` and `linux-lab-web`, normally the tips of `main` with green CI, including the `Runtime` workflow (which runs the end-to-end checks under runsc).
2. Run the private missions workflow against the chosen API commit, and wait for both runtimes to pass: `gh workflow run missions.yml -R FranciscoPedro06/linux-lab-content -f api_sha=<API_SHA>`. Record the content commit it tested and the run. An earlier run against another API commit does not count.
3. On the host, from checkouts of both repositories at those commits or any later ones:
   ```sh
   infra/release/build.sh --api-repo /var/lib/linuxlab/src/linux-lab-api --api-sha <API_SHA> \
     --web-repo /var/lib/linuxlab/src/linux-lab-web --web-sha <WEB_SHA> --out /var/lib/linuxlab/releases
   ```
4. Optionally, `infra/tests/e2e.sh <release file>` on the host (its own project, networks and label; nothing of production is touched).

## Deploying

For the first deployment and every update. `R` is the release file.

1. `infra/ops/migrate.sh $R`: backs up, restores that backup into a new database to prove it, then migrates. With no pending migration it does nothing. The previous API keeps running meanwhile ([Migrations](#migrations)).
2. `infra/ops/content-sync.sh $R /var/lib/linuxlab/content/linux-lab-content <CONTENT_SHA>`, with the content commit recorded in step 2 of Releasing.
3. `docker compose -p linuxlab-prod -f infra/compose.prod.yml --env-file $R --env-file /etc/linuxlab/production.env up -d --wait`. The API is recreated (terminals close with 1012 and reconnect, a lab replacement in progress gets up to 120 s to finish), then Caddy. For the first deployment, set `ACME_CA` to the Let's Encrypt staging directory and check that a certificate is issued; then set it back and recreate Caddy, which gets a certificate from the production directory.
4. Acceptance on the real host, before telling testers anything:
   - `curl -sI https://<site>/` has the headers of [deployment.md](deployment.md#headers); `http://` redirects;
   - from outside, a port scan of the host shows 80 and 443 (and SSH from allowed addresses) only;
   - a test account: lab, terminal, validation, history, reset, mission switch, end, logout, in a browser;
   - during that session: `docker inspect --format '{{.HostConfig.Runtime}} {{.HostConfig.NetworkMode}}' ll-lab-<id>` is `runsc none`, and inside the lab `ls /var/run/docker.sock` fails and `ip -o link` lists only `lo`;
   - six failed logins from one address get `429`, and a login from a second address right after gets `401`;
   - `infra/ops/check-labs.sh $R` reports nothing.
5. Open the beta: send the invite code to the testers.
6. Keep the release file: it is what a rollback goes back to.

## Migrations

Production only moves forward: `alembic downgrade` is never run on the production database. Downgrades exist and are tested in CI on an empty database, but a downgrade that drops a table drops its data.

- Write migrations as expand and contract: first add (columns, tables, nullable or with defaults) in a release whose code works with and without them, then remove what is no longer used in a later release. The previous release then keeps working on the new schema, which is what makes a rollback of the application possible.
- A migration that rewrites or deletes data cannot be undone by the application. Before it, `migrate.sh` has taken a backup and restored it into a new database; if the new release is broken after such a migration, fix forward with a new release. Restoring the backup is the emergency procedure ([Restoring](#restoring)), and loses everything written since.
- A migration that fails part way stops `migrate.sh`; PostgreSQL runs each migration in a transaction, so check `alembic current` (shown by the script) before doing anything else.
- `migrate.sh` exits 0 only when the database ends at the release's head. It stops with Alembic's own error output when `alembic heads` or `alembic current` fails or prints anything but revision ids, when the release has more than one head (the history is linear) or the database is at more than one revision, and when the upgrade fails or leaves the database short of the head. A database with no revision yet is migrated. `infra/tests/migrate-cases.sh` checks each case with the docker CLI replaced by a stub.

## Syncing mission content

`content-sync.sh` runs `linuxlab content sync --content-dir /content` in a one-off container of the release's API image, with the checkout's `content/` mounted read-only, as the checkout's owner (root) without capabilities, and removes the container when it ends. It refuses a checkout that is not at the given commit or that has modified or untracked files. No running service mounts the content, and no image contains it: the sync writes it into PostgreSQL, where the API reads it ([architecture.md](architecture.md#missions-and-versioning)). Solutions and counterexamples are part of the stored versions and never leave the API ([api.md](api.md)).

How the host gets the checkout is pending. The options:

- a read-only deploy key of `linux-lab-content` on the host, root-owned, used only to fetch into `/var/lib/linuxlab/content`: simple, but a compromised host can read the private repository;
- the operator sends a bundle from their own machine (`git bundle create` of the commit, copied with `scp`, then `git clone` from the bundle on the host): no credential on the host.

Either way, only root reads the checkout, and the commit synced is the one the missions workflow tested.

## Backups

`backup.sh` writes, into `BACKUP_DIR` (0700, files 0600):

- `linuxlab-<time>-<id>-<label>.dump`: `pg_dump --format=custom` of the database, read back in full with `pg_restore -f /dev/null` (`pg_restore --list` reads only the table of contents and accepts a truncated dump), with a `.sha256`;
- `caddy-data-<time>-<id>-<label>.tar.gz`: Caddy's data volume (certificates and their private keys), checked with `gzip -t` and listed to its end with `tar -tzf`, with a `.sha256`.

`<time>` is UTC to the second and `<id>` 64 random bits, new for each run. Each file is written as `<name>.partial` and gets its final name only after that check, its checksum first: a file under its final name always has its `.sha256`. The `.sha256` names the file without its directory, so `sha256sum --check` works wherever the pair is copied. When a run fails or is interrupted, its partial files are removed; a `.partial` left by an earlier run (a crash, a reboot) is never a backup, and `backup.sh` reports it and leaves it for the operator.

A file never replaces another. Final names are given with a hard link (`ln`, the `link` system call), which fails when the name exists, in one step; if the dump's name turns out to be taken after its checksum was published, that checksum is removed again, and only while it is still this run's file. A backup already in `BACKUP_DIR` is never changed, whatever happens to a later run. `BACKUP_DIR` must be on a filesystem with hard links (any Linux filesystem; not FAT or some network mounts), or every backup fails.

Writing to disk: before the names are given, the content of the file and of its checksum is written to disk (`sync` of the two files, an `fsync` of each); after, the directory is (`sync` of the directory), which makes the new names durable. Once `backup.sh` has reported a file, a power loss or crash does not take it back. A crash before that point leaves, at worst, partial files or a checksum without its file, or loses the new names altogether; a final name, once it survives, always points to content already on disk. `restore.sh` refuses a partial file and a dump without its checksum, so a crash costs that backup, never an earlier one, and never makes a damaged file pass for a backup. This relies on the disk honoring flushes (a write cache without power protection that ignores them defeats any `fsync`).

It runs `BACKUP_COPY_COMMAND` on each file to copy it off the host, and reports when that is not set or fails (exit status other than 0). Its exit status is all that is known of the copy: nothing checks that the file arrived intact. The command runs without the lock's file descriptor, so a process it leaves running (an ssh master connection, a daemon) does not keep the lock after `backup.sh` ends. `migrate.sh` takes a backup labeled `pre-migrate-<api commit>` before every migration.

**No backup is removed automatically.** Removal by age is suspended until copies off the host are verified (download and checksum comparison), so that the last good backup on the host is never removed on the strength of a copy command's exit status. Every run reports how many backup files `BACKUP_DIR` holds; the operator removes old ones by hand after checking that a copy of a newer backup is intact off the host. `BACKUP_KEEP_DAYS` is not applied.

`backup.sh`, `restore.sh` and `migrate.sh` take the same lock (`flock` on `/run/linuxlab-ops.lock`) without waiting: while one runs, the others stop at once with exit status 1 and change nothing. `migrate.sh` holds the lock for its whole run, and the `backup.sh` and `restore.sh` it starts run under it (they inherit it). `flock` comes with util-linux on the host.

The lock file of the production project (`linuxlab-prod`) is fixed: no setting changes it. The tests run the scripts on projects of their own, with their own containers and database, and use two variables that only those projects accept: `LINUXLAB_TEST_LOCK_FILE` (a lock file in the test's directory) and `LINUXLAB_TEST_BACKUP_RUN` (a fixed `<time>-<id>`, to make two runs collide). With the production project, either one makes every operation script stop before doing anything. Neither belongs in the environment file or a cron line.

Cron on the host, as root. A failure, including a backup refused because another operation held the lock, goes to the host's syslog through `logger`:

```
15 3 * * *  root  /var/lib/linuxlab/src/linux-lab-api/infra/ops/backup.sh <release file> >>/var/log/linuxlab-backup.log 2>&1 || logger -p user.err -t linuxlab "backup failed, see /var/log/linuxlab-backup.log"
```

This only records the failure on the host. Getting it to the operator (mail, a monitoring service) is part of provisioning the host and is not set up by this repository.

What the backups hold: accounts (email, display name, Argon2id hashes), session token hashes, labs, validation runs, progress, mission versions (with solutions) and the **command history, which stores what students typed, secrets included, with no redaction** ([architecture.md](architecture.md#privacy)). So:

- backups are encrypted at the destination and readable only by the operator; their credentials are not the host's;
- retention is to be decided with the destination (proposal: 30 days off the host); on the host nothing is removed automatically for now (see above);
- the history is kept as long as the lab's row, which is removed only with its user: there is no retention job for it yet. Deleting a user's data means deleting the user, and backups keep it until they expire;
- the history is not a place for secrets, and the web page tells students so; operators must not present it otherwise.

## Restoring

Test a backup regularly (monthly, and after every change to the database):

```sh
infra/ops/restore.sh $R /var/backups/linuxlab/linuxlab-<time>-<id>-scheduled.dump
```

It restores into a new database `restore_<time>` on the same PostgreSQL, never into an existing one, prints the row counts next to the database in use, and drops it. Before creating anything it requires the dump's `.sha256` next to it and matching, and reads the dump in full; a dump without its checksum, or one that fails either check, is refused. The only optional argument is `--keep`; any other is refused before anything is done.

Emergency, when the database in use cannot be kept (a migration that cannot be fixed forward, corruption):

1. Stop the entry: `docker compose ... stop caddy`.
2. `infra/ops/restore.sh $R <dump> --keep`, and note the database name it prints.
3. Set `DATABASE_NAME=<that name>` in the environment file and recreate the API with the release that matches the dump's schema: `docker compose ... --env-file <that release> up -d --wait api`.
4. `infra/ops/check-labs.sh`: labs created after the dump have containers but no row, and the reaper removes them as orphans; labs the dump records as unfinished whose containers are gone are ended by the reaper as `container_lost`. Unidentified containers are reported and left alone.
5. Start Caddy again. The old database stays untouched until it is decided what to do with it.

This is not a rollback without loss: **everything written after the dump is gone**, accounts, sessions, progress, validation runs and history included, and students lose the labs they had.

## Checking labs

`check-labs.sh` lists this deployment's lab containers (by the `linuxlab.deployment` label) and the unfinished labs in the database, and reports:

| Finding | Meaning | Who acts |
|---|---|---|
| Orphan container | A lab container with no unfinished lab | The reaper removes it within 30 s; if it stays, the reaper is failing |
| Unidentified container | This deployment's label, but not named and labeled as a lab the API created | A person: inspect it, it is never removed automatically |
| Ready lab without a running container | The container died or was removed | The reaper ends the lab (`oom` or `container_lost`) |
| Provisioning for more than 3 minutes | Creation never finished | The reaper fails it after 2 minutes; if it stays, the reaper is failing |
| Terminating or failed for more than 3 minutes | Removal is not completing | Look at the API's log (`lab removal failed`) and at Docker |
| API not healthy, reconciliation errors in the last hour | The reaper may not be running | Look at the API's log |
| Disk over 85 %, less than 512 MB of memory available | No room for another lab | Lower `LAB_CAPACITY` or free space |

It never removes or changes anything, and exits 1 when something needs attention. Cron, every 10 minutes:

```
*/10 * * * *  root  /var/lib/linuxlab/src/linux-lab-api/infra/ops/check-labs.sh <release file> >>/var/log/linuxlab-labs.log 2>&1 || logger -t linuxlab "check-labs needs attention"
```

The reaper's guarantees are in [architecture.md](architecture.md#reaper-and-reconciliation): every step is idempotent and retried every 30 seconds, so cleanup is eventual, not immediate. A lab whose removal keeps failing stays `terminating` and keeps its place in `LAB_CAPACITY` until it is removed.

Investigating: `docker compose ... logs --since 1h api | grep lab=<id>`; `docker ps -a --filter label=linuxlab.lab_id=<key>`; `docker inspect ll-lab-<key>`. Removing a container by hand is safe only for a container `check-labs.sh` reports as an orphan for longer than a few minutes: `docker rm -f ll-lab-<key>`.

## Rolling back the application

Go back to an earlier release file, if its code works with the current schema (it does when the migrations since then followed expand and contract; compare `alembic heads` of the old image with `alembic current`):

```sh
docker compose -p linuxlab-prod -f infra/compose.prod.yml --env-file <earlier release file> \
  --env-file /etc/linuxlab/production.env up -d --wait
```

This replaces the API, Caddy and the frontend together; labs keep running and terminals reconnect. Labs started after the rollback use the earlier lab image; running labs keep theirs. The earlier images must still be on the host: do not prune images of the last releases. If the earlier code does not work with the current schema, fix forward, or restore as above, with its loss.

## Logs

`docker compose ... logs <service>`; Docker keeps 5 files of 10 MB per container. Caddy's access log is JSON with `client_ip`, method, URI, status and duration, and credentials redacted. The API logs lab and terminal events with lab ids, never tokens, passwords, parameters or history lines. Logs are read by the operator only, on the host.

## Contingency: entry through ngrok

Only when the conditions in [deployment.md](deployment.md#entry-through-ngrok) are met: account and plan chosen, the forged-header checks passed through the real tunnel, testers told that ngrok handles their traffic in clear.

1. Reserve or note the ngrok domain. Set `SITE_ADDRESS` to it, `PUBLIC_ORIGIN` and `NGROK_URL` to `https://<domain>` in `/etc/linuxlab/production.env`.
2. Copy `infra/ngrok/ngrok.env.example` to `/etc/linuxlab/ngrok.env` (root, 0600) with an authtoken made for this agent and restricted to that domain. Run `infra/release/check-env.sh /etc/linuxlab/production.env /etc/linuxlab/ngrok.env`.
3. Deploy as above, adding the override to every Compose command and script:
   ```sh
   export LINUXLAB_OVERRIDES=$PWD/infra/compose.ngrok.yml
   docker compose -p linuxlab-prod -f infra/compose.prod.yml -f infra/compose.ngrok.yml \
     --env-file $R --env-file /etc/linuxlab/production.env up -d --wait
   ```
   Caddy then publishes no port; the host needs no inbound port but SSH.
4. Acceptance as above, through the ngrok domain, including six failed logins from one address and a login from another.
5. Going back to the own domain: set the settings back, run `docker compose ... -f infra/compose.prod.yml up -d --wait --remove-orphans` without the override (removes the agent), and revoke the authtoken in ngrok.

## Incidents

| Situation | First steps |
|---|---|
| The site does not answer | `docker compose ... ps`; `curl -s http://127.0.0.1:2019/config/` inside Caddy; the API's health check |
| Certificate errors | Caddy's log (ACME errors); DNS still points to the host; ports 80 and 443 reachable; staging directory left in `ACME_CA` |
| API refuses to start | Its log: `refusing to start` means Docker or runsc is not available ([runtime.md](runtime.md)) |
| Labs fail to start | `lab_start_failed` and setup errors in the API's log; `LAB_CAPACITY` reached; disk and memory (`check-labs.sh`) |
| Suspected compromise of the API or a lab escape | Stop the stack (`docker compose ... stop`), keep the host as it is for investigation, do not restore over it. The API has root-equivalent access to the host through the Docker socket ([threat-model.md](threat-model.md#accepted-risks)) |
