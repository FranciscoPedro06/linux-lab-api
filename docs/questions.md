# Questions

A question is a short conceptual exercise of the easy mode: a statement, a few options, one of them correct. It has no lab, no setup and no validation tree. The student picks an option; the API checks it against the question's current version, records the answer and says whether it was right, why, and what the question teaches.

Only single choice exists in this version (`type: single_choice`): one correct option among two to six. Multiple selection, written answers and other formats are not supported; a file of another type is refused by the sync.

What students read (titles, statements, options, feedback, explanations, hints) is written in Portuguese. Slugs, option ids, file names and field names are in English.

## Layout

```
content/
  modules/
    first-steps.yaml        # lists questions
    fundamentals.yaml       # lists missions
  questions/
    absolute-path.yaml
    pwd-command.yaml
```

- A question is one file, `questions/<slug>.yaml`: it has no scripts or Markdown files to reference, so it needs no directory.
- A module lists missions **or** questions, never both: a module file has exactly one of `missions` and `questions`. A module that lists both, or neither, is refused.
- Every question is listed by exactly one module of questions, and every slug a module lists has a file in `questions/`. A question in no module, in two modules or twice in one module is an error, as for missions.

## Module of questions

```yaml
schema: 1
slug: first-steps
title: Primeiros passos
description: Conceitos básicos de terminal, diretórios, arquivos e permissões.
status: draft
questions:
  - current-directory
  - pwd-command
```

The order of `questions` is the order of the module, the one students follow. Nothing is locked: a student may open any published question of the module.

## Question file

```yaml
schema: 1
slug: absolute-path
type: single_choice
title: Caminho absoluto
summary: Diferenciar um caminho absoluto de um relativo.
difficulty: 1
estimated_minutes: 1
status: draft
tags: [paths]
question: |
  Qual destes caminhos é absoluto?
options:
  - id: relative
    text: "`projeto`"
    feedback: "Esse caminho é relativo: depende do diretório atual."
  - id: absolute
    text: "`/home/student/projeto`"
  - id: dot
    text: "`./projeto`"
    feedback: "`./` indica o diretório atual, então o caminho é relativo."
answer: absolute
explanation: |
  Um caminho absoluto começa em `/`, a raiz, e leva ao mesmo lugar de qualquer diretório.
hints:
  - "Olhe o primeiro caractere de cada caminho."
```

| Field | Rule |
|---|---|
| `schema` | The integer `1` |
| `slug` | `[a-z0-9-]`, up to 64 characters; the file name without `.yaml`. Never changes |
| `type` | `single_choice`, the only type |
| `title`, `summary` | Text, not blank. The summary is what a card shows |
| `difficulty` | Integer from 1 to 5 |
| `estimated_minutes` | Integer, at least 1 |
| `status` | `draft`, `published` or `archived` |
| `tags` | Slugs, each once; optional |
| `question` | The statement, Markdown, not blank |
| `options` | From 2 to 6 options, in the order shown |
| `options[].id` | `[a-z0-9-]`, up to 32 characters, unique in the question. The stable identifier an answer names and an attempt records; the letters students see (a, b, c...) are given by the interface from the position, so options can be reordered without changing what was recorded |
| `options[].text` | Markdown of one line, not blank; two options cannot have the same text |
| `options[].feedback` | Required on every wrong option, shown when it is chosen. The correct option has none: the explanation says why it is right |
| `answer` | The `id` of the correct option |
| `explanation` | Markdown, not blank. Shown after an answer, right or wrong |
| `hints` | Text, not blank; optional |

The sync checks all of it as data and refuses the whole content on any error, naming the file and the field (`questions/absolute-path.yaml: answer: no option has the id abs`). The rules of [missions.md](missions.md#content-rules) hold for question files too: UTF-8, line endings normalized to LF, safe YAML without duplicate keys or aliases, no symbolic links, values never coerced (`difficulty: "1"` is refused), unknown fields refused. A question file is at most 64 KiB.

## Versions

The specification of a version is every field of the file except `status`, the answer and the feedback included. Its SHA-256 (canonical JSON, as for missions) decides whether the sync creates a version: a changed statement, option, answer, feedback, explanation or hint does; a changed status, module or position does not. Versions are immutable (a trigger rejects `UPDATE` and `DELETE` on `question_versions`), a question removed from `content/` is archived with its versions, and a slug that comes back reuses its row ([architecture.md](architecture.md#questions)).

A question is visible when it is `published` and listed in a `published` module. A draft or archived question, or one in a module that is not published, does not exist for students: reading it or answering it is `404 question_not_found`, the same as an unknown slug.

## Answering

```
GET  /api/question-modules            modules of questions, cards, the user's progress
GET  /api/questions/{slug}            statement, options (id and text), hints, version, progress
POST /api/questions/{slug}/answers    {"version": 1, "option": "absolute"}
```

Details in [api.md](api.md#questions).

- **The answer is checked by the server.** The client sends the option's id and the version it shows, nothing else; a request with any other field is refused (`422`). Whether the answer is right is decided by the API from the stored version, never by the client.
- **Reading never reveals the answer.** The listing and the detail carry, for each option, its id and text only. The correct option, the feedback and the explanation are never in them, before or after the user completed the question. They leave the server only in the response to an answer: the chosen option's feedback when it is wrong, and the explanation in every case.
- **Every answer is kept.** Each answer the API checks is a new row in `question_attempts`: the user, the question, the version, the option chosen, whether it was right, and when. A new answer never changes or removes an earlier one. Answers refused before they are checked (`404`, `409`, `422`, `429`) record nothing.
- **Trying again is always allowed.** A wrong answer locks nothing; the student can answer again at once, and a completed question can be answered again.
- **Completion is the first correct answer.** It is recorded in `user_questions` in the same transaction as that attempt and never changes afterwards: later answers, right or wrong, do not undo it.

## Progress

```json
{ "status": "attempted", "attempts": 2, "completed_version": null, "completed_at": null }
```

| `status` | When |
|---|---|
| `completed` | A correct answer was recorded, on any version of the question |
| `attempted` | Not completed, and the user answered the current version |
| `not_answered` | Not completed, and no answer to the current version |

- `attempts` is the number of answers the user gave to the **current version**. Answers to earlier versions stay in `question_attempts` with their version, and are never counted with the current one.
- `completed_version` and `completed_at` are those of the first correct answer, fixed when it is recorded.
- A new version does not undo a completion, as for missions: an editorial change (a typo, a better feedback) must not take progress away. A change in what a question asks for gets a new slug.
- Progress is always the requesting user's, read from the session. No route takes a user id.

## When a question changes

Each answer names the version the student was shown. When it is not the current one (a sync published a new version since the page was loaded), the answer is refused with `409 question_outdated` and nothing is recorded: the client reloads the question and shows the new version. An answer is therefore always checked against the statement, options and answer key the student saw, and attempts of different versions are never mixed.

## Limits

- 30 answers per minute per user (`429 rate_limited`, with `Retry-After`), counted in the API process like the other limits ([architecture.md](architecture.md#questions)).
- With a few options a student can find the answer by elimination, and after a wrong answer the explanation is shown: completion means the student reached the right answer, not a score. There is no score, streak or ranking.

## Backups

The questions, their versions, the attempts and the completions are tables of the database and are in every backup. A backup's manifest records the questions' fingerprint next to the missions' (slug, status, current version and its `content_hash`), and a restore must find the same, or it fails ([operations.md](operations.md#backups)).
