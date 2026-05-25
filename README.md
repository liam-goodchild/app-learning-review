# Enhanced Learning App

A self-hosted learning review app for turning Obsidian notes into interactive retrieval practice. The MVP uses FastAPI, server-rendered Jinja templates, SQLite, a read-only Obsidian vault mount, manual question approval, and a simple spaced-repetition scheduler.

## What It Does

- Scans configured Obsidian vault folders for Markdown notes with `learning_status`.
- Imports notes marked `needs-questions`, `questions-drafted`, `active`, or `stale`.
- Extracts `## Practice questions` into draft short-answer questions.
- Lets you create, edit, approve, or retire questions.
- Schedules approved questions per question.
- Runs a daily review queue with simple interleaving across source notes.
- Supports multiple choice, short answer, rubric/self-scored, and categorisation questions.
- Records attempts, confidence, rating, feedback state, and next due date.
- Includes login, CSRF checks for form posts, Docker Compose, and SQLite backup scripts.

The MVP does not write back to the Obsidian vault. Mount the vault read-only by default.

## Local Development

```bash
cd /home/lgoodchild-a/app-learning-review
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

Open `http://localhost:8080`. The default login from `.env.example` is `admin` / `change-me`; change it before real use.

For local development without Docker, set `DATABASE_URL=sqlite:///./data/learning.db` and `VAULT_PATH` to your synced Obsidian vault path.

## Docker Compose

```bash
cd /home/lgoodchild-a/app-learning-review
cp .env.example .env
```

Edit `.env`:

```env
APP_SECRET_KEY=replace-with-a-long-random-value
APP_USERNAME=admin
APP_PASSWORD=replace-me
VAULT_HOST_PATH=/path/to/ObsidianVault
APP_PORT=8080
```

Start the app:

```bash
docker compose up -d --build
```

The compose file mounts:

- `${VAULT_HOST_PATH}` to `/vault:ro` for read-only Obsidian access.
- `learning_data` to `/data` for the SQLite database and backups.

## Obsidian Note Marker

The importer ignores notes unless they include a recognized `learning_status` frontmatter value. Example:

```yaml
---
title: Terraform Variables and Outputs
tags:
  - terraform
  - iac
status: inbox
confidence: medium
learning_status: needs-questions
learning_question_goal: 8
learning_question_types:
  - multiple-choice
  - short-answer
  - rubric
  - categorisation
---
```

Add a `## Practice questions` section to seed draft short-answer questions. Repeated scans are idempotent and notes moved between configured folders are matched by hash/source metadata where possible.


## AI Question Generation with Codex

The app can queue AI generation jobs without storing an API key. A host-side worker uses your authenticated Codex CLI subscription, calls the app worker API, and imports generated JSON as draft questions. Generated questions are never auto-approved.

Enable the worker API in `.env`:

```env
WORKER_TOKEN=replace-with-a-long-random-value
APP_BASE_URL=http://127.0.0.1:8080
```

Set `APP_BASE_URL` to match the host port in `APP_PORT`. On this VM the local `.env` currently uses port `8081` because `8080` was already allocated.

Rebuild/restart after changing `.env`:

```bash
docker compose up -d --build
```

From a source detail page, click **Generate with Codex** to queue a job. Then run the host worker from the repository directory:

```bash
scripts/process_generation_jobs.py --limit 5
```

For ad hoc generation from the app, leave the worker running in watch mode:

```bash
scripts/process_generation_jobs.py --watch --poll-seconds 10 --limit 1
```

For nightly processing, run the same script from cron or a systemd timer as the Linux user that is already logged in to Codex. The worker uses `codex exec --ephemeral --sandbox read-only -c approval_policy="never"` and validates the final JSON before creating draft questions.

A manual test path is available without calling Codex:

```bash
scripts/process_generation_jobs.py --fake-output tests/fixtures/generated_questions.json --limit 1
```

## Review Workflow

1. Click **Scan vault**.
2. Open **Sources** and inspect imported notes.
3. Edit draft questions as needed. For multiple choice, use JSON like:

```json
[
  {"id": "A", "text": "Use a local value", "correct": false, "feedback": "Locals are internal helpers."},
  {"id": "B", "text": "Use an input variable", "correct": true, "feedback": "Variables define caller-provided inputs."}
]
```

4. Approve questions. Approval creates an initial due schedule.
5. Open **Daily Review**, answer, read feedback, then rate `again`, `hard`, `good`, or `easy` and choose confidence.

Rubric questions use `rubric_json` like:

```json
[
  {"criterion": "Identifies variables as caller-provided module inputs", "points": 1},
  {"criterion": "Identifies locals as internal named expressions", "points": 1}
]
```

Categorisation questions use `options_json` like:

```json
{
  "categories": ["variable", "local", "output"],
  "items": [
    {"text": "Exposes a value to a parent module", "category": "output"},
    {"text": "Stores a repeated expression inside the module", "category": "local"}
  ]
}
```

## Authentication

The app is intended to be reachable only through Tailscale or a trusted local network, but it still includes username/password login and signed HTTP-only cookies. POST forms include CSRF tokens.

Set a real `APP_SECRET_KEY` and password in `.env`. You can also change the password from the Settings page after logging in.

For private tailnet access, expose the service with Tailscale Serve or by connecting directly to the VM Tailscale IP and `APP_PORT`. Do not expose this app directly to the public internet.

## Backups

Obsidian Sync does not back up the app database. Back up `/data/learning.db` regularly.

Inside the container:

```bash
docker compose exec app /app/scripts/backup_sqlite.sh
```

The script creates timestamped backups under `/data/backups` and keeps the latest 14 by default. Override with `KEEP_LAST_BACKUPS`.

Restore from a backup:

```bash
docker compose stop app
docker compose run --rm app /app/scripts/restore_sqlite.sh /data/backups/learning-YYYYMMDD-HHMMSS.db
docker compose up -d
```

The restore script saves a `.before-restore-*` copy of the existing database if one exists.

## Tests

```bash
python3 -m pytest -q
```

The tests cover frontmatter parsing, no-frontmatter and malformed-frontmatter files, scan idempotency, content hash updates, moved notes, question approval, scheduler ratings, review attempt recording, and interleaving.

## Configuration

Environment variables are loaded from `.env` if present. Important settings:

- `DATABASE_URL`: SQLite URL. Docker default is `sqlite:////data/learning.db`.
- `VAULT_PATH`: Container path for the vault, usually `/vault`.
- `VAULT_IMPORT_FOLDERS`: Comma-separated folders to scan.
- `MIN_FILE_AGE_SECONDS`: Skips recently modified files to avoid half-written Obsidian Sync updates.
- `SCAN_INTERVAL_SECONDS`: Background scan interval.
- `REVIEW_SESSION_SIZE`: Daily review cap.

## Repository Layout

```text
app/
  importer/      Markdown/frontmatter scanning
  scheduling/    Scheduler interface and simple fallback
  services/      Question and review workflows
  templates/     Server-rendered UI
  static/        CSS
scripts/         SQLite backup and restore
tests/           Importer, scheduler, and review flow tests
```
