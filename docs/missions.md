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
- `briefing.md` is the problem statement. `explanation.md` is shown only after completion.

## Content rules

`linuxlab content sync` reads `content/`, checks everything below, and writes nothing unless all of it holds. Errors name the file and, where there is one, the field (`missions/x/mission.yaml: validation.all.2.mode: Input should be a valid string`).

- `content/` holds only `modules/` and `missions/`; `modules/` only `<slug>.yaml` files; `missions/` only mission directories, each with a `mission.yaml`. `.gitkeep` files are ignored.
- Every mission is listed by exactly one module, and every slug a module lists has a mission directory. A mission in no module, in two modules or twice in one module is an error.
- Slugs (`[a-z0-9-]`, up to 64 characters) match the file or directory name.
- No symbolic links, and nothing but regular files and directories, anywhere under `content/`.
- Files named in `mission.yaml` (`briefing`, `explanation`, `setup.script`, the scripts of `solutions` and `counterexamples`) are relative paths with `/`, inside the mission directory: no absolute paths, `..`, `.` or empty components.
- Files are UTF-8. Line endings are normalized to LF before hashing.
- YAML is read with a safe loader. Duplicate keys and aliases (`&a` / `*a`) are errors.
- Nothing is executed or compiled during the sync: scripts, conditions and parameters are checked as data.

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
| `environment.image` | Alias resolved by platform configuration to a pinned digest |
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

This section and the next four describe how conditions, parameters, setup and mission tests behave once they are implemented (increments 07, 08 and 11). The sync already checks their structure and stores them in each version; it does not run them.

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

Limits: 50 leaves and a depth of 5, counting the root as level 1. Each leaf `id` is a slug, unique within the mission, and is used to report which conditions fail most often.

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

Every leaf accepts `fail_message` and `pass_message`.

- `mode` must be a string of four octal digits (`"0700"`). In YAML, an unquoted `700` is read as a decimal integer and `0700` as octal 448; the parser rejects both.
- `regex` uses RE2 syntax.
- `answer` is compared by the API. The answer is never sent to the container.

## Parameters

Parameters are generated by the API for each lab.

| Generator | Options |
|---|---|
| `hex` | `length` |
| `word` | built-in word list |
| `int` | `min`, `max` |
| `choice` | `values` |

- Every generated value must match `^[a-z0-9_-]{1,64}$`.
- No parameter comes from user input.
- In setup, parameters are only available as `LAB_PARAM_<NAME>` environment variables. The script text is never modified.
- In conditions, `{{ name }}` is replaced by a plain lookup. There is no template engine. Every placeholder, and every `equals_param`, must name a declared parameter.
- Values are stored in `lab_sessions.params` and never sent to the client.

## Setup

When a lab is created:

1. `labctl init` runs as root: it populates `/home/student` from `/etc/skel` and creates `/run/lab`.
2. `setup.sh` is piped to `bash -euo pipefail` on stdin, as the user set in `setup.user`.

The script is never written to the container's filesystem. A non-zero exit code or a timeout marks the lab `failed`.

Authoring rules:

- Use `student` as the setup user unless the mission needs something the student could not create.
- Always quote parameters: `"$LAB_PARAM_TOKEN"`.
- Start background processes with `env -i`. Otherwise the student can read the parameters from `/proc/<pid>/environ`.
- The rootfs is read-only. Setup can only write to `/home/student`, `/tmp` and `/run/lab`.

## Mission tests

Every mission is tested automatically:

1. A fresh lab with no action taken must validate as `failed`.
2. Each script in `solutions` runs as `student` in a fresh lab and must validate as `passed`. For missions with `answer`, the script's stdout is submitted as the answer.
3. Each script in `counterexamples` must validate as `failed`.
4. For missions with parameters, two labs must receive different values.
5. No student process may have `LAB_PARAM_` in its environment.

A mission needs at least two different solutions and one counterexample. This checks that validation accepts more than one path and rejects shortcuts.

The scripts in `solutions` are also shown to the student after completion.
