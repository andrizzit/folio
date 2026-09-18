---
name: folio
description: Save the immediately preceding completed Codex response as local Markdown, open it in Folio's browser reader, or browse the flat category library. Use when the user invokes $folio or asks to save, render, reopen, or organize a previous response.
---

# Folio

Folio preserves the preceding completed response exactly from the current Codex
thread, stores it locally, and opens a readable browser view.

## Commands

Run `folio` from `PATH` (normally installed at `~/.local/bin/folio`).

- Normal invocation: run `folio capture`.
- A requested category: add `--category "<name>"`.
- A requested title: add `--title "<title>"`.
- Library-only requests: run `folio library` and do not capture.
- Health or troubleshooting requests: run `folio doctor` or `folio status`.

Treat `CATEGORY=...`, `TITLE=...`, and `LIBRARY=true` in the request as explicit
arguments. Natural-language equivalents are also valid. Default to the `Inbox`
category and infer the title when they are omitted. Do not ask a follow-up
question for an ordinary capture.

Folio's capture adapter reads only the session matching `CODEX_THREAD_ID` and
requires an `assistant` message with a recognized completed-response marker
(`phase: final_answer` in the current Codex build). Never write beneath
`~/.codex`, inspect Codex SQLite databases, or reconstruct the prior response
from memory.

The CLI starts or reuses a loopback-only server and attempts to open the saved
page. If browser launching is blocked but capture succeeds, report the printed
URL and source path; do not run capture again because every capture is an
intentional immutable library entry.

Keep the completion concise: state the title, category, Markdown path, and URL.
If exact capture is unavailable, fail clearly rather than silently saving a
best-effort reconstruction.
