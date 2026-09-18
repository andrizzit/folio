# Folio

Folio 0.3.0 is a private local reading library for AI responses and Markdown.
It saves content as durable Markdown and renders it in a readable browser
interface.

Codex has native exact-response capture. Responses from Claude, Grok, ChatGPT,
Gemini, and other tools are manual imports through a file, stdin, the system
clipboard, or the web library. Imported provider labels are user-supplied and
are never presented as native or verified provenance.

Folio's mark is a folio page containing a terminal prompt:

```text
▤  Folio
```

Folio uses only the Python standard library. It has no provider API
integration, accounts, provider credentials, telemetry, cloud service,
database, frontend build, hooks, or permanently installed background daemon.
Its web server binds only to `127.0.0.1`, starts lazily, and is reused until
stopped or the process exits.

## Support

| Source | How content enters Folio | Library label |
| --- | --- | --- |
| Codex CLI | Native exact capture with `folio` or `folio capture` | Codex · Native |
| Claude | File, stdin, clipboard, or web paste | Claude · Imported |
| Grok | File, stdin, clipboard, or web paste | Grok · Imported |
| ChatGPT | File, stdin, clipboard, or web paste | ChatGPT · Imported |
| Gemini | File, stdin, clipboard, or web paste | Gemini · Imported |
| Other or custom source | File, stdin, clipboard, or web paste | User-supplied label · Imported |

Categories remain flat and independent from providers. The library can filter
by provider without changing an entry's category.

## Requirements and installation

- Python 3.11 or newer
- macOS or another Unix-like system with `fcntl`

Clone or copy the repository, then run:

```bash
./install.sh
```

The installer links `folio` into `~/.local/bin` and copies the optional Codex
skill into `~/.codex/skills/folio`. Ensure `~/.local/bin` is on `PATH`.

## Codex fast path

Inside the Codex CLI, use its direct shell-command prefix:

```text
!folio
!folio --category "Project Alpha" --title "API notes"
!folio library
```

`!folio` executes Folio directly without an additional agent/model turn. The
optional `$folio` skill supports natural-language requests, but it is not the
fast path.

`folio` and `folio capture` save the exact preceding completed Codex response.
Capture fails closed if Folio cannot identify one unambiguous completed
response; it never reconstructs an answer from memory or captures commentary.

## Manual imports

`folio add` accepts exactly one input method: `--source FILE`, `--stdin`, or
`--clipboard`. Use `--agent` to identify the source and optionally
`--source-label` to control its display name.

```bash
folio add --source response.md --agent claude --category "Research"
agent-command --markdown | folio add --stdin --agent grok
folio add --clipboard --agent chatgpt --title "Design review"
folio add --source answer.md --agent custom --source-label "Local model"
```

Clipboard access occurs only after an explicit `--clipboard` invocation. New
file imports retain only the source basename in metadata, not the absolute
input path.

To paste content in the browser, run `folio library` and use the import form.
Choose a provider or enter a custom label before saving. Browser-pasted content
is also marked as imported.

## Commands

```bash
folio
folio capture --no-open
folio add --source response.md --agent claude --no-open
folio add --stdin --agent grok
folio add --clipboard --agent chatgpt
folio library --no-open
folio serve --port 8765
folio status
folio stop
folio doctor
```

`add`, `capture`, and `library` start or reuse the loopback-only server and
open the relevant page unless `--no-open` is present. `folio stop` shuts it
down. The next applicable command starts it again; Folio does not install an
always-on system service.

Commands print JSON containing the saved entry details and local reader URL.

## Library

The web library provides:

- readable response pages with light and dark themes;
- provider badges and provider filtering;
- title and content search;
- flat categories, category creation, and category moves;
- category deletion that first moves its entries to Inbox;
- confirmed permanent response deletion;
- code copy controls, tables of contents, print styles, and safe Markdown.

Existing Folio entries remain readable. New entries use the cross-agent source
metadata while legacy Codex and file-import metadata is normalized when read,
without rewriting the library at startup.

## Data and privacy

The default data directory is:

```text
~/Library/Application Support/Folio/
├── library/
│   ├── inbox/
│   │   ├── category.json
│   │   ├── <entry-id>.md
│   │   └── <entry-id>.json
│   └── <flat-category-id>/
├── locks/
└── run/
    ├── server.json
    └── server.log
```

Set `FOLIO_HOME` to use another location. Saved responses are not stored in the
Git repository.

Folio escapes raw HTML, restricts active links to `http`, `https`, and
`mailto`, serves no external assets, and does not expose arbitrary local
files. The server rejects non-loopback Host headers and uses restrictive
browser security headers. Every content and API route requires a random
per-server capability token. Folio stores that token in its private
`server.json`, uses it in a browser bootstrap URL, then redirects into an
unguessable per-instance path with a per-instance HttpOnly, same-site session
cookie scoped to that path. This prevents old Folio cookies from shadowing the
current session and prevents the browser from sending the token to unrelated
localhost services. Category changes, imports, and deletions additionally
require same-origin requests.

Treat a printed Folio URL as private while that server instance is running:
the bootstrap URL grants access to the local library, but is never sent to an
AI provider or external service.

Library paths and identifiers are validated before use. Writes are serialized,
flushed, and atomically replaced. Folio keeps its data private with directory
mode `0700` and file mode `0600`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
