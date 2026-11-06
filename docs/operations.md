# Operations

How to install, update, check, back up and recover the production stack described in [deployment.md](deployment.md). Every script takes the release file written by `infra/release/build.sh` and refuses to run if an image tag no longer points to the image id recorded in it. They read `/etc/linuxlab/production.env` (`LINUXLAB_ENV_FILE` overrides it) and work on the Compose project `linuxlab-prod` (`LINUXLAB_PROJECT`).

| Script | Does |
|---|---|
| `infra/release/build.sh` | Builds and checks the release images, writes the release file |
| `infra/release/check-env.sh` | Checks the environment file, without printing values |
| `infra/ops/migrate.sh` | Backup, restore test of that backup, then `alembic upgrade head` |
| `infra/ops/content-sync.sh` | Syncs mission content from a checkout of the private repository at a given commit |
| `infra/ops/backup.sh` | One package: database dump, Caddy's data and a manifest, each read back in full; encrypted; copied off the host and read back there; removes nothing |
| `infra/ops/sync-backups.sh` | Copies off the host the backups that have no receipt yet, without a new dump |
| `infra/ops/prune-backups.sh` | Removes old local backups whose copy off the host it reads back first; disabled unless configured |
| `infra/ops/backup-status.sh` | Reports the copies off the host: latest receipt, its age, pending backups, failures; changes nothing |
| `infra/ops/restore.sh` | Restores a package's database into a new database and compares it with the manifest; drops it afterwards unless `--keep` |
| `infra/ops/restore-offsite.sh` | On a new host: finds the newest backup confirmed off the host, checks and decrypts it, rebuilds the release from its commits, restores PostgreSQL and Caddy's data, starts and checks the stack ([Disaster recovery](#disaster-recovery)) |
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
| Backup destination | S3-compatible storage off the host, reached with rclone: provider, bucket, a credential limited to create and read under one prefix, versioning or Object Lock, lifecycle rules ([Copies off the host](#copies-off-the-host)); and a second credential, for recovery, limited to list and read that prefix, kept by the operator ([Disaster recovery](#the-recovery-credential)) |
| Local retention | Days a backup stays on the host once its copy off the host is confirmed; none until decided ([Local retention](#local-retention)) |
| Access to the private content | How the host gets a checkout of `linux-lab-content` ([Syncing mission content](#syncing-mission-content)). The beta must not open before this is decided and tested |
| `LAB_CAPACITY` | Sized to the chosen VM |

## Preparing the host

1. Install Docker Engine from Docker's repository, `python3` (3.10 or later: the operation scripts build and check backup packages with it), age with `sudo infra/release/install-age.sh` (the release the scripts are tested with, its archive checked against the SHA-256 in `infra/ops/age.sh`; [Backups](#backups)), rclone with `sudo infra/release/install-rclone.sh`, and gVisor with `sudo infra/release/install-gvisor.sh` (the release CI tests, its archive checked against the recorded SHA-512; [runtime.md](runtime.md#environment)). Turn swap off or encrypt it ([Memory and swap](#memory-and-swap)). Record `uname -r`, `docker version` and `runsc --version`.
2. Merge `infra/docker/daemon.json.example` into `/etc/docker/daemon.json` by hand and restart Docker. Check: `docker info --format '{{json .Runtimes}}'` lists `runsc`; `docker run --rm --runtime=runsc --network=none ubuntu:24.04 true` works; `docker info --format '{{.LoggingDriver}}'` is `json-file`.
3. Create the directories, owned by root: `/etc/linuxlab` (0700), `/var/lib/linuxlab/releases`, `/var/lib/linuxlab/content` (0700, the private checkout), `/var/backups/linuxlab` (0700).
4. Write `/etc/linuxlab/backup-recipients.txt` (root, 0644 or stricter) with the two public keys backups are encrypted to ([Encryption](#encryption)). Copy `infra/production.env.example` to `/etc/linuxlab/production.env` (0600), fill it in (it says how to generate each secret), and run `infra/release/check-env.sh /etc/linuxlab/production.env`. Secrets go only in that file: never on a command line, in a shell history or in the repository.
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

1. `infra/ops/migrate.sh $R`: backs up, restores that backup into a new database to prove it (before it is encrypted: the host holds no key to decrypt it), then migrates. With no pending migration it does nothing. The previous API keeps running meanwhile ([Migrations](#migrations)).
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

Each sync that succeeds is recorded in the database, in the same transaction (`content_syncs`): when it ran, the commit `content-sync.sh` checked the checkout against (it refuses a checkout at another commit, or with changed, untracked or ignored files in `content/`), and the missions' fingerprint right after it. A backup names that commit as the content's only while the missions are still what that sync left ([Backups](#backups)). A sync run by hand with `linuxlab content sync` records no commit unless given `--source-commit`.

## Backups

`backup.sh` writes one encrypted package into `BACKUP_DIR` (0700, files 0600): `linuxlab-<time>-<id>-<label>.tar.age`, an uncompressed tar archive encrypted with age ([Encryption](#encryption)), and `linuxlab-<time>-<id>-<label>.tar.age.sha256`, the SHA-256 of the encrypted bytes. Nothing unencrypted is ever written to `BACKUP_DIR`. The package, once decrypted, holds, as regular files and nothing else:

| Path | Content | Checked before packaging |
|---|---|---|
| `manifest.json` | What was captured, from what, and how to restore it | Its shape, by `infra/ops/package.py` |
| `database/postgres.dump` | `pg_dump --format=custom` of the database | Read back in full with `pg_restore -f /dev/null` (`pg_restore --list` reads only the table of contents and accepts a truncated dump) |
| `caddy/caddy-data.tar.gz` | Caddy's data volume: certificates and their private keys. Absent when the deployment has no such volume | `gzip -t`, and `tar -tzf` listing it to its end |

`infra/ops/package.py` (Python 3.10 or later, standard library only, run with the host's `python3`) builds the package and checks it: exactly `manifest.json` and the components the manifest lists, each once, no absolute path, no `..`, nothing but regular files, the tar archive's end where it should be, and each component's size and SHA-256 as the manifest records them. `backup.sh` checks the package that way before publishing it, and `restore.sh` before using it. The package's own SHA-256 is in its `.sha256`, not in the manifest, which would have to hash itself.

The manifest (`format_version` 1) records:

- the backup: its id (`<time>-<id>`), UTC time and label;
- the code: the API and web commits of the release file, whose image ids `load_release` checks against the local images; each image's `org.opencontainers.image.revision` label, which must match (`backup.sh` stops otherwise); and the image of the running API and Caddy containers, when they run;
- the private content: the number of missions and a fingerprint of them (slug, status, current version and that version's `content_hash`, which a checkout of `linux-lab-content` can be checked against), and the commit of the last sync recorded in `content_syncs` with the time of that sync, when that sync named one and the missions are still what it left; otherwise no commit (`code.content.sha` null) and, in `code.content.source`, why: no `content_syncs` table yet, no sync since, a sync without a commit, or missions changed since. Backups made before this record existed have no commit either;
- PostgreSQL: the database's name, the server's version, `pg_dump`'s version, the Alembic revision, a fingerprint of the schema (columns, constraints, indexes, triggers, functions and sequences, without owners), and the roles;
- each component's path, type, size and SHA-256;
- what a restore needs: PostgreSQL of the dump's major version or later, the release images, the role `linuxlab`; and what the operator provides separately: `/etc/linuxlab/production.env`, a checkout of `linux-lab-content`, the release file or its commits.

Left out, on purpose:

- PostgreSQL's globals (roles, their passwords and grants). The only role is `linuxlab`, a superuser the postgres image creates from `POSTGRES_USER` when its volume is initialised; the dump is restored with `--no-owner` and grants nothing to other roles. `backup.sh` stops when the database has any other role, since the package would then need the globals.
- Caddy's `/config` volume. It holds only `autosave.json`, the Caddyfile adapted to JSON, which Caddy rewrites at every start; Caddy starts from the Caddyfile in the release's edge image (`caddy run --config /etc/caddy/Caddyfile`), never from `autosave.json`.
- Settings and secrets: the environment file, ngrok's authtoken, any credential of a copy destination.

Nothing the manifest records changes while `backup.sh` runs: migrations and content syncs take the same lock, and the API changes neither the schema nor the missions.

`<time>` is UTC to the second and `<id>` 64 random bits, new for each run. The components and the unencrypted package exist only in a run directory of the work directory ([Encryption](#encryption)); the encrypted package is written to `BACKUP_DIR` as `<name>.partial` and gets its final name only once the package has checked and been encrypted, its checksum first: a package under its final name always has its `.sha256`. The `.sha256` names the file without its directory, so `sha256sum --check` works wherever the pair is copied. When a run fails or is interrupted, its partial files and directory are removed; a `.partial` left by an earlier run (a crash, a reboot) is never a backup, and `backup.sh` reports it and leaves it for the operator.

A file never replaces another. Final names are given with a hard link (`ln`, the `link` system call), which fails when the name exists, in one step; if the package's name turns out to be taken after its checksum was published, that checksum is removed again, and only while it is still this run's file. A backup already in `BACKUP_DIR` is never changed, whatever happens to a later run. `BACKUP_DIR` must be on a filesystem with hard links (any Linux filesystem; not FAT or some network mounts), or every backup fails.

Writing to disk: before the names are given, the content of the file and of its checksum is written to disk (`sync` of the two files, an `fsync` of each); after, the directory is (`sync` of the directory), which makes the new names durable. Once `backup.sh` has reported a file, a power loss or crash does not take it back. A crash before that point leaves, at worst, partial files or a checksum without its file, or loses the new names altogether; a final name, once it survives, always points to content already on disk. `restore.sh` refuses a partial file and a dump without its checksum, so a crash costs that backup, never an earlier one, and never makes a damaged file pass for a backup. This relies on the disk honoring flushes (a write cache without power protection that ignores them defeats any `fsync`).

When `BACKUP_REMOTE` is set, `backup.sh` then copies the package, its checksum and a receipt off the host and reads each back in full ([Copies off the host](#copies-off-the-host)); a copy that cannot be confirmed leaves the backup on the host, pending, and the exit status is 1. Without `BACKUP_REMOTE` it says that the backup exists on this host only. `BACKUP_COPY_COMMAND`, an arbitrary command of earlier versions, is refused: no script runs a configurable command. `migrate.sh` takes a backup labeled `pre-migrate-<api commit>` before every migration, with `backup.sh --restore-test`: the package is restored into a new database and compared with its manifest before it is encrypted, and a package that does not restore is not written.

**`backup.sh` removes no backup.** Old backups are removed from the host only by `prune-backups.sh`, which is disabled until `BACKUP_LOCAL_RETENTION_DAYS` is set, and then removes a backup only after reading its copy off the host back in full ([Local retention](#local-retention)). `BACKUP_KEEP_DAYS` is not applied.

`backup.sh`, `restore.sh`, `migrate.sh`, `content-sync.sh`, `sync-backups.sh` and `prune-backups.sh` take the same lock (`flock` on `/run/linuxlab-ops.lock`) without waiting: while one runs, the others stop at once with exit status 1 and change nothing. `migrate.sh` holds the lock for its whole run, and the `backup.sh` and `restore.sh` it starts run under it (they inherit it). `flock` comes with util-linux on the host.

The lock file of the production project (`linuxlab-prod`) is fixed: no setting changes it. The tests run the scripts on projects of their own, with their own containers and database, and use two variables that only those projects accept: `LINUXLAB_TEST_LOCK_FILE` (a lock file in the test's directory) and `LINUXLAB_TEST_BACKUP_RUN` (a fixed `<time>-<id>`, to make two runs collide). With the production project, either one makes every operation script stop before doing anything. Neither belongs in the environment file or a cron line.

Cron on the host, as root. A failure, including a backup refused because another operation held the lock, goes to the host's syslog through `logger`:

```
15 3 * * *  root  /var/lib/linuxlab/src/linux-lab-api/infra/ops/backup.sh <release file> >>/var/log/linuxlab-backup.log 2>&1 || logger -p user.err -t linuxlab "backup failed, see /var/log/linuxlab-backup.log"
```

A backup whose copy off the host failed is pending; a second line retries pending backups later in the day, without a new dump (an example, not installed by anything here):

```
15 9 * * *  root  /var/lib/linuxlab/src/linux-lab-api/infra/ops/sync-backups.sh >>/var/log/linuxlab-backup.log 2>&1 || logger -p user.err -t linuxlab "backup sync failed, see /var/log/linuxlab-backup.log"
```

This only records the failure on the host, in its syslog. Getting it to the operator (mail, a monitoring service) is part of provisioning the host and is not set up by this repository: no notification channel exists yet.

What the backups hold: accounts (email, display name, Argon2id hashes), session token hashes, labs, validation runs, progress, mission versions (with solutions) and the **command history, which stores what students typed, secrets included, with no redaction** ([architecture.md](architecture.md#privacy)). So:

- packages are encrypted, readable only with the operator's or the recovery key, neither of which is on the host; only encrypted packages, their checksums and receipts leave the host. The host's credential for the destination can create and read objects under its prefix, never delete them;
- retention off the host is the destination's (versioning, Object Lock, lifecycle rules), to be decided with it; on the host nothing is removed until `BACKUP_LOCAL_RETENTION_DAYS` is decided and set;
- the history is kept as long as the lab's row, which is removed only with its user: there is no retention job for it yet. Deleting a user's data means deleting the user, and backups keep it until they expire;
- the history is not a place for secrets, and the web page tells students so; operators must not present it otherwise.

### Encryption

Every package is encrypted with [age](https://age-encryption.org) (`AGE_VERSION` in `infra/ops/age.sh`, installed by `infra/release/install-age.sh`, which refuses an archive whose SHA-256 is not the recorded one; CI installs and tests that release) to every public key of `BACKUP_AGE_RECIPIENTS_FILE`. Any one of the matching private keys decrypts it; age authenticates every chunk, so a key that is not a recipient, a changed byte or a cut file is refused.

**Keys.** Two, at least: the operator's and an offline recovery key, so that losing one does not lose the backups. Each is made away from the host, with `age-keygen -o <file>`, on the operator's machine; the recovery key is kept offline (printed, or on a device kept apart). Only the public keys (`age1...`, from `age-keygen -y <file>`) go to the host, in the recipients file:

```
# operator, 2026-11
age1...
# offline recovery, 2026-11
age1...
```

One recipient per line; lines starting with `#` and blank lines are ignored. `backup.sh` and `check-env.sh` refuse a file that is missing, is a link, is not owned by the user running them (root), is writable by its group or others (whoever could add a line could read every later backup), sits in a directory another user could change (its own directory must be root's and not writable by its group or others; every directory above it, along the path as written and once links are resolved, must be root's and not writable by its group or others, or root's and sticky as `/tmp`), holds a private key (`AGE-SECRET-KEY-`), has a line that is not an X25519 recipient, lists a key twice or has fewer than two. Without the setting no backup is written. Private keys never go to the host's disk, the repository, an image, `production.env`, CI or the backup destination.

**Unencrypted data while a backup runs.** The dump, Caddy's data and the package exist unencrypted in `BACKUP_WORK_DIR`, by default `/run/linuxlab-backup` on `/run`'s tmpfs (memory, gone at reboot), mode 0700, made if missing and refused if it is a link, is not root's or has another mode, or is not on tmpfs or ramfs: the filesystem it is really on decides (`stat -f`), not its path, and no other directory and no disk is ever used instead. Each run works in a directory of its own there (`run.<random>`), removed as soon as the package is encrypted and again by the run's trap on success, failure, `INT`, `TERM` and `HUP`. A run killed outright (`SIGKILL`, out of memory) leaves its run directory: the next backup or restore, which holds the lock, removes it, and a reboot empties `/run` anyway. Removal is `rm`, not secure erasure. Before dumping, `backup.sh` checks that the work directory has room for twice the database's size (three times with `--restore-test`) plus 64 MiB, and stops otherwise. On ramfs, which has no size of its own, the room is the memory available. `/run` is usually a tenth of the memory: if the database outgrows it, point `BACKUP_WORK_DIR` at a tmpfs mounted for this, of the size needed (and the memory to back it). A directory on disk is refused: unencrypted data would reach the disk and could outlive its removal there.

**Rotation.** A change of the recipients file applies to backups made after it; nothing is decrypted or encrypted again. Each backup needs one of the keys it was encrypted to, so a private key is kept as long as any backup encrypted only to it, and those to its pair, are kept. To add or replace a key: make the new key away from the host; add its public key to the recipients file (keeping the others); run a backup, copy it to the operator's machine and check there that the new key decrypts it (`age --decrypt -i <new key> -o /dev/null <package>`), never on the host; only then remove an old public key from the file, and keep the old private key until the last backup that needs it has expired.

### Memory and swap

Unencrypted packages exist only in the work directory, which must be a tmpfs or a ramfs (memory). A tmpfs can be written to swap under memory pressure, and swap on a disk would then hold unencrypted data that outlives the run. So `backup.sh`, `restore.sh`, `migrate.sh` and `restore-offsite.sh` refuse to write into a tmpfs work directory while any swap area that is neither encrypted (a dm-crypt device) nor in memory (zram) is in use (`/proc/swaps`), and say which one. On the host, either:

- no swap: `swapoff -a`, and remove the swap entries from `/etc/fstab` (and the provider's swap file, if its image makes one); or
- encrypted swap: swap on a dm-crypt device with a random key at each boot (`/etc/crypttab`: `swap /dev/<device> /dev/urandom swap,cipher=aes-xts-plain64,size=512`).

A ramfs is never swapped, but has no size limit: it is accepted, not recommended. Prefer a tmpfs with an explicit size, for the work directory only, when `/run` (usually a tenth of the memory) is too small for twice the package plus 64 MiB:

```
tmpfs  /run/linuxlab-backup  tmpfs  size=2g,mode=0700,uid=0,gid=0,noexec,nosuid,nodev  0 0
```

with `BACKUP_WORK_DIR=/run/linuxlab-backup` and the memory to back it. The room is checked before anything is decrypted or dumped.

### Copies off the host

With `BACKUP_REMOTE` set, every backup is copied to S3-compatible storage with [rclone](https://rclone.org) (`RCLONE_VERSION` in `infra/ops/remote.sh`, installed by `infra/release/install-rclone.sh`, which refuses an archive whose SHA-256 is not the recorded one; CI installs and tests that release). The scripts call `rclone` itself, with each argument separate, never through a shell and never with a configurable command, and without the lock's file descriptor, so that nothing rclone leaves running keeps the lock.

Settings:

| Setting | Meaning |
|---|---|
| `BACKUP_REMOTE` | `remote:bucket/prefix`: a remote of the rclone configuration, then the bucket and the prefix. Letters, digits, `.`, `_`, `-` and `/` only, no `..`; anything else is refused before a backup starts |
| `BACKUP_RCLONE_CONFIG` | rclone's configuration file, with the credential. Checked as identity files are ([Restoring](#restoring)): a regular file, not a link, root's, mode 0600 or 0400, in directories only root can change. No script reads it except rclone, or prints it |
| `BACKUP_RETRIES`, `BACKUP_RETRY_DELAY` | Attempts at a copy (default 5) and seconds before the second (default 30), doubled before each one after |
| `BACKUP_MAX_AGE_HOURS` | Age of the latest confirmed copy after which `check-labs.sh` and `backup-status.sh` report it (default 26) |
| `BACKUP_LOCAL_RETENTION_DAYS` | [Local retention](#local-retention); empty: disabled |

The remote is of type `s3` (any S3-compatible provider). The configuration names the endpoint and the credential, for example (placeholders, not an account):

```
[offsite]
type = s3
provider = Other
access_key_id = <key id>
secret_access_key = <secret>
endpoint = https://<provider endpoint>
```

**The credential.** The host needs to create objects under the prefix and to read them back, since every copy is checked by reading it: it cannot be write-only. It needs nothing else, and is given nothing else: no listing, no deletion, no bucket administration (the scripts pass `--s3-no-check-bucket`, so rclone never tries to create the bucket). As an S3 policy, with the bucket and prefix of `BACKUP_REMOTE`:

```json
{"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
  "Action": ["s3:PutObject", "s3:GetObject"],
  "Resource": ["arn:aws:s3:::<bucket>/<prefix>/*"]}]}
```

Without the right to list, S3 answers a read of a missing object with "access denied", as for an object that cannot be read: the scripts treat both as "not there or not readable" and upload, and the upload is what decides.

**What is copied, and how it is confirmed.** For a backup `linuxlab-<...>.tar.age`, three objects under the prefix, in this order, each confirmed before the next:

1. the package; 2. its `.sha256`; 3. its receipt, `linuxlab-<...>.tar.age.receipt.json`.

Each object is first read: if it is there with the same bytes (an earlier attempt), it is not uploaded again; if it is there with other bytes, nothing is overwritten, the copy is a conflict, it is not retried, and a person must look. Otherwise it is uploaded (`rclone copyto` with `If-None-Match: *`, a conditional write the server refuses when the object exists, always in one request, never multipart: a file over 5,000,000,000 bytes is refused before any upload and reported instead) and then read back in full (`rclone cat`, streamed through SHA-256 and a byte count, with `pipefail`): the copy is confirmed only when the bytes read and their SHA-256 are those of the file on the host. Neither rclone's exit status, nor the size a `HEAD` request gives, nor an ETag, nor the object's existence counts. A read that fails, or is cut, is not a confirmation.

The receipt is written once the package and its checksum are confirmed, uploaded and read back the same way, and only then published next to the backup on the host, as backups are (written to disk, given its name by a hard link that never replaces a file). It holds no credential, key or manifest content:

```json
{
  "format_version": 1,
  "backup_id": "20261104T031500Z-0123456789abcdef-scheduled",
  "object": "linuxlab-20261104T031500Z-0123456789abcdef-scheduled.tar.age",
  "size": 104857,
  "sha256": "<SHA-256 of the package>",
  "checksum_object": "linuxlab-20261104T031500Z-0123456789abcdef-scheduled.tar.age.sha256",
  "checksum_sha256": "<SHA-256 of the checksum file>",
  "destination": "offsite:linuxlab-backups/host",
  "rclone_version": "v1.75.2",
  "verified_at": "2026-11-04T03:15:20Z",
  "status": "verified"
}
```

A backup without a receipt on the host is **pending**: not confirmed off the host, for any purpose. A copy that cannot be confirmed (no connection, a timeout, an upload or a read cut, an object missing after its upload) is retried `BACKUP_RETRIES` times; after the last attempt, or at once on a conflict, `<backup>.push-failed` records when and why, the backup stays on the host unchanged, and the exit status is 1. Nothing on the host is ever removed or changed by a failed copy.

`sync-backups.sh` copies the pending backups of `BACKUP_DIR` the same way, without a dump or any call to Docker. Running it again is safe: a backup with a receipt is skipped, objects already there and identical are recognised and not uploaded again, the receipt left off the host by an interrupted run is used when it matches, and a conflicting object stays a conflict.

`backup-status.sh` reports, from the host's files only: the latest receipt and its age, pending backups, failed copies, receipts that cannot be read or do not match their backup, and the free space of `BACKUP_DIR`; `check-labs.sh` reports the same ([Checking labs](#checking-labs)). A receipt says that a copy was confirmed when it was written, not that it is still there.

**What depends on the destination.** These are not guarantees of rclone or of these scripts, and must be checked with the chosen provider:

- refusing a conditional write (`If-None-Match: *`) on an existing object. AWS S3 and MinIO do (tested here with MinIO); a provider that ignores the header would let a second upload replace an object, which the read that follows would then report as a conflict, after the fact;
- that an interrupted upload leaves no object (true of a single-request upload on S3 and MinIO);
- keeping what was written: the host cannot delete, but anyone holding another credential of the bucket can. Versioning, or Object Lock in compliance mode with a retention period, protects the copies from deletion and replacement; lifecycle rules remove them after the retention the operator decides. None of these is set up by this repository; the retention off the host and the bucket's credentials (other than the host's) are the operator's.

No script deletes anything off the host.

### Local retention

`prune-backups.sh` removes backups from `BACKUP_DIR` once they are older than `BACKUP_LOCAL_RETENTION_DAYS` days (by the time in their name) and confirmed off the host now. It is disabled until that setting is given; no number of days is assumed. A backup is removed (its package and checksum; the receipt stays, as the record of where the backup is) only if:

- it has a receipt on the host, and is not the newest backup that has one: the latest confirmed backup always stays on the host, however old;
- its package, checksum and receipt are each read back in full from the destination, during this run, and match the files on the host. A receipt on the host alone is not enough: the copy may have been lost since.

A pending backup is never removed. Every candidate is confirmed before any is removed: one that cannot be read back (the destination unreachable, an object missing, changed or cut) suspends the whole run, nothing is removed, and the exit status is 1. With `BACKUP_REMOTE` unset it refuses to run.

## Restoring

Test a backup regularly (monthly, and after every change to the database):

```sh
install -m 700 -d /run/linuxlab-key && (umask 077; cat >/run/linuxlab-key/operator.key)
infra/ops/restore.sh $R /var/backups/linuxlab/linuxlab-<time>-<id>-scheduled.tar.age \
  --identity-file /run/linuxlab-key/operator.key
rm -rf /run/linuxlab-key
```

The private key is needed only for the restore: the operator pastes it into a file on `/run`'s tmpfs (`cat` reads it until Ctrl-D), restores, and removes it; it never goes to the host's disk. Before age reads any key file, `restore.sh` requires it to be a regular file, not a link, owned by the user running it (root), with no permission at all for its group or others (0600 or 0400), in a directory nobody else can change, checked as for the recipients file: the commands above make it so. `--identity-file` can be given more than once; keys are never read from the environment, and never printed.

It restores the package's database into a new database `restore_<time>` on the same PostgreSQL, never into an existing one, prints the row counts next to the database in use, and drops it. Before creating anything it requires the package's `.sha256` next to it and matching the encrypted bytes, an age file, and age to decrypt all of it into a run directory of the work directory; age writes what it has decrypted before it reaches a damaged chunk and then fails, so its output is used only once it has exited with success, and is removed in any case. Then it checks the package with `package.py` (layout, manifest, each component's size and SHA-256) while unpacking it there, reads the dump in full, and requires a server of the dump's major version or later; a package that fails any of these is refused. Everything decrypted is removed when the restore ends, whatever the reason. After the restore, the Alembic revision, the schema fingerprint and the missions' fingerprint must be those of the manifest, or the restore fails and the new database is dropped. The only optional argument is `--keep`; any other is refused before anything is done. Only encrypted packages are restored: an unencrypted package (`.tar`) or a bare dump of an earlier version of `backup.sh` is refused.

From the copy off the host, when the files on the host are gone: fetch the package and its checksum with any credential that can read the prefix (the host's can), into one directory, and restore from there; `restore.sh` checks the checksum as above. `infra/tests/e2e.sh` does this against MinIO on every run:

```sh
rclone --config /etc/linuxlab/rclone.conf --s3-no-check-bucket cat --no-traverse offsite:<bucket>/<prefix>/<name>.tar.age >/var/backups/linuxlab/recovered/<name>.tar.age
rclone --config /etc/linuxlab/rclone.conf --s3-no-check-bucket cat --no-traverse offsite:<bucket>/<prefix>/<name>.tar.age.sha256 >/var/backups/linuxlab/recovered/<name>.tar.age.sha256
```

`restore.sh` restores the database only. On a new host, the whole deployment, Caddy's data included, is restored by `restore-offsite.sh` ([Disaster recovery](#disaster-recovery)).

Emergency, when the database in use cannot be kept (a migration that cannot be fixed forward, corruption):

1. Stop the entry: `docker compose ... stop caddy`.
2. `infra/ops/restore.sh $R <package> --identity-file <key> --keep`, with the key on `/run` as above, and note the database name it prints.
3. Set `DATABASE_NAME=<that name>` in the environment file and recreate the API with the release that matches the dump's schema: `docker compose ... --env-file <that release> up -d --wait api`.
4. `infra/ops/check-labs.sh`: labs created after the dump have containers but no row, and the reaper removes them as orphans; labs the dump records as unfinished whose containers are gone are ended by the reaper as `container_lost`. Unidentified containers are reported and left alone.
5. Start Caddy again. The old database stays untouched until it is decided what to do with it.

This is not a rollback without loss: **everything written after the dump is gone**, accounts, sessions, progress, validation runs and history included, and students lose the labs they had.

## Disaster recovery

When the host is lost (disk, VM or account gone, or a compromise that rules out restoring over it), the platform is rebuilt on a new host by `infra/ops/restore-offsite.sh`, from the copies off the host alone. Nothing of the lost host is used: not its disk, its backups or receipts, its release file, its images, or its settings.

### What a recovery needs

| From | What | Notes |
|---|---|---|
| The destination | The newest backup whose package, checksum and receipt read back intact | Found by listing the prefix with the recovery credential, or named by the operator (`--receipt`) |
| GitHub | `linux-lab-api` and `linux-lab-web`, holding the commits the manifest names | Public repositories; cloned on the new host |
| The operator | `/etc/linuxlab/production.env` for the new host | Written anew, as in [Preparing the host](#preparing-the-host): new `POSTGRES_PASSWORD` (the dump holds no role password), invite code, `DOCKER_SOCKET_GID` of the new host, `LAB_CAPACITY` for its size. Same `SITE_ADDRESS` and `PUBLIC_ORIGIN` to keep the certificate and the origin users know |
| The operator | `/etc/linuxlab/backup-recipients.txt` | The public keys, as before, for the backups the new host will take |
| The operator | The recovery credential: an rclone configuration file | See below; never the host's own credential |
| The operator | One age private key the backups are encrypted to | The operator's or the offline recovery key, on a tmpfs, removed afterwards |
| The operator | DNS and firewall | The site's `A` record to the new host, 80 and 443 open, as in [Decisions](#decisions-before-the-first-deployment) |

Not needed: the private content repository. The missions are in the database (every version, with its specification), and are restored with it. The content's commit is reported when the backup recorded it ([Syncing mission content](#syncing-mission-content)); for older backups it is reported unknown, and the missions are checked against the manifest's fingerprint instead.

### The recovery credential

The host's credential can create and read objects but cannot list them, so a new host could not find the backups with it, and widening it would let a compromised host learn and read every backup name. A second credential, made for recovery and kept by the operator away from the host (with the keys), may list the prefix and read objects, and nothing else: no upload, no deletion, no other prefix. As an S3 policy, with the bucket and prefix of `BACKUP_REMOTE`:

```json
{"Version": "2012-10-17", "Statement": [
  {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": ["arn:aws:s3:::<bucket>"],
   "Condition": {"StringLike": {"s3:prefix": ["<prefix>/", "<prefix>/*"]}}},
  {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::<bucket>/<prefix>/*"]}]}
```

Its rclone configuration (a remote named, for example, `recovery`) goes in a file of root's, mode 0600, in a directory only root can change, as identity files; `restore-offsite.sh` refuses it when it is the file, or a copy of the file, named by `BACKUP_RCLONE_CONFIG`. Where a provider cannot restrict listing to a prefix, the credential gets `GetObject` only, and the operator names the receipt instead (`--receipt linuxlab-<time>-<id>-<label>.tar.age.receipt.json`): receipt names are in the backup log and in `backup-status.sh` output, which the operator should keep off the host as well. `infra/tests/minio.sh` creates exactly these credentials; `infra/tests/offsite-cases.sh` checks that the recovery credential lists and reads under the prefix only, cannot upload or delete, and that the host's still cannot list.

### Preparing the new host

As in [Preparing the host](#preparing-the-host): Linux x86_64, Docker Engine 28 or later with the Compose plugin, `python3` 3.10 or later, `git`, `flock`; then, from a clone of `linux-lab-api`:

```sh
git clone https://github.com/FranciscoPedro06/linux-lab-api /var/lib/linuxlab/src/linux-lab-api
git clone https://github.com/FranciscoPedro06/linux-lab-web /var/lib/linuxlab/src/linux-lab-web
cd /var/lib/linuxlab/src/linux-lab-api
sudo infra/release/install-age.sh
sudo infra/release/install-rclone.sh
sudo infra/release/install-gvisor.sh && sudo systemctl restart docker
```

[Memory and swap](#memory-and-swap) applies before anything is decrypted. Then the settings, the recovery credential and the key; the key on `/run`'s tmpfs, pasted (`cat` reads until Ctrl-D):

```sh
install -d -m 700 /etc/linuxlab /run/linuxlab-key
(umask 077; cat >/etc/linuxlab/production.env)          # or from infra/production.env.example
(umask 022; cat >/etc/linuxlab/backup-recipients.txt)
(umask 077; cat >/etc/linuxlab/recovery-rclone.conf)
(umask 077; cat >/run/linuxlab-key/recovery.key)
infra/release/check-env.sh /etc/linuxlab/production.env
```

Leave `BACKUP_REMOTE` empty until the recovery is done: the new host's own credential is set afterwards.

### Running it

```sh
sudo infra/ops/restore-offsite.sh --remote recovery:<bucket>/<prefix> \
  --rclone-config /etc/linuxlab/recovery-rclone.conf --identity-file /run/linuxlab-key/recovery.key \
  --api-repo /var/lib/linuxlab/src/linux-lab-api --web-repo /var/lib/linuxlab/src/linux-lab-web
```

`--check-only` stops after the backup is downloaded, decrypted and checked (no Docker, nothing restored): a rehearsal that can be run from any machine with the tools. In order, the script:

1. Checks the host: Linux x86_64, root, the tools and their tested releases (age, rclone), Docker Engine 28 or later, Compose, `runsc` registered, `check-env.sh` on the environment file, the identity and credential files (owner, mode, directories). Takes the operations' lock.
2. Lists the receipts under the prefix (or reads the one given), and keeps only those that describe a confirmed copy, made for this bucket and prefix: named as a backup's receipt, `format_version` 1, `status` verified, the object, checksum object and backup id consistent with the name, a size and two SHA-256, a `verified_at` time. Refused receipts are listed with the reason; neither a file name nor an object's date is trusted.
3. Takes the valid receipts newest first by `verified_at`, and for each: reads its checksum object, which must have the receipt's `checksum_sha256` and name the package with the receipt's `sha256`; checks the room on disk; downloads the package and requires its byte count and SHA-256 to be the receipt's, then its checksum file, then an age header. The first that passes is used; each newer one that did not is reported, with why, and the report warns that what was written after the backup used is lost.
4. Decrypts the package with age into the work directory (memory), checks it with `package.py` (layout, manifest, each component's size and SHA-256) while unpacking it, and requires its manifest to be of the backup the receipt names.
5. Requires its own checkout to hold the manifest's API commit in its history, and the clones to hold both commits; builds the release with `infra/release/build.sh` from those exact commits (`git archive` of each), never from a branch or a tag; checks the images' `org.opencontainers.image.revision` labels against the commits. The new image ids differ from the lost host's: the recorded ids are reported, not required.
6. Creates the project's volumes, labeled with the backup (`linuxlab.recovery`), and refuses any volume of the project that exists without that label: an existing deployment is never written to. Starts PostgreSQL alone (the release's pinned image), requires a server of the dump's major version or later, reads the dump back in full, and requires the database to be empty.
7. Restores the dump in one transaction (`pg_restore --single-transaction --exit-on-error --no-owner`), then requires the Alembic revision, the schema fingerprint, the missions' fingerprint and the roles to be the manifest's, and every sequence to be ahead of its column; prints the row counts.
8. Extracts Caddy's data into the new `caddy-data` volume and compares it entry by entry with the package (`tar --compare`); `caddy-config` stays new and empty: Caddy starts from the release's Caddyfile.
9. Starts the stack (`up --wait`), then checks it: the API's health (database and runtime); every mission's current version read back by the release's own code as provisioning and validation read it, and their number the manifest's; through Caddy on the published port, with the site's name: the certificate served is the restored one, the entry page with the site's headers, client routes, the API's health and routes (401 without a session), no API documentation; the configuration Caddy loaded serves the site on 443, ports 80 and 443 are published, and a plain HTTP request on 80 is redirected to the same path on `https://<site>`.
10. Writes a report, `/var/lib/linuxlab/recovery/<backup>/report.txt`: the backup used and those rejected, the commits, the release file, the database's revision and missions, Caddy's certificate.

Everything decrypted is removed when the script ends, whatever the reason; only encrypted files are downloaded to disk. Any inconsistency stops the run with exit status 1 and says what was done (`done.*` and `restored.*` files in the backup's recovery directory). Running the same command again goes on from there: a restored database is checked again, not restored again (and a database the recovery did not restore is never overwritten), Caddy's data likewise, the release file is reused when its images still match. To start over, with another backup for example: `docker compose -p linuxlab-prod down --volumes` removes the volumes the recovery made, which hold only restored data. Nothing is ever dropped or overwritten by the script itself.

### After the recovery

1. Point the site's `A` record to the new host; open 80 and 443 in the provider's firewall (and SSH as decided). Caddy serves the restored certificate for `SITE_ADDRESS`, and renews it with ACME as usual once the name resolves to the new host. With another domain, or without Caddy's data in the backup, it requests a new certificate (mind the ACME rate limits: use the staging directory first, as for the first deployment).
2. The acceptance of [Deploying](#deploying), step 4, on the real host.
3. Remove the key and the recovery credential from the host: `rm -rf /run/linuxlab-key /etc/linuxlab/recovery-rclone.conf`. Keep `/var/lib/linuxlab/recovery` (encrypted package and report) until the first backup of the new host is confirmed, then remove it.
4. Give the new host its own credential (`BACKUP_REMOTE`, `BACKUP_RCLONE_CONFIG`, [Copies off the host](#copies-off-the-host)), take a backup and check that it is confirmed off the host; install the cron lines.
5. Labs the backup recorded as unfinished have no container: the reaper ends them as `container_lost` when the API starts. Students lose what they did after the backup.

### What is tested

- `infra/tests/offsite-cases.sh` (CI, job "Docker image"): the discovery, download and checks against MinIO with real rclone and age, without Docker: the newest backup by `verified_at`, receipts that are not JSON, pending, of another prefix or for other objects; packages changed, cut or missing off the host, a changed checksum object (each rejected, the next one used, the warning); a package under another backup's receipt; a credential without listing (refused, then `--receipt`); the host's own credential refused; a key that is not a recipient; unsafe identity or environment files; unencrypted swap; a work directory on disk.
- `infra/tests/disaster.sh` (CI, job "Disaster recovery on a new host (runsc)"): an origin on the runner's Docker Engine, with synthetic missions synced at a commit and students who used labs, backed up three times to MinIO with the host's credential (by migrate.sh, then twice by backup.sh, students working in between), then destroyed: its containers, labs, volumes and networks, its three images and its release file, its settings, backups and receipts, each checked gone. A new host, a privileged container with its own Docker Engine on its own volume (no image, volume, release file, backup or receipt of the origin; only MinIO and the keys are shared), is prepared with the repository's installers and recovered with `restore-offsite.sh` as `linuxlab-prod`, with the swap of the runner turned off. Then: every table's rows, the sequences, the extensions and the schema equal the origin's; the content commit is the one synced; Caddy's certificate and CA are the origin's and served; the images carry the manifest's commits; a second run restores nothing again; a volume not made by the recovery is refused; the site's checks pass through the new Caddy; a student of the origin logs in with their password and finds their progress; a new student's whole session runs with labs under runsc on the new host.

Not tested: the private missions (CI has no access to `linux-lab-content`; the origin syncs the synthetic missions of `tests/fixtures/content` from a commit of a throwaway repository, through `content-sync.sh`), a real provider (only MinIO: listing restricted by prefix, conditional writes and error codes may differ), a real domain with an ACME certificate restored and renewed (the tests use Caddy's local CA for `linuxlab.localhost`), DNS and firewall changes, a host provisioned by a provider image (the tests use Alpine in a container with Docker's `dind` image), and databases of production size.

### Known limitations

- **Content commit of older backups.** Backups taken before `content_syncs` existed, or after a sync run without a commit, record no content commit (`code.content.sha` is null, and `code.content.source` says why). Their missions are restored from the database and checked against the manifest's fingerprint; the commit they came from is reported unknown. Backups taken after a `content-sync.sh` record it.
- **Secrets are the operator's.** `production.env`, the recipients file, the recovery credential and an age private key are not in any backup and cannot be recovered from one. Without a private key the backups are unreadable; without the recovery credential (or the receipt names and a reading credential), the backups cannot be found.
- **Domain and DNS.** The restored certificate is only useful under the same `SITE_ADDRESS`, once DNS points to the new host. Until then the platform answers only by address.
- **Rebuilt images are not the lost host's images.** They are built from the same commits, but the API's `Dockerfile` uses base images by tag (the edge's and PostgreSQL's are pinned by digest), and `npm ci` and `uv sync` resolve from their lock files at build time: a base image updated since gives other image ids. The labels prove the source commit, not bit-for-bit identity.
- **MinIO is not the provider.** Listing restricted to a prefix, the policy syntax, and how a provider answers a missing or forbidden object are checked against MinIO only.
- **Memory and space.** The decrypted package and its unpacked components need about twice the package's size in the work directory (memory); Docker needs room for the images and the restored database (the script requires 6 GiB plus ten times the dump's size). Both are checked before anything is restored; a tmpfs sized for this must be mounted when `/run` is too small ([Memory and swap](#memory-and-swap)).
- **No restore of PostgreSQL globals.** As for any restore: only the role `linuxlab`, created by the postgres image, exists; a database with other roles is not backed up ([Backups](#backups)).

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
| Backups: `BACKUP_REMOTE` not set, no valid receipt, the latest confirmed copy older than `BACKUP_MAX_AGE_HOURS`, backups without a receipt, a failed copy, a receipt that does not match its backup | Backups are not (all) confirmed off the host | The operator: `backup-status.sh`, the backup log, `sync-backups.sh` ([Copies off the host](#copies-off-the-host)) |

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
| Suspected compromise of the API or a lab escape | Stop the stack (`docker compose ... stop`), keep the host as it is for investigation, do not restore over it. The API has root-equivalent access to the host through the Docker socket ([threat-model.md](threat-model.md#accepted-risks)). Rebuild on a new host ([Disaster recovery](#disaster-recovery)), and rotate the secrets the old host held |
| The host is lost | [Disaster recovery](#disaster-recovery) |
