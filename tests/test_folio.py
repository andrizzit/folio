from __future__ import annotations

import json
import http.client
import io
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import folio  # noqa: E402


class TemporaryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "folio-home"
        self.store = folio.FolioStore(self.home)

    def test_add_creates_new_immutable_entries_and_metadata(self) -> None:
        first = self.store.add_entry("# Hello\n\nA response.", category_name="Project Alpha")
        second = self.store.add_entry("# Hello\n\nA response.", category_name="project alpha")

        self.assertNotEqual(first["metadata"]["id"], second["metadata"]["id"])
        self.assertEqual(first["metadata"]["category"], second["metadata"]["category"])
        self.assertEqual(first["metadata"]["category"]["name"], "Project Alpha")
        self.assertTrue(Path(first["markdown_path"]).is_file())
        self.assertEqual(
            Path(first["markdown_path"]).read_text(encoding="utf-8"),
            "# Hello\n\nA response.",
        )
        self.assertEqual(
            first["metadata"]["sha256"],
            folio.hashlib.sha256(b"# Hello\n\nA response.").hexdigest(),
        )
        self.assertEqual(
            first["metadata"]["reader_path"],
            f"/entry/{first['metadata']['id']}",
        )
        self.assertEqual(first["metadata"]["version"], 2)
        self.assertEqual(first["metadata"]["source"]["provider"], "generic")
        self.assertEqual(first["metadata"]["source"]["support_level"], "imported")

    def test_category_separators_are_rejected_without_entry_files(self) -> None:
        with self.assertRaisesRegex(folio.FolioError, "cannot contain"):
            self.store.add_entry("content", category_name="Parent/Child")
        with self.assertRaisesRegex(folio.FolioError, "cannot contain"):
            self.store.add_entry("content", category_name=r"Parent\Child")
        entry_markdown = list(self.store.library_dir.glob("*/*.md"))
        self.assertEqual(entry_markdown, [])

    def test_search_and_newest_first(self) -> None:
        first = self.store.add_entry("# Older\n\nNeedle one.")
        second = self.store.add_entry("# Newer\n\nNeedle two.")
        entries = self.store.load_entries(query="needle")
        self.assertEqual([entry["id"] for entry in entries], [second["metadata"]["id"], first["metadata"]["id"]])
        self.assertEqual(self.store.load_entries(query="two")[0]["title"], "Newer")

    def test_provider_metadata_and_filtering(self) -> None:
        claude_source = folio.source_descriptor(
            agent="claude",
            support_level="imported",
            transport="clipboard",
            adapter="folio-clipboard-v1",
        )
        codex_source = folio.source_descriptor(
            agent="codex",
            support_level="native",
            transport="session",
            adapter="codex-session-v1",
            conversation_id="thread-123",
        )
        claude = self.store.add_entry("# Claude", source=claude_source)
        codex = self.store.add_entry("# Codex", source=codex_source)

        entries = self.store.load_entries(provider_id="claude")

        self.assertEqual([entry["id"] for entry in entries], [claude["metadata"]["id"]])
        self.assertEqual(entries[0]["_source"]["label"], "Claude")
        self.assertEqual(
            self.store.load_entries(query="Codex · Native")[0]["id"],
            codex["metadata"]["id"],
        )

    def test_legacy_source_metadata_remains_readable(self) -> None:
        saved = self.store.add_entry("# Legacy")
        metadata_path = Path(saved["metadata_path"])
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["version"] = 1
        metadata["source"] = {
            "kind": "codex",
            "thread_id": "legacy-thread",
            "session_file": "legacy.jsonl",
            "record_line": 3,
            "phase": "final_answer",
        }
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        entry = self.store.load_entries()[0]

        self.assertEqual(entry["_source"]["provider"], "codex")
        self.assertEqual(entry["_source"]["support_level"], "native")
        self.assertEqual(self.store.get_entry(metadata["id"])[1], "# Legacy")

    def test_unknown_metadata_version_fails_closed(self) -> None:
        saved = self.store.add_entry("# Future")
        metadata_path = Path(saved["metadata_path"])
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["version"] = 99
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        self.assertEqual(self.store.load_entries(), [])
        with self.assertRaisesRegex(folio.FolioError, "unsupported version"):
            self.store.get_entry(metadata["id"])

    def test_source_metadata_rejects_unexpected_secret_fields(self) -> None:
        source = folio.source_descriptor(
            agent="claude",
            support_level="imported",
            transport="stdin",
            adapter="folio-stdin-v1",
        )
        source["api_key"] = "canary-secret"

        with self.assertRaisesRegex(folio.FolioError, "unsupported fields"):
            self.store.add_entry("# Reject", source=source)

        self.assertEqual(self.store.load_entries(), [])

    def test_native_provenance_requires_exact_codex_session_tuple(self) -> None:
        with self.assertRaisesRegex(folio.FolioError, "reserved"):
            folio.source_descriptor(
                agent="claude",
                support_level="native",
                transport="clipboard",
                adapter="folio-clipboard-v1",
                conversation_id="forged",
            )
        with self.assertRaisesRegex(folio.FolioError, "reserved"):
            folio.source_descriptor(
                agent="codex",
                source_label="Definitely Codex",
                support_level="native",
                transport="session",
                adapter="codex-session-v1",
                conversation_id="forged",
            )

    def test_move_entry_creates_category_and_preserves_reader_url(self) -> None:
        saved = self.store.add_entry("# Move me")
        entry_id = saved["metadata"]["id"]
        original_reader_path = saved["metadata"]["reader_path"]

        moved = self.store.move_entry(entry_id, "Project Alpha")

        self.assertTrue(moved["moved"])
        self.assertEqual(moved["metadata"]["category"]["name"], "Project Alpha")
        self.assertEqual(moved["metadata"]["reader_path"], original_reader_path)
        self.assertFalse(Path(saved["markdown_path"]).exists())
        self.assertFalse(Path(saved["metadata_path"]).exists())
        self.assertTrue(Path(moved["markdown_path"]).is_file())
        self.assertTrue(Path(moved["metadata_path"]).is_file())
        metadata, markdown = self.store.get_entry(entry_id)
        self.assertEqual(metadata["category"]["name"], "Project Alpha")
        self.assertEqual(markdown, "# Move me")

        unchanged = self.store.move_entry(entry_id, "project alpha")
        self.assertFalse(unchanged["moved"])

    def test_delete_category_moves_entries_to_inbox(self) -> None:
        first = self.store.add_entry("# First", category_name="Project Alpha")
        second = self.store.add_entry("# Second", category_name="Project Alpha")
        category_id = first["metadata"]["category"]["id"]
        category_dir = Path(first["metadata_path"]).parent

        deleted = self.store.delete_category(category_id)

        self.assertEqual(deleted["category"]["name"], "Project Alpha")
        self.assertEqual(deleted["moved_count"], 2)
        self.assertEqual(deleted["destination"]["name"], "Inbox")
        self.assertFalse(category_dir.exists())
        self.assertNotIn(category_id, {item["id"] for item in self.store.categories()})
        for saved in (first, second):
            metadata, markdown = self.store.get_entry(saved["metadata"]["id"])
            self.assertEqual(metadata["category"]["name"], "Inbox")
            self.assertEqual(metadata["reader_path"], saved["metadata"]["reader_path"])
            self.assertIn(metadata["title"], markdown)

    def test_inbox_cannot_be_deleted(self) -> None:
        with self.assertRaisesRegex(folio.FolioError, "Inbox cannot"):
            self.store.delete_category("inbox")

    def test_category_with_unrecognized_files_is_not_deleted(self) -> None:
        saved = self.store.add_entry("# Keep me", category_name="Project Alpha")
        category_dir = Path(saved["metadata_path"]).parent
        unexpected = category_dir / "notes.txt"
        unexpected.write_text("do not remove", encoding="utf-8")

        with self.assertRaisesRegex(folio.FolioError, "unrecognized files"):
            self.store.delete_category(saved["metadata"]["category"]["id"])

        self.assertTrue(category_dir.is_dir())
        self.assertTrue(unexpected.is_file())
        metadata, _markdown = self.store.get_entry(saved["metadata"]["id"])
        self.assertEqual(metadata["category"]["name"], "Project Alpha")

    def test_delete_entry_removes_markdown_and_metadata(self) -> None:
        saved = self.store.add_entry("# Delete me", category_name="Project Alpha")
        other = self.store.add_entry("# Keep me", category_name="Project Alpha")

        deleted = self.store.delete_entry(saved["metadata"]["id"])

        self.assertEqual(deleted["metadata"]["title"], "Delete me")
        self.assertFalse(Path(saved["markdown_path"]).exists())
        self.assertFalse(Path(saved["metadata_path"]).exists())
        with self.assertRaisesRegex(folio.FolioError, "Entry not found"):
            self.store.get_entry(saved["metadata"]["id"])
        metadata, markdown = self.store.get_entry(other["metadata"]["id"])
        self.assertEqual(metadata["title"], "Keep me")
        self.assertEqual(markdown, "# Keep me")

    def test_metadata_cannot_redirect_markdown_read(self) -> None:
        saved = self.store.add_entry("# Safe\n\nLibrary content.")
        metadata_path = Path(saved["metadata_path"])
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        secret_path = Path(self.temp.name) / "outside-secret.md"
        secret_path.write_text("OUTSIDE SECRET", encoding="utf-8")
        metadata["markdown_file"] = str(secret_path)
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        self.assertEqual(self.store.load_entries(), [])
        with self.assertRaisesRegex(folio.FolioError, "filename"):
            self.store.get_entry(saved["metadata"]["id"])

    def test_malformed_metadata_is_skipped_without_rendering_crashes(self) -> None:
        mutations = {
            "title": 42,
            "created_at": {"not": "a timestamp"},
            "reader_path": "/entry/someone-else",
        }
        for field, invalid_value in mutations.items():
            with self.subTest(field=field):
                saved = self.store.add_entry(f"# Invalid {field}")
                metadata_path = Path(saved["metadata_path"])
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata[field] = invalid_value
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

                self.assertNotIn(
                    metadata["id"],
                    {entry["id"] for entry in self.store.load_entries()},
                )
                with self.assertRaises(folio.FolioError):
                    self.store.get_entry(metadata["id"])

    def test_malformed_legacy_source_is_rejected_as_folio_error(self) -> None:
        saved = self.store.add_entry("# Legacy invalid")
        metadata_path = Path(saved["metadata_path"])
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["version"] = 1
        metadata["source"] = {
            "kind": "codex",
            "thread_id": 123,
            "session_file": "session.jsonl",
            "record_line": 1,
            "phase": "final_answer",
        }
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        self.assertEqual(self.store.load_entries(), [])
        with self.assertRaisesRegex(folio.FolioError, "thread ID"):
            self.store.get_entry(metadata["id"])

    def test_markdown_symlink_is_never_read(self) -> None:
        saved = self.store.add_entry("# Safe")
        markdown_path = Path(saved["markdown_path"])
        outside_path = Path(self.temp.name) / "outside.md"
        outside_path.write_text("OUTSIDE SECRET", encoding="utf-8")
        markdown_path.unlink()
        markdown_path.symlink_to(outside_path)

        self.assertEqual(self.store.load_entries(), [])
        with self.assertRaisesRegex(folio.FolioError, "symlink"):
            self.store.get_entry(saved["metadata"]["id"])

    def test_private_permissions_are_created_and_migrated(self) -> None:
        home = Path(self.temp.name) / "existing-home"
        inbox = home / "library" / "inbox"
        run_dir = home / "run"
        locks_dir = home / "locks"
        inbox.mkdir(parents=True)
        run_dir.mkdir()
        locks_dir.mkdir()
        category_file = inbox / "category.json"
        category_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "id": "inbox",
                    "name": "Inbox",
                    "created_at": "2026-09-18T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        log_file = run_dir / "server.log"
        log_file.write_text("old log", encoding="utf-8")
        lock_file = locks_dir / "library.lock"
        lock_file.write_text("", encoding="utf-8")
        for directory in (home, home / "library", inbox, run_dir, locks_dir):
            directory.chmod(0o755)
        for file_path in (category_file, log_file, lock_file):
            file_path.chmod(0o644)

        store = folio.FolioStore(home)
        store.ensure_layout()
        saved = store.add_entry("# Private")
        with folio.exclusive_lock(store.server_lock_path):
            pass

        for directory in (home, home / "library", inbox, run_dir, locks_dir):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for file_path in (
            category_file,
            log_file,
            lock_file,
            store.server_lock_path,
            Path(saved["markdown_path"]),
            Path(saved["metadata_path"]),
        ):
            self.assertEqual(stat.S_IMODE(file_path.stat().st_mode), 0o600)


class MarkdownTest(unittest.TestCase):
    def test_safe_markdown_rendering_and_features(self) -> None:
        source = """# Heading

Raw <script>alert("x")</script> and **strong** plus *em* and `code`.

[safe](https://example.com) [bad](javascript:alert(1))

> quoted

- one
- two

| A | B |
| --- | --- |
| 1 | 2 |

```python
print("<unsafe>")
```
"""
        rendered, headings = folio.render_markdown(source)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("<strong>strong</strong>", rendered)
        self.assertIn("<em>em</em>", rendered)
        self.assertIn('href="https://example.com"', rendered)
        self.assertNotIn('href="javascript:', rendered)
        self.assertIn('class="unsafe-link"', rendered)
        self.assertIn("<blockquote>", rendered)
        self.assertIn("<ul>", rendered)
        self.assertIn("<table>", rendered)
        self.assertIn("&lt;unsafe&gt;", rendered)
        self.assertIn("data-copy-code", rendered)
        self.assertEqual(headings[0][1], "Heading")

    def test_link_scheme_restrictions(self) -> None:
        self.assertEqual(folio.safe_link("mailto:test@example.com"), "mailto:test@example.com")
        self.assertIsNone(folio.safe_link("javascript:alert(1)"))
        self.assertIsNone(folio.safe_link("/relative"))
        self.assertIsNone(folio.safe_link("https:///missing-host"))

    def test_deep_blockquotes_are_bounded(self) -> None:
        source = ("> " * 1200) + "still safe"
        rendered, _headings = folio.render_markdown(source)
        self.assertIn("still safe", rendered)
        self.assertLessEqual(rendered.count("<blockquote>"), folio.MAX_BLOCKQUOTE_DEPTH + 1)


class CodexCaptureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sessions = Path(self.temp.name) / "sessions"
        self.sessions.mkdir()
        self.thread_id = "thread-123"

    def write_session(self, name: str, records: list[dict]) -> Path:
        path = self.sessions / name
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        return path

    def test_final_answer_is_canonical_and_commentary_is_rejected(self) -> None:
        path = self.write_session(
            f"rollout-{self.thread_id}.jsonl",
            [
                {"type": "session_meta", "payload": {"id": self.thread_id}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": "working"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "# Exact answer"}],
                    },
                },
            ],
        )
        markdown, source = folio.extract_latest_final(path)
        self.assertEqual(markdown, "# Exact answer")
        self.assertEqual(source["phase"], "final_answer")
        self.assertEqual(source["record_line"], 3)

    def test_final_phase_is_accepted_for_compatibility(self) -> None:
        path = self.write_session(
            f"rollout-{self.thread_id}.jsonl",
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final",
                        "content": [{"type": "output_text", "text": "compatible"}],
                    },
                }
            ],
        )
        markdown, source = folio.extract_latest_final(path)
        self.assertEqual(markdown, "compatible")
        self.assertEqual(source["phase"], "final")

    def test_only_commentary_fails_closed(self) -> None:
        path = self.write_session(
            f"rollout-{self.thread_id}.jsonl",
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": "not final"}],
                    },
                }
            ],
        )
        with self.assertRaisesRegex(folio.FolioError, "No completed"):
            folio.extract_latest_final(path)

    def test_capture_uses_exactly_one_read_only_session(self) -> None:
        session = self.write_session(
            f"rollout-{self.thread_id}.jsonl",
            [
                {"type": "session_meta", "payload": {"id": self.thread_id}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Preserve  spaces\nand lines.",
                            }
                        ],
                    },
                },
            ],
        )
        before = session.read_bytes()
        with mock.patch.dict(
            os.environ,
            {
                "FOLIO_CODEX_SESSIONS": str(self.sessions),
                "CODEX_THREAD_ID": self.thread_id,
            },
            clear=False,
        ):
            markdown, source = folio.capture_response()
        self.assertEqual(markdown, "Preserve  spaces\nand lines.")
        self.assertEqual(source["provider"], "codex")
        self.assertEqual(source["support_level"], "native")
        self.assertEqual(source["conversation_id"], self.thread_id)
        self.assertEqual(source["locator"]["phase"], "final_answer")
        self.assertEqual(session.read_bytes(), before)

    def test_ambiguous_sessions_fail_closed(self) -> None:
        record = {"type": "session_meta", "payload": {"id": self.thread_id}}
        self.write_session(f"one-{self.thread_id}.jsonl", [record])
        self.write_session(f"two-{self.thread_id}.jsonl", [record])
        with self.assertRaisesRegex(folio.FolioError, "exactly one"):
            folio.find_session_file(self.thread_id, self.sessions)

    def test_filename_match_requires_exact_session_meta_id(self) -> None:
        self.write_session(
            f"rollout-{self.thread_id}.jsonl",
            [{"type": "session_meta", "payload": {"id": "different-thread"}}],
        )
        with self.assertRaisesRegex(folio.FolioError, "found 0"):
            folio.find_session_file(self.thread_id, self.sessions)

    def test_session_meta_fallback_scan_is_not_permitted(self) -> None:
        self.write_session(
            "unrelated-filename.jsonl",
            [{"type": "session_meta", "payload": {"id": self.thread_id}}],
        )
        with self.assertRaisesRegex(folio.FolioError, "found 0"):
            folio.find_session_file(self.thread_id, self.sessions)

    def test_symlink_session_candidate_is_rejected(self) -> None:
        outside = Path(self.temp.name) / "outside.jsonl"
        outside.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": self.thread_id}}) + "\n",
            encoding="utf-8",
        )
        candidate = self.sessions / f"rollout-{self.thread_id}.jsonl"
        candidate.symlink_to(outside)
        with self.assertRaisesRegex(folio.FolioError, "symlink"):
            folio.find_session_file(self.thread_id, self.sessions)

    def test_schema_mismatch_fails_closed(self) -> None:
        path = self.sessions / f"rollout-{self.thread_id}.jsonl"
        path.write_text("{not json}\n", encoding="utf-8")
        with self.assertRaisesRegex(folio.FolioError, "invalid JSON"):
            folio.extract_latest_final(path)

    def test_untyped_text_is_not_treated_as_output(self) -> None:
        path = self.write_session(
            f"rollout-{self.thread_id}.jsonl",
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"text": "wrong schema"}],
                    },
                }
            ],
        )
        with self.assertRaisesRegex(folio.FolioError, "has no text"):
            folio.extract_latest_final(path)


class IngestionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = folio.FolioStore(Path(self.temp.name) / "home")

    def test_stdin_import_preserves_text_and_labels_agent(self) -> None:
        args = folio.build_parser().parse_args(
            [
                "add",
                "--stdin",
                "--agent",
                "claude",
                "--category",
                "Research",
                "--no-open",
            ]
        )
        with (
            mock.patch.object(folio.sys, "stdin", io.StringIO("# Exact\n\nText  here.")),
            mock.patch.object(
                folio,
                "ensure_server",
                return_value={"port": 8765},
            ),
        ):
            result = folio.execute(args, self.store)

        self.assertEqual(result["agent"], "Claude")
        metadata, markdown = self.store.get_entry(result["entry_id"])
        self.assertEqual(markdown, "# Exact\n\nText  here.")
        self.assertEqual(metadata["category"]["name"], "Research")
        self.assertEqual(metadata["source"]["transport"], "stdin")
        self.assertEqual(metadata["source"]["support_level"], "imported")

    def test_file_import_stores_basename_not_absolute_path(self) -> None:
        source_path = Path(self.temp.name) / "private" / "response.md"
        source_path.parent.mkdir()
        source_path.write_text("# File response", encoding="utf-8")
        args = folio.build_parser().parse_args(
            ["add", "--source", str(source_path), "--agent", "grok", "--no-open"]
        )
        with mock.patch.object(
            folio,
            "ensure_server",
            return_value={"port": 8765},
        ):
            result = folio.execute(args, self.store)

        metadata, _markdown = self.store.get_entry(result["entry_id"])
        self.assertEqual(metadata["source"]["provider"], "grok")
        self.assertEqual(metadata["source"]["locator"], {"file_name": "response.md"})
        self.assertNotIn(str(source_path.parent), json.dumps(metadata))

    def test_file_import_rejects_symlink_source(self) -> None:
        secret = Path(self.temp.name) / "secret.md"
        secret.write_text("CANARY-LOCAL-SECRET", encoding="utf-8")
        source_path = Path(self.temp.name) / "response.md"
        source_path.symlink_to(secret)

        with self.assertRaisesRegex(folio.FolioError, "safely"):
            folio.read_markdown_file(source_path)

    def test_clipboard_import_uses_explicit_local_reader(self) -> None:
        with (
            mock.patch.object(
                folio.shutil,
                "which",
                side_effect=lambda name: "/usr/bin/pbpaste" if name == "pbpaste" else None,
            ),
            mock.patch.object(
                folio,
                "read_process_output_bounded",
                return_value=b"# Clipboard\n\nCopied response.",
            ) as bounded_read,
        ):
            markdown = folio.read_clipboard_markdown()

        self.assertEqual(markdown, "# Clipboard\n\nCopied response.")
        bounded_read.assert_called_once_with(
            ["/usr/bin/pbpaste"],
            "Clipboard content",
            timeout=8,
        )

    def test_process_output_reader_stops_at_ingest_limit(self) -> None:
        command = [
            sys.executable,
            "-c",
            (
                "import os; "
                f"chunk=b'x'*65536; remaining={folio.MAX_INGEST_BYTES + 1}; "
                "\nwhile remaining:"
                "\n size=min(len(chunk), remaining)"
                "\n os.write(1, chunk[:size])"
                "\n remaining-=size"
            ),
        ]
        with self.assertRaisesRegex(folio.FolioError, "exceeds"):
            folio.read_process_output_bounded(
                command,
                "Clipboard content",
                timeout=5,
            )

    def test_invalid_utf8_and_oversized_input_fail_closed(self) -> None:
        with self.assertRaisesRegex(folio.FolioError, "valid UTF-8"):
            folio.read_stdin_markdown(io.BytesIO(b"\xff"))
        with self.assertRaisesRegex(folio.FolioError, "exceeds"):
            folio.read_stdin_markdown(
                io.BytesIO(b"x" * (folio.MAX_INGEST_BYTES + 1))
            )

    def test_unsupported_native_adapter_gives_import_guidance(self) -> None:
        with self.assertRaisesRegex(folio.FolioError, "no native capture adapter"):
            folio.capture_agent_response("claude")

    def test_add_requires_exactly_one_input(self) -> None:
        parser = folio.build_parser()
        with (
            mock.patch.object(folio.sys, "stderr", io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            parser.parse_args(["add", "--agent", "claude"])
        with (
            mock.patch.object(folio.sys, "stderr", io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            parser.parse_args(["add", "--stdin", "--clipboard"])

    def test_capture_flags_remain_compatible(self) -> None:
        args = folio.build_parser().parse_args(
            [
                "capture",
                "--agent",
                "codex",
                "--conversation-id",
                "thread-123",
                "--thread-id",
                "thread-123",
            ]
        )
        with mock.patch.object(
            folio,
            "capture_agent_response",
            return_value=(
                "# Captured",
                folio.source_descriptor(
                    agent="codex",
                    support_level="native",
                    transport="session",
                    adapter="codex-session-v1",
                    conversation_id="thread-123",
                ),
            ),
        ) as capture:
            with mock.patch.object(
                folio,
                "entry_command_result",
                return_value={"status": "saved"},
            ):
                result = folio.execute(args, self.store)

        self.assertEqual(result, {"status": "saved"})
        capture.assert_called_once_with("codex", "thread-123")


class ServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = folio.FolioStore(Path(self.temp.name) / "home")
        self.entry = self.store.add_entry(
            "# Reader heading\n\nHello <script>bad()</script>.",
            category_name="Docs",
            source=folio.source_descriptor(
                agent="codex",
                support_level="native",
                transport="session",
                adapter="codex-session-v1",
                conversation_id="thread-123",
            ),
        )
        self.instance_id = "test-instance"
        self.auth_token = "test-capability-token"
        self.route_prefix = "/s/test-instance-token"
        self.server = folio.FolioHTTPServer(
            (folio.LOOPBACK_HOST, 0),
            folio.make_handler(
                self.store,
                self.instance_id,
                self.auth_token,
                self.route_prefix,
            ),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = f"http://{folio.LOOPBACK_HOST}:{self.server.server_address[1]}"

    def stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def fetch(self, path: str) -> tuple[int, str, dict]:
        request = urllib.request.Request(
            self.base + self.route_prefix + path,
            headers={"Authorization": f"Bearer {self.auth_token}"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return (
                response.status,
                response.read().decode("utf-8"),
                dict(response.headers),
            )

    def post_category(
        self,
        entry_id: str,
        category: str,
        *,
        origin: str | None = None,
        request_header: bool = True,
    ) -> tuple[int, dict]:
        payload = json.dumps({"category": category}).encode("utf-8")
        request = urllib.request.Request(
            self.base + self.route_prefix + f"/api/entry/{entry_id}/category",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.auth_token}",
                **({"Origin": origin or self.base} if origin is not None else {}),
                **({"X-Folio-Request": "1"} if request_header else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read().decode("utf-8"))

    def post_delete_category(
        self,
        category_id: str,
        *,
        origin: str | None = None,
        request_header: bool = True,
    ) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + self.route_prefix + f"/api/category/{category_id}/delete",
            data=b"{}",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.auth_token}",
                **({"Origin": origin or self.base} if origin is not None else {}),
                **({"X-Folio-Request": "1"} if request_header else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read().decode("utf-8"))

    def post_delete_entry(
        self,
        entry_id: str,
        *,
        origin: str | None = None,
        request_header: bool = True,
    ) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + self.route_prefix + f"/api/entry/{entry_id}/delete",
            data=b"{}",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.auth_token}",
                **({"Origin": origin or self.base} if origin is not None else {}),
                **({"X-Folio-Request": "1"} if request_header else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read().decode("utf-8"))

    def post_entry(
        self,
        payload: dict,
        *,
        origin: str | None = None,
        request_header: bool = True,
    ) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + self.route_prefix + "/api/entry",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.auth_token}",
                **({"Origin": origin or self.base} if origin is not None else {}),
                **({"X-Folio-Request": "1"} if request_header else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read().decode("utf-8"))

    def test_health_library_reader_and_static_assets(self) -> None:
        status, body, _headers = self.fetch("/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["instance_id"], self.instance_id)

        status, body, headers = self.fetch("/library")
        self.assertEqual(status, 200)
        self.assertIn("Reader heading", body)
        self.assertIn("data-delete-entry", body)
        self.assertIn("Add an AI response", body)
        self.assertIn("Codex · Native", body)
        self.assertIn(f'data-folio-base="{self.route_prefix}"', body)
        self.assertIn(
            f'href="{self.route_prefix}/static/folio.css"',
            body,
        )
        self.assertIn("Content-Security-Policy", headers)
        self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertIn("geolocation=()", headers["Permissions-Policy"])

        reader_path = self.entry["metadata"]["reader_path"]
        status, body, _headers = self.fetch(reader_path)
        self.assertEqual(status, 200)
        self.assertIn("Reader heading", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>bad()", body)
        self.assertIn("Move to category", body)
        self.assertIn("data-category-form", body)
        self.assertIn("Codex · Native", body)

        category_id = self.entry["metadata"]["category"]["id"]
        status, body, _headers = self.fetch(f"/library?category={category_id}")
        self.assertEqual(status, 200)
        self.assertIn("Delete this category", body)
        self.assertIn("data-delete-category", body)

        status, css, _headers = self.fetch("/static/folio.css")
        self.assertEqual(status, 200)
        self.assertIn("--accent", css)

    def test_all_content_routes_require_capability_token(self) -> None:
        for path in ("/health", "/library", self.entry["metadata"]["reader_path"], "/static/folio.css"):
            with self.subTest(path=path):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(
                        self.base + self.route_prefix + path,
                        timeout=2,
                    )
                with raised.exception:
                    self.assertEqual(raised.exception.code, 401)

    def test_auth_bootstrap_sets_cookie_and_redirects_to_clean_path(self) -> None:
        connection = http.client.HTTPConnection(
            folio.LOOPBACK_HOST,
            self.server.server_address[1],
            timeout=2,
        )
        query = urllib.parse.urlencode(
            {
                "token": self.auth_token,
                "next": self.entry["metadata"]["reader_path"],
            }
        )
        connection.request("GET", f"/auth?{query}")
        response = connection.getresponse()
        response.read()
        connection.close()

        self.assertEqual(response.status, 303)
        self.assertEqual(
            response.getheader("Location"),
            self.route_prefix + self.entry["metadata"]["reader_path"],
        )
        cookie = response.getheader("Set-Cookie")
        session_cookie_name = folio.auth_cookie_name(self.instance_id)
        self.assertIn(f"{session_cookie_name}={self.auth_token}", cookie)
        self.assertIn(
            f"Path={self.route_prefix}/",
            cookie,
        )
        self.assertNotIn("Path=/;", cookie)

        request = urllib.request.Request(
            self.base + self.route_prefix + "/library",
            headers={
                "Cookie": (
                    f"{session_cookie_name}={self.auth_token}; "
                    f"{folio.AUTH_COOKIE_NAME}=old-root-cookie"
                )
            },
        )
        with urllib.request.urlopen(request, timeout=2) as authenticated:
            self.assertEqual(authenticated.status, 200)

    def test_provider_filter_composes_with_category_and_search(self) -> None:
        claude = self.store.add_entry(
            "# Claude answer\n\nCross-agent needle.",
            category_name="Docs",
            source=folio.source_descriptor(
                agent="claude",
                support_level="imported",
                transport="clipboard",
                adapter="folio-clipboard-v1",
            ),
        )

        status, body, _headers = self.fetch(
            "/library?category="
            + self.entry["metadata"]["category"]["id"]
            + "&agent=claude&q=needle"
        )

        self.assertEqual(status, 200)
        self.assertIn("Claude answer", body)
        self.assertIn("Claude · Imported", body)
        self.assertNotIn("Reader heading", body)
        self.assertIn(claude["metadata"]["id"], body)

    def test_web_import_saves_and_escapes_manual_response(self) -> None:
        status, result = self.post_entry(
            {
                "markdown": "# Claude web\n\n<script>unsafe()</script>",
                "category": "Research",
                "title": "",
                "agent": "Claude",
            },
            origin=self.base,
        )

        self.assertEqual(status, 201)
        self.assertEqual(result["agent"], "Claude")
        metadata, markdown = self.store.get_entry(result["entry_id"])
        self.assertEqual(metadata["source"]["support_level"], "imported")
        self.assertEqual(metadata["source"]["transport"], "web")
        self.assertEqual(metadata["category"]["name"], "Research")
        self.assertIn("<script>", markdown)
        status, body, _headers = self.fetch(result["reader_path"])
        self.assertEqual(status, 200)
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>unsafe()", body)

    def test_web_import_requires_same_origin_custom_header(self) -> None:
        payload = {
            "markdown": "# Do not save",
            "category": "Inbox",
            "agent": "Claude",
        }
        status, _result = self.post_entry(
            payload,
            origin="http://attacker.example",
        )
        self.assertEqual(status, 403)
        status, _result = self.post_entry(
            payload,
            origin=self.base,
            request_header=False,
        )
        self.assertEqual(status, 403)
        self.assertEqual(len(self.store.load_entries()), 1)

    def test_source_labels_are_escaped_in_library(self) -> None:
        self.store.add_entry(
            "# Custom source",
            source=folio.source_descriptor(
                agent="custom",
                source_label='<img src=x onerror="bad()">',
                support_level="imported",
                transport="stdin",
                adapter="folio-stdin-v1",
            ),
        )

        status, body, _headers = self.fetch("/library")

        self.assertEqual(status, 200)
        self.assertIn("&lt;img src=x onerror=&quot;bad()&quot;&gt;", body)
        self.assertNotIn('<img src=x onerror="bad()">', body)

    def test_category_can_be_moved_from_reader_api(self) -> None:
        entry_id = self.entry["metadata"]["id"]
        status, result = self.post_category(
            entry_id,
            "Project Alpha",
            origin=self.base,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "moved")
        self.assertEqual(result["category"], "Project Alpha")
        self.assertEqual(result["reader_path"], f"/entry/{entry_id}")

        metadata, markdown = self.store.get_entry(entry_id)
        self.assertEqual(metadata["category"]["name"], "Project Alpha")
        self.assertIn("Reader heading", markdown)

        status, body, _headers = self.fetch(f"/entry/{entry_id}")
        self.assertEqual(status, 200)
        self.assertIn('value="Project Alpha"', body)

    def test_category_move_requires_same_origin_custom_header(self) -> None:
        entry_id = self.entry["metadata"]["id"]
        status, _result = self.post_category(
            entry_id,
            "Project Alpha",
            origin="http://attacker.example",
        )
        self.assertEqual(status, 403)

        status, _result = self.post_category(
            entry_id,
            "Project Alpha",
            origin=self.base,
            request_header=False,
        )
        self.assertEqual(status, 403)
        metadata, _markdown = self.store.get_entry(entry_id)
        self.assertEqual(metadata["category"]["name"], "Docs")

    def test_category_can_be_deleted_from_library_api(self) -> None:
        entry_id = self.entry["metadata"]["id"]
        category_id = self.entry["metadata"]["category"]["id"]

        status, result = self.post_delete_category(category_id, origin=self.base)

        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["category"], "Docs")
        self.assertEqual(result["moved_count"], 1)
        self.assertEqual(result["destination"], "Inbox")
        self.assertEqual(result["library_path"], "/library?category=inbox")
        self.assertNotIn(category_id, {item["id"] for item in self.store.categories()})
        metadata, markdown = self.store.get_entry(entry_id)
        self.assertEqual(metadata["category"]["name"], "Inbox")
        self.assertIn("Reader heading", markdown)

    def test_category_delete_requires_same_origin_custom_header(self) -> None:
        category_id = self.entry["metadata"]["category"]["id"]
        status, _result = self.post_delete_category(
            category_id,
            origin="http://attacker.example",
        )
        self.assertEqual(status, 403)

        status, _result = self.post_delete_category(
            category_id,
            origin=self.base,
            request_header=False,
        )
        self.assertEqual(status, 403)
        self.assertIn(category_id, {item["id"] for item in self.store.categories()})

    def test_response_can_be_deleted_from_library_api(self) -> None:
        entry_id = self.entry["metadata"]["id"]

        status, result = self.post_delete_entry(entry_id, origin=self.base)

        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["entry_id"], entry_id)
        self.assertEqual(result["title"], "Reader heading")
        with self.assertRaisesRegex(folio.FolioError, "Entry not found"):
            self.store.get_entry(entry_id)

    def test_response_delete_requires_same_origin_custom_header(self) -> None:
        entry_id = self.entry["metadata"]["id"]
        status, _result = self.post_delete_entry(
            entry_id,
            origin="http://attacker.example",
        )
        self.assertEqual(status, 403)

        status, _result = self.post_delete_entry(
            entry_id,
            origin=self.base,
            request_header=False,
        )
        self.assertEqual(status, 403)
        metadata, _markdown = self.store.get_entry(entry_id)
        self.assertEqual(metadata["title"], "Reader heading")

    def test_category_move_rejects_nested_category(self) -> None:
        entry_id = self.entry["metadata"]["id"]
        status, result = self.post_category(
            entry_id,
            "Parent/Child",
            origin=self.base,
        )
        self.assertEqual(status, 400)
        self.assertIn("cannot contain", result["error"])

    def test_unknown_entry_returns_404(self) -> None:
        request = urllib.request.Request(
            self.base + self.route_prefix + "/entry/not-valid",
            headers={"Authorization": f"Bearer {self.auth_token}"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        with raised.exception:
            self.assertEqual(raised.exception.code, 404)

    def test_hostile_host_header_is_rejected(self) -> None:
        connection = http.client.HTTPConnection(
            folio.LOOPBACK_HOST,
            self.server.server_address[1],
            timeout=2,
        )
        connection.putrequest(
            "GET",
            self.route_prefix + "/health",
            skip_host=True,
        )
        connection.putheader("Host", "attacker.example")
        connection.endheaders()
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 421)
        self.assertIn("Invalid Host", body)

    def test_search_terms_are_not_logged(self) -> None:
        captured = io.StringIO()
        with mock.patch.object(folio.sys, "stderr", captured):
            status, _body, _headers = self.fetch("/library?q=private-search-term")
        self.assertEqual(status, 200)
        self.assertEqual(captured.getvalue(), "")


class StateTest(unittest.TestCase):
    def test_stale_state_is_recovered_without_signalling_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = folio.FolioStore(Path(temporary) / "home")
            store.ensure_layout()
            folio.atomic_write(
                store.server_state_path,
                folio.json_bytes(
                    {
                        "pid": 999999,
                        "port": 9,
                        "host": folio.LOOPBACK_HOST,
                        "instance_id": "stale",
                    }
                ),
            )
            result = folio.stop_server(store)
            self.assertFalse(result["was_running"])
            self.assertTrue(result["recovered_stale_state"])
            self.assertFalse(store.server_state_path.exists())

    def test_ensure_server_restarts_verified_older_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = folio.FolioStore(Path(temporary) / "home")
            store.ensure_layout()
            old_state = {
                "version": 1,
                "pid": 12345,
                "port": 8765,
                "host": folio.LOOPBACK_HOST,
                "instance_id": "old-instance",
            }
            new_state = {
                "version": folio.SERVER_STATE_VERSION,
                "app_version": folio.VERSION,
                "pid": 12346,
                "port": 8766,
                "host": folio.LOOPBACK_HOST,
                "instance_id": "new-instance",
                "auth_token": "new-token",
                "route_prefix": "/s/new-instance-token",
            }
            folio.atomic_write(store.server_state_path, folio.json_bytes(old_state))
            old_health = {
                "status": "ok",
                "version": "0.2.0",
                "instance_id": "old-instance",
            }
            new_health = {
                "status": "ok",
                "version": folio.VERSION,
                "instance_id": "new-instance",
            }
            with (
                mock.patch.object(
                    folio,
                    "read_server_state",
                    side_effect=[old_state, new_state],
                ),
                mock.patch.object(
                    folio,
                    "health_for_state",
                    side_effect=[old_health, new_health],
                ),
                mock.patch.object(
                    folio,
                    "stop_verified_server",
                    return_value=True,
                ) as stop_old,
                mock.patch.object(folio.subprocess, "Popen") as popen,
            ):
                state = folio.ensure_server(store)

            self.assertEqual(state, new_state)
            stop_old.assert_called_once_with(store, old_state)
            popen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
