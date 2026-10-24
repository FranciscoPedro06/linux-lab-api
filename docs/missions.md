# Missions

A mission describes a problem, prepares the lab's initial state and declares the conditions that mean the problem is solved. It does not say which commands the student should use.

Mission content that students read (titles, briefings, objectives, hints, messages, explanations) is written in Portuguese. Slugs, file names, field names and parameter names are in English.

## Layout

```
content/
  modules/
    fundamentals.yaml
  missions/
    secure-deploy-script/
      mission.yaml
      briefing.md
      explanation.md
      setup.sh
      tests/
        solution-octal.sh
        solution-symbolic.sh
        counter-recreate-empty.sh
```

- The directory name is the mission slug, and the module file name is the module slug.
- The module file lists which missions belong to it and in what order. It is the only place where that is defined.
- `briefing.md` is the problem statement. `explanation.md` is meant to be shown only after completion; until that is implemented (a candidate for increment 14) it never leaves the server.

## Content rules

`linuxlab content sync` reads `content/`, checks everything below, and writes nothing unless all of it holds. Errors name the file and, where there is one, the field (`missions/x/mission.yaml: validation.all.2.mode: Input should be a valid string`).

- `content/` holds only `modules/` and `missions/`; `modules/` only `<slug>.yaml` files; `missions/` only mission directories, each with a `mission.yaml`. `.gitkeep` files are ignored.
- Every mission is listed by exactly one module, and every slug a module lists has a mission directory. A mission in no module, in two modules or twice in one module is an error.
- Slugs (`[a-z0-9-]`, up to 64 characters) match the file or directory name.
- No symbolic links, and nothing but regular files and directories, anywhere under `content/`.
- Files named in `mission.yaml` (`briefing`, `explanation`, `setup.script`, the scripts of `solutions` and `counterexamples`) are relative paths with `/`, inside the mission directory: no absolute paths, `..`, `.` or empty components.
- Files are UTF-8. Line endings are normalized to LF before hashing.
- YAML is read with a safe loader. Duplicate keys and aliases (`&a` / `*a`) are errors.
- Nothing is executed during the sync: scripts, conditions and parameters are checked as data. The only thing compiled is each RE2 pattern (`regex`, `args_regex`), to check its syntax, with every `{{ }}` placeholder replaced by a literal; the stored specification and its hash keep the pattern as written.

Size limits, in bytes:

| File | Limit |
|---|---|
| Module file | 64 KiB |
| `mission.yaml` | 64 KiB |
| `briefing` and `explanation` (Markdown) | 64 KiB each |
| `setup.script` | 32 KiB |
| Each script in `solutions` and `counterexamples` | 64 KiB |
| A mission's `mission.yaml` and every file it names, each counted once | 256 KiB |

## Sync

```sh
linuxlab content sync                  # content/ of this repository, or CONTENT_DIR
linuxlab content sync --content-dir DIR
linuxlab content sync --allow-empty    # only to archive the whole catalog on purpose
```

It exits with `0` when the database matches the content (printing what was created, updated and archived), and `1` when the content is invalid, the directory is empty without `--allow-empty`, or the database write failed; in every case `1` means nothing changed. Run it again on the same content and it writes nothing. A changed mission gets a new version; a removed module or mission is archived with its versions kept. Details in [architecture.md](architecture.md#missions-and-versioning).

In the Compose environment: `docker compose -f infra/compose.yml exec api linuxlab content sync`.

## Module

```yaml
schema: 1
slug: fundamentals
title: Fundamentos
description: Navegar, criar e organizar arquivos.
status: published
missions:
  - notes-backup
  - tidy-project
  - secure-deploy-script
  - hidden-file
  - runaway-process
```

## `mission.yaml`

```yaml
schema: 1
slug: secure-deploy-script
title: Proteja o script de deploy
summary: Qualquer usuário consegue ler um script que contém um token.
difficulty: 1
estimated_minutes: 5
status: published
tags: [permissions, chmod]

briefing: briefing.md
explanation: explanation.md
objectives:
  - "`~/deploy.sh` continua existindo, com o conteúdo original."
  - "Somente o dono pode ler, escrever e executar o arquivo."

environment:
  image: base
  profile: default

params:
  token: { generator: hex, length: 8 }

setup:
  script: setup.sh
  user: student
  timeout_seconds: 20

hints:
  - "Veja as permissões atuais com `ls -l`."
  - "Permissões podem ser escritas em octal ou na forma simbólica."

validation:
  all:
    - id: exists
      type: file_exists
      path: /home/student/deploy.sh
      fail_message: "O arquivo ~/deploy.sh não existe ou não é um arquivo comum."
    - id: content-kept
      type: file_content
      path: /home/student/deploy.sh
      contains: "DEPLOY_TOKEN={{ token }}"
      fail_message: "O conteúdo original do script foi perdido."
    - id: mode
      type: file_permissions
      path: /home/student/deploy.sh
      mode: "0700"
      fail_message: "As permissões ainda não estão corretas."
    - id: owner
      type: file_owner
      path: /home/student/deploy.sh
      user: student
      fail_message: "O dono do arquivo mudou."

solutions:
  - label: Octal
    script: tests/solution-octal.sh
  - label: Simbólico
    script: tests/solution-symbolic.sh

counterexamples:
  - label: Recriar o arquivo vazio com 700
    script: tests/counter-recreate-empty.sh
```

| Field | Rule |
|---|---|
| `schema` | Version of the file format, not of the mission. Only `1` exists |
| `slug` | `[a-z0-9-]`, 1 to 64 characters, equal to the directory name, never changed |
| `title`, `summary` | Required, not blank |
| `difficulty` | 1 to 5 |
| `estimated_minutes` | At least 1 |
| `status` | `draft`, `published` or `archived`. Only `published` missions in a `published` module appear in the catalog |
| `tags` | Optional list of distinct slugs |
| `objectives` | At least one |
| `hints` | Optional |
| `environment.image` | Image alias. Only `base` exists, resolved by the platform to `LAB_IMAGE`; any other value is refused by the sync |
| `environment.profile` | Container security profile. Only `default` exists in the MVP |
| `params` | Optional. Names match `[a-z][a-z0-9_]*`, up to 32 characters |
| `setup` | Required |
| `setup.user` | `student` (default) or `root` |
| `setup.timeout_seconds` | 1 to 60 |
| `solutions` | At least two, each with a different script |
| `counterexamples` | At least one |

Module files take `schema`, `slug` (equal to the file name), `title`, `description`, `status` (same values as missions) and `missions`, a list of distinct mission slugs. Modules are listed by slug.

The mission version is not declared in the file. It is derived from the content during sync (see [architecture.md](architecture.md#missions-and-versioning)). Everything in `mission.yaml` except `status`, and the content of every file it names, is part of the version; changing the status, or the module and position of a mission, does not create one.

Values are not coerced: a field that expects a string refuses a number, a boolean or a date, and the reverse. Unknown fields are rejected by the parser.

## Validation tree

The sync checks the structure of conditions, parameters, setup and mission tests and stores them in each version; it never runs them. Parameters and setup are used when a lab is created (increment 07), conditions when the student validates the lab (increment 08, `POST /api/labs/{id}/validate`), and solutions and counterexamples by the [mission tests](#mission-tests) (increment 11); all are described below.

The root of `validation` is a single node. A node is either:

- a leaf, with `id` and `type`;
- an operator, with exactly one of the keys `all`, `any` or `not`.

`all` and `any` take a list of nodes. `not` takes a single node and requires `fail_message`.

```yaml
validation:
  all:
    - id: original-kept
      type: file_exists
      path: /home/student/notas.txt
    - any:
        - id: backup-plain
          type: file_content
          path: /home/student/backup/notas.txt
          contains: "{{ token }}"
        - id: backup-bak
          type: file_content
          path: /home/student/backup/notas.txt.bak
          contains: "{{ token }}"
      fail_message: "Não há uma cópia das notas em ~/backup."
```

Limits: 50 leaves and a depth of 5, counting the root as level 1, and `file_content` conditions on at most 8 different paths. Each leaf `id` is a slug, unique within the mission, and is used to report which conditions fail most often.

Paths in conditions are absolute and canonical: no `.` or `..` components, no empty components (`//` or a trailing `/`), no backslash or NUL, up to 4096 characters. They are resolved as written, never normalized.

## Validators

| Type | Parameters | Passes when |
|---|---|---|
| `file_exists` | `path`, `follow_symlinks` (default `false`) | The path is a regular file |
| `directory_exists` | `path`, `follow_symlinks` (default `false`) | The path is a directory |
| `path_absent` | `path` | Nothing exists at the path |
| `file_content` | `path` and one of `equals`, `contains`, `not_contains`, `regex`; `trim` (default `true`) | The content, up to 64 KB, matches |
| `file_permissions` | `path`, `mode` | `st_mode & 0o7777` equals `mode` |
| `file_owner` | `path`, `user` | The owner has this name inside the container |
| `file_group` | `path`, `group` | The group has this name inside the container |
| `process_running` | `match` (`comm` or `args_regex`), `user`, `min_count` (default 1) | Matching processes exist |
| `process_not_running` | `match`, `user` | No matching process exists |
| `answer` | one of `equals`, `equals_param`, `one_of`; `normalize` (`trim`, `lowercase`) | The submitted answer matches |

Every leaf accepts `fail_message` and `pass_message`. Messages are shown to the student as written and cannot contain `{{ }}` placeholders, so they never reveal a parameter.

- `mode` must be a string of four octal digits (`"0700"`). In YAML, an unquoted `700` is read as a decimal integer and `0700` as octal 448; the parser rejects both.
- `regex` and `args_regex` use RE2 syntax, up to 1024 characters. Constructs RE2 does not support (lookaround, backreferences, possessive quantifiers) are refused by the sync; there is no fallback to another engine.
- `answer` is compared by the API. The answer is never sent to the container.

How each condition is checked against the lab:

- Paths are never resolved through a symbolic link the student may have created: a symbolic link on the way, or in place of the file, makes `file_exists`, `file_content`, `file_permissions`, `file_owner` and `file_group` fail (`wrong_type`). Only `file_exists` and `directory_exists` with `follow_symlinks: true` follow links, to check the target's type. `file_permissions`, `file_owner` and `file_group` accept any type other than a symbolic link, so they also apply to directories.
- `path_absent` passes only when nothing, not even a dangling symbolic link, exists at the path.
- `file_content` reads a regular file of at most 64 KiB, as UTF-8. A larger file, content that is not UTF-8, a FIFO or another non-regular file fails the condition. With `trim`, whitespace is removed from both ends of the content before comparing; the expected value and the pattern are used as written. `equals` compares the whole content, `contains` and `not_contains` look for a substring, and `regex` searches anywhere in the content (use `^` and `$` to anchor).
- `process_running` counts processes whose `comm` (the kernel's name, at most 15 bytes) equals `comm`, or whose command line (arguments separated by spaces, cut at 4 KiB) matches `args_regex` anywhere, owned by `user` when given; `process_not_running` passes when there are none. Zombies are not counted.
- `answer` applies `normalize` to both the submitted answer and the expected values: `trim` first, then `lowercase`, whatever their order in the list. `equals_param` compares with the lab's value of that parameter. Without a submitted answer the condition fails.
- A condition fails with a reason (`not_found`, `wrong_type`, `too_large`, `not_utf8`, `mismatch`, `no_answer`) when the lab's state does not match. It is `error`, never `failed`, when it could not be checked: the probe timed out or failed, or the container stopped.
- Without `pass_message` or `fail_message`, the student sees a generic message.

## Parameters

Parameters are generated by the API for each lab, in the transaction that creates it, from the operating system's cryptographic source (`secrets`).

| Generator | Options | Value |
|---|---|---|
| `hex` | `length` (1 to 64) | Exactly `length` lowercase hexadecimal digits |
| `word` | none | A word from the project's list, `src/linuxlab/content/words.txt`: lowercase English words, ASCII letters only |
| `int` | `min`, `max` (inclusive, within ±10^18) | The number in decimal, with a leading `-` if negative |
| `choice` | `values` (distinct, each a valid value) | One of `values` |

- Every generated value must match `^[a-z0-9_-]{1,64}$`; a value that does not fails the lab's creation before anything is stored.
- No parameter comes from user input, and generation evaluates nothing from the mission.
- Each lab gets independent draws. Two labs can receive the same value; nothing makes values unique. What is required is that a parameter can vary: the mission tests refuse a parameter whose generator can draw only one value (an `int` with `min` equal to `max`, a `choice` with one value).
- Values are stored in `lab_sessions.params` as a JSON object of strings (`{"token": "3fa9c01e"}`), fixed for the lab's lifetime. They are never sent to the client or written to a log.
- In setup, parameters are only available as `LAB_PARAM_<NAME>` environment variables, with the name in uppercase. The script text is never modified.
- In conditions, `{{ name }}` is replaced by a plain lookup. There is no template engine. Every placeholder, and every `equals_param`, must name a declared parameter. In `regex` and `args_regex` the value is escaped, so it matches only itself. Placeholders are not allowed in `fail_message` and `pass_message`.

## Setup

When a lab is created, after its container starts and before the lab becomes `ready`:

1. `labctl init` runs as root (`python3 -I -S /opt/labctl/labctl init`, shipped in the lab image): it copies `/etc/skel` into `/home/student`, owned by the student, with the modes of `/etc/skel`. It refuses to write through a symbolic link, and running it again gives the same result. `/run/lab` already exists: the runtime mounts it.
2. The setup script of the lab's pinned version is written to the standard input of `bash -euo pipefail`, which reads and runs it, as the user set in `setup.user`.

The script comes only from the stored mission version; a request cannot supply or change it. It is never written to the container's filesystem.

| | `student` | `root` |
|---|---|---|
| Identity | uid 1000, no capabilities, `no-new-privileges` | uid 0 with the four platform capabilities (`CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `KILL`) |
| Working directory | `/home/student` | `/`, with `HOME=/root` (read-only): use absolute paths |
| Environment | `PATH`, `LANG`, `LAB_PARAM_*` (Docker adds `HOSTNAME` and `HOME`) | the same |

`setup.timeout_seconds` (at most 60) bounds the script: at the limit its process group receives SIGTERM, then SIGKILL 2 seconds later. Its output is read while the script runs and discarded; it is never returned or logged. The API logs only the lab id, the exit code and whether the script timed out.

A non-zero exit code or a timeout, of `labctl init` or of setup, fails the lab (`provisioning_failed`): its container is removed and `POST /api/labs` answers `503 lab_start_failed`. Setup runs once per lab. It is never retried, and a lab whose provisioning was interrupted (the API stopped mid-setup) is failed by the reaper after the provisioning timeout, not resumed.

Authoring rules:

- Use `student` as the setup user unless the mission needs something the student could not create.
- Always quote parameters: `"$LAB_PARAM_TOKEN"`. With `-u`, a misspelled parameter name fails setup.
- Start background processes with `env -i`, and redirect their output (`env -i cmd >/dev/null 2>&1 &`). Without `env -i` the student can read the parameters from `/proc/<pid>/environ`; a process that keeps the output open keeps setup running until its timeout.
- The script is bash's standard input: a command in it that reads standard input (`read`, `cat` without a file) consumes the rest of the script. Redirect such commands from a file or `/dev/null`.
- The rootfs is read-only. Setup can only write to `/home/student`, `/tmp` and `/run/lab`.

## Mission tests

Every mission declares at least two different solutions and one counterexample. Solutions show that validation accepts more than one way to reach the final state; counterexamples show that it rejects shortcuts. The mission tests (increment 11, `tests/missions/`) run them in real labs. They are test code: not part of the production image or of the `linuxlab` command.

For every mission in the content directory under test (`CONTENT_DIR`, as for the sync; by default `content/`):

1. **Static checks**, without a database or a lab: the content rules of the sync, and every parameter must be able to take at least two values.
2. **Sync into a throwaway database.** The content is synced, with `sync_content`, into a database created for the test session on the PostgreSQL server of `DATABASE_URL`, and dropped at the end; the database `DATABASE_URL` names is never written to. Every module and mission is set to `published` on the loaded copy only. The status is not part of a version, so the stored version and its hash are the ones `linuxlab content sync` stores, and drafts are tested without being published anywhere.
3. **One case per script** in `solutions` and `counterexamples`, read from the stored version. Each case gets a new user and a new lab, created by the lab service as for a student (parameters, `labctl init`, setup), and then:
   1. the lab is validated by the validation service (`labctl probe`, the evaluator, `validation_runs`) and must be `failed`;
   2. as the student, a fixed check reads the environment of every process the student is allowed to read, and counts those with a `LAB_PARAM_` variable. It prints only the count, and there must be none;
   3. the script runs as the student, as the standard input of `bash -euo pipefail`, in `/home/student`, with the fixed environment and no parameter: it has to find what it needs in the lab, as a student would;
   4. for a mission with an `answer` condition, the script's standard output is the answer;
   5. the lab is validated again: a solution must be `passed`, a counterexample `failed`;
   6. the lab is ended, whether the case passed or not.

The tests add no evaluator: outcomes come from the validation service, and runs and completions are stored in the throwaway database like any others.

Limits:

| | |
|---|---|
| Script | 30 seconds, then the runtime ends its process group; output up to 1 MiB per stream, as for any exec |
| Answer | Standard output as UTF-8 with only the final `\n` removed. Empty, truncated, non-UTF-8 or longer than 1024 bytes fails the case (`answer_invalid`); nothing is submitted then |
| Setup | `setup.timeout_seconds`, as in production |
| `error` | Never an accepted outcome, before or after the script |

Authoring rules for solutions and counterexamples: non-interactive bash scripts, without parameters, that do not read standard input (it holds the script), and that end with status 0. A counterexample must also run successfully: it reaches a wrong state, it does not fail.

### Failures

| Code | Class | Meaning |
|---|---|---|
| `invalid_content` | content | The content breaks a rule of the sync |
| `params_degenerate` | content | A parameter can take only one value |
| `setup_failed`, `setup_timed_out` | content | Setup exited non-zero, or hit its timeout |
| `fresh_not_failed` | content | The lab passed before the script ran |
| `param_leak` | content | A process the student can read carries `LAB_PARAM_` variables |
| `script_failed`, `script_timed_out` | content | The script exited non-zero, or hit its 30 seconds |
| `answer_invalid` | content | The output is not a usable answer |
| `solution_not_passed`, `counterexample_not_failed` | content | The validation outcome is not the expected one |
| `runtime_unavailable` | platform | Docker cannot be reached |
| `lab_start_failed` | platform | The lab could not be created for a reason other than setup |
| `labctl_init_failed` | platform | `labctl init` failed |
| `validation_error` | platform | A validation ended in `error` (with its error code, such as `probe_failed`) |
| `leak_check_failed` | platform | The environment check gave no count |
| `infrastructure_error` | platform | The runtime or PostgreSQL failed during the case |

A content failure means the mission has to change; a platform failure says nothing about the mission. Setup failures are content failures here, while in production they fail the lab before any validation run exists, so the student never gets an attempt from them.

Each failure is reported on one line with the mission, its version, the first 12 characters of its hash, the case, the step, the class and the code, and the status and reason of each condition:

```
secure-deploy-script v1 3fa9c01e2b7d solution-2 step=validate [content] solution_not_passed: expected passed, got failed; exists=passed mode=failed(mismatch)
```

Cases are named by position (`solution-2` is the second entry of `solutions`), since script names and labels can say how a mission is solved. Reports never contain parameters, script output, answers, or expected or observed values.

### Running

```sh
uv run pytest -m missions                                  # the harness on a FakeRuntime; PostgreSQL
uv run pytest -m content                                   # static checks of CONTENT_DIR and the initial missions
uv run pytest -m mission_labs                              # cases in real labs; Docker, the lab image and PostgreSQL
uv run pytest -m "mission_labs and content" -k secure-deploy-script
CONTENT_DIR=/path/to/content uv run pytest -m "content or mission_labs"
```

The PostgreSQL user of `DATABASE_URL` needs the `CREATEDB` privilege. Cases in real labs run under `LINUXLAB_TEST_OCI_RUNTIME` and need Linux, as the other lab tests ([runtime.md](runtime.md#environment)). The harness itself is tested on synthetic missions: those in `tests/fixtures/content`, and those in `tests/fixtures/harness`, written to fail in one way each.

CI runs every mission on every change, not only the missions that changed, since a change to the loader, setup, parameters, probe or evaluator can break a mission that did not: `Mission harness (PostgreSQL)` and `Mission labs (Docker, runc)` in the CI workflow, and `Mission labs (runsc)` in the Runtime workflow, which also runs for changes in `content/`. Each job runs the harness's own tests first, then the content under test as a separate step. A mission is ready to publish when the three jobs pass on the same commit.

## Content source

The repositories are public. A mission's solutions and counterexamples tell how it is solved, and are meant to reach a student only after completion (a candidate for increment 14); committed to a public repository, anyone could read them. For that reason `content/` in this repository holds no missions yet: the five initial missions planned for increment 11 (`notes-backup`, `tidy-project`, `secure-deploy-script`, `hidden-file`, `runaway-process`) wait for a content source whose visibility matches that decision.

Until then:

- `linuxlab content sync` and the mission tests read `CONTENT_DIR`, which can point to a directory outside this repository;
- the content tests fail and name the missing missions, so CI does not pass on an empty catalog;
- the production image still copies `content/` from this repository (Dockerfile); deploying content from another source is part of increment 13.

The examples in this document are illustrative and are not the content of those missions.
