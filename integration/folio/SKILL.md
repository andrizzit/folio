---
name: folio
description: Save the immediately preceding completed Codex response exactly, open Folio's local browser library, or import explicitly supplied AI or Markdown content. Use when the user invokes $folio or asks to save, read, import, or organize responses in Folio.
---

# Folio

Use the installed `folio` executable to preserve responses in Folio's private
local Markdown library.

## Choose the operation

- Save the preceding completed Codex response: run `folio capture`.
- Open or browse the library without saving: run `folio library`.
- Check the local service: run `folio status` or `folio doctor`.
- Import content the user explicitly identifies: run `folio add` with exactly
  one of `--source FILE`, `--stdin`, or `--clipboard`.

Add `--category "<name>"` or `--title "<title>"` when requested. Otherwise,
default to Inbox and let Folio infer the title. Do not ask a follow-up question
for an ordinary capture. Treat `CATEGORY=...`, `TITLE=...`, and `LIBRARY=true`
in a request as explicit arguments.

## Native Codex capture

Codex is Folio's only native exact-capture adapter. It identifies the completed
response in the current Codex session and rejects ambiguous, malformed, or
commentary records.

Never reconstruct the preceding response from memory, silently substitute
clipboard content, write beneath `~/.codex`, or inspect Codex databases. If
exact capture is unavailable, report the failure clearly.

Every successful capture creates an intentional immutable entry. If browser
opening fails after a successful capture, report Folio's printed URL rather
than capturing again.

For the fastest interactive path, users can invoke `!folio` directly in Codex;
that bypasses an additional skill/model turn.

## Explicit imports

Examples:

```bash
folio add --source response.md --agent claude
folio add --stdin --agent grok
folio add --clipboard --agent chatgpt
folio add --source answer.md --agent custom --source-label "Local model"
```

Use clipboard ingestion only when the user explicitly asks to import the
current clipboard. Do not invoke provider APIs, request API keys, install
hooks, or describe imported Claude, Grok, ChatGPT, Gemini, or custom content as
native. Provider and custom labels on manual imports are user-supplied
metadata, not verified provenance.

For a browser paste, run `folio library` and direct the user to the local
import form. Folio's server is loopback-only, starts or is reused on demand,
and is not a permanently installed system daemon.

## Report completion

Keep the result concise. For saved content, state the title, category,
Markdown path, source label, and local URL. For library-only requests, report
the local library URL.
