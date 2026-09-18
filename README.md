# Folio

Folio turns completed Codex responses and explicit Markdown files into a private,
durable local library with a readable browser interface.

Its mark is a folio page containing a terminal prompt:

```text
▤  Folio
```

Folio is a Python standard-library application. It has no package-manager
installation, cloud service, account, telemetry, database, or frontend build.
The web server binds only to `127.0.0.1`.

## Requirements

- Python 3.11 or newer (developed on Python 3.14)
- macOS or another Unix-like system with `fcntl`

## Install

Clone or copy this repository, then run:

```bash
./install.sh
```

The installer links `folio` into `~/.local/bin` and copies the optional Codex
skill into `~/.codex/skills/folio`. Ensure `~/.local/bin` is on `PATH`.

## Commands

Inside the Codex CLI, use its direct shell-command prefix for the fastest path:

```text
!folio
!folio --category "Project Alpha" --title "API notes"
!folio library
```

These commands execute Folio directly without an agent/model turn. The
`$folio` skill remains available when natural-language interpretation is more
useful. Folio intentionally does not install a custom `/folio` prompt because
custom prompts still require an agent/model turn; `!folio` is the fast path.

The executable interface is:

```bash
folio capture --no-open
folio capture --category "Project Alpha" --title "API notes"
folio add --source response.md --category "Research" --no-open
folio library --no-open
folio serve --port 8765
folio status
folio stop
folio doctor
```

`capture` requires either `--thread-id ID` or `CODEX_THREAD_ID`. It searches
read-only beneath `~/.codex/sessions`. A candidate must contain the thread ID in
its filename, be a non-symlink regular file contained beneath the resolved
sessions root, and declare the exact same ID in `session_meta`. Folio requires
exactly one such JSONL session and saves the latest completed assistant message
whose phase is `final_answer`. The older `final` phase is accepted as a
compatibility alias; commentary is never captured.

`add`, `capture`, and `library` start or reuse a detached local server. They
open the relevant page unless `--no-open` is present. All commands print JSON,
including the durable source path and local reader URL.

## Data

The default data directory is:

```text
~/Library/Application Support/Folio/
├── library/
│   ├── inbox/
│   │   ├── category.json
│   │   ├── <entry-id>.md
│   │   └── <entry-id>.json
│   └── <flat-category-id>/
│       └── ...
├── locks/
└── run/
    ├── server.json
    └── server.log
```

Set `FOLIO_HOME` to use another location. Tests always use an isolated
temporary directory. `FOLIO_CODEX_SESSIONS` can point capture tests at fixture
sessions without touching the real Codex directory.

Each capture creates a new immutable Markdown entry, even when content is
identical. Adjacent JSON stores the title, display category, creation time,
source thread and JSONL line, SHA-256, and stable `/entry/<id>` reader path.
Category display names never become raw paths. Categories are flat,
case-insensitively unique, and reject `/` and `\`.

Writes are serialized with `flock`, flushed with `fsync`, and committed with
atomic replacement. An orphaned Markdown file from an interrupted metadata
write is ignored by the library scanner. Folio validates every category,
metadata, and Markdown path before reading it. Entry Markdown must be exactly
`<validated-entry-id>.md` in a direct, non-symlink category directory.

Folio creates and migrates its data, category, run, and lock directories to
mode `0700`. Markdown, metadata, state, log, and lock files use mode `0600`.

## Reader and security

The server renders HTML on demand and provides:

- newest-first response cards;
- flat category navigation and title/content search;
- category moves from the response reader, including creation of a new flat
  category by typing its name;
- category deletion from the library, with every saved response moved safely
  to Inbox before the category is removed;
- confirmed permanent response deletion from library cards;
- readable prose width, responsive tables, and horizontally scrolling code;
- code copy controls, heading links, table of contents, print CSS, and
  persistent light/dark theme;
- headings, paragraphs, emphasis, inline and fenced code, lists, blockquotes,
  links, and simple tables.

Raw HTML is escaped. Links are active only for `http`, `https`, and `mailto`.
Responses include a restrictive Content Security Policy, no external assets,
no executable response HTML, and no route that exposes arbitrary local files.
Every request must carry a Host header containing `127.0.0.1` or `localhost`
with Folio's active port. Responses also set same-origin resource isolation and
a restrictive Permissions Policy. Category changes require a same-origin JSON
request with Folio's custom request header. Request logging is disabled so
searches cannot enter the server log.

Server state contains a random instance identifier. Status and stop operations
validate that identifier against loopback health before trusting a PID, which
avoids signalling a process referenced by a stale or reused PID file.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Coverage includes immutable storage, category validation, search ordering,
safe Markdown, malicious links and HTML, canonical `final_answer` capture,
commentary exclusion, compatibility with `final`, capture ambiguity/schema
failures, wrong-thread and symlink rejection, read-only fixture capture,
metadata path containment, symlinked Markdown rejection, private permission
migration, bounded deeply nested blockquotes, safe category deletion, confirmed
response deletion, hostile Host rejection, silent request handling, HTTP
health/library/reader/static routes, security headers, 404 behavior, and stale
server-state recovery.
