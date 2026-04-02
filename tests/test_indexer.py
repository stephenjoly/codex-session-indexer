from __future__ import annotations

import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from watchdog.events import FileModifiedEvent

from codex_sessions.cli import main
from codex_sessions.indexer import (
    MANAGED_MARKER,
    format_duration,
    is_managed_generated_file,
    parse_session_file,
    render_global_markdown,
    render_project_markdown,
)
from codex_sessions.sync import SyncConfig, load_state, run_sync
from codex_sessions.watch import DebouncedTrigger, RelevantChangeHandler, watch_forever


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def overwrite_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class FakeObserver:
    def __init__(self) -> None:
        self.scheduled: list[tuple[object, str, bool]] = []
        self.started = False
        self.stopped = False
        self.joined = False

    def schedule(self, handler, path: str, recursive: bool) -> None:
        self.scheduled.append((handler, path, recursive))

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def join(self) -> None:
        self.joined = True


class IndexerTests(unittest.TestCase):
    def test_parse_session_uses_index_thread_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cwd = root / "project-a"
            cwd.mkdir()
            session_file = root / "rollout-1.jsonl"
            write_jsonl(
                session_file,
                [
                    {
                        "timestamp": "2026-04-01T10:00:00.000Z",
                        "type": "session_meta",
                        "payload": {"id": "sess-1", "cwd": str(cwd)},
                    },
                    {
                        "timestamp": "2026-04-01T10:01:00.000Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "first prompt"},
                    },
                    {
                        "timestamp": "2026-04-01T10:05:00.000Z",
                        "type": "response_item",
                        "payload": {"type": "message", "role": "assistant"},
                    },
                ],
            )

            record = parse_session_file(session_file, {"sess-1": "Named Thread"})
            self.assertEqual(record.thread_name, "Named Thread")
            self.assertEqual(record.prompt_count, 1)
            self.assertEqual(record.event_count, 3)
            self.assertEqual(format_duration(record.lifetime_seconds), "5m")

    def test_parse_session_falls_back_to_prompt_then_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cwd = root / "project-a"
            cwd.mkdir()

            prompt_file = root / "rollout-2.jsonl"
            write_jsonl(
                prompt_file,
                [
                    {
                        "timestamp": "2026-04-01T10:00:00.000Z",
                        "type": "session_meta",
                        "payload": {"id": "sess-2", "cwd": str(cwd)},
                    },
                    {
                        "timestamp": "2026-04-01T10:01:00.000Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "This is a very long first prompt that should be trimmed once it is used as fallback title text.",
                        },
                    },
                ],
            )
            prompt_record = parse_session_file(prompt_file, {})
            self.assertEqual(prompt_record.fallback_source, "first_user_prompt")
            self.assertTrue(prompt_record.thread_name.startswith("This is a very long first prompt"))
            self.assertTrue(prompt_record.thread_name.endswith("..."))

            name_file = root / "rollout-3.jsonl"
            write_jsonl(
                name_file,
                [
                    {
                        "timestamp": "2026-04-01T10:00:00.000Z",
                        "type": "session_meta",
                        "payload": {"id": "sess-3", "cwd": str(cwd)},
                    }
                ],
            )
            name_record = parse_session_file(name_file, {})
            self.assertEqual(name_record.fallback_source, "filename")
            self.assertEqual(name_record.thread_name, "rollout-3")

    def test_renderers_include_managed_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cwd = root / "project-a"
            cwd.mkdir()
            session_file = root / "rollout.jsonl"
            write_jsonl(
                session_file,
                [
                    {
                        "timestamp": "2026-04-01T10:00:00.000Z",
                        "type": "session_meta",
                        "payload": {"id": "sess-1", "cwd": str(cwd)},
                    },
                    {
                        "timestamp": "2026-04-01T10:01:00.000Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "first prompt"},
                    },
                ],
            )
            record = parse_session_file(session_file, {"sess-1": "Named Thread"})
            project_md = render_project_markdown(cwd=cwd, sessions=[record], generated_at=record.last_updated_at)
            global_md = render_global_markdown(global_root=root, sessions=[record], generated_at=record.last_updated_at)

            self.assertTrue(project_md.startswith(MANAGED_MARKER))
            self.assertIn("Named Thread", project_md)
            self.assertIn("## Highlights", project_md)
            self.assertIn("### Most Recent Sessions", project_md)
            self.assertIn("### Most Active Sessions", project_md)
            self.assertTrue(global_md.startswith(MANAGED_MARKER))
            self.assertIn(str(cwd), global_md)
            self.assertIn("## Highlights", global_md)

    def test_highlights_rank_recent_and_active_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cwd = root / "project-a"
            cwd.mkdir()

            records = []
            for name, updated_at, prompts, lifetime in [
                ("Recent Low", "2026-04-01T12:00:00.000Z", 1, "2026-04-01T11:59:00.000Z"),
                ("Mid High", "2026-04-01T11:00:00.000Z", 8, "2026-04-01T10:00:00.000Z"),
                ("Older Highest", "2026-04-01T10:00:00.000Z", 20, "2026-04-01T08:00:00.000Z"),
                ("Old Low", "2026-04-01T09:00:00.000Z", 0, "2026-04-01T08:59:00.000Z"),
            ]:
                session_file = root / f"{name.lower().replace(' ', '-')}.jsonl"
                write_jsonl(
                    session_file,
                    [
                        {
                            "timestamp": lifetime,
                            "type": "session_meta",
                            "payload": {"id": name, "cwd": str(cwd)},
                        },
                        {
                            "timestamp": updated_at,
                            "type": "event_msg",
                            "payload": {"type": "user_message", "message": "prompt"},
                        },
                    ]
                    + (
                        [
                            {
                                "timestamp": updated_at,
                                "type": "event_msg",
                                "payload": {"type": "user_message", "message": "prompt"},
                            }
                        ]
                        * max(prompts - 1, 0)
                    ),
                )
                records.append(parse_session_file(session_file, {name: name}))

            records.sort(key=lambda record: (record.last_updated_at, str(record.session_file_path)), reverse=True)
            project_md = render_project_markdown(cwd=cwd, sessions=records, generated_at=records[0].last_updated_at)

            recent_section = project_md.split("### Most Recent Sessions\n", 1)[1].split("\n\n### Most Active Sessions\n", 1)[0]
            active_section = project_md.split("### Most Active Sessions\n", 1)[1].split("\n\n| Session |", 1)[0]

            self.assertIn("**Recent Low**", recent_section)
            self.assertIn("**Mid High**", recent_section)
            self.assertIn("**Older Highest**", active_section)
            self.assertIn("20 prompts", active_section)
            self.assertLess(recent_section.index("**Recent Low**"), recent_section.index("**Mid High**"))
            self.assertLess(active_section.index("**Older Highest**"), active_section.index("**Mid High**"))

    def test_incremental_generate_no_changes_does_not_rewrite_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config, project_a, _, _, _, _, _ = self._build_workspace(root)

            first = run_sync(config)
            self.assertTrue(first.full_rebuild)

            project_file = project_a / "codex-sessions.md"
            global_file = config.global_root / "codex-sessions-index.md"
            project_mtime = project_file.stat().st_mtime_ns
            global_mtime = global_file.stat().st_mtime_ns

            time.sleep(0.05)
            second = run_sync(config)
            self.assertFalse(second.full_rebuild)
            self.assertEqual(second.parsed_session_files, 0)
            self.assertEqual(project_mtime, project_file.stat().st_mtime_ns)
            self.assertEqual(global_mtime, global_file.stat().st_mtime_ns)

    def test_incremental_generate_rewrites_only_affected_project_and_global(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config, project_a, project_b, _, sessions_dir, index_path, _ = self._build_workspace(root)

            run_sync(config)
            project_a_file = project_a / "codex-sessions.md"
            project_b_file = project_b / "codex-sessions.md"
            global_file = config.global_root / "codex-sessions-index.md"
            before_a = project_a_file.stat().st_mtime_ns
            before_b = project_b_file.stat().st_mtime_ns
            before_global = global_file.stat().st_mtime_ns

            time.sleep(0.05)
            write_jsonl(
                sessions_dir / "a.jsonl",
                [
                    {
                        "timestamp": "2026-04-01T09:00:00.000Z",
                        "type": "session_meta",
                        "payload": {"id": "a", "cwd": str(project_a)},
                    },
                    {
                        "timestamp": "2026-04-01T09:10:00.000Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "alpha prompt"},
                    },
                    {
                        "timestamp": "2026-04-01T09:20:00.000Z",
                        "type": "response_item",
                        "payload": {"type": "message", "role": "assistant"},
                    },
                ],
            )

            stats = run_sync(config)
            self.assertFalse(stats.full_rebuild)
            self.assertEqual(stats.parsed_session_files, 1)
            self.assertGreater(project_a_file.stat().st_mtime_ns, before_a)
            self.assertEqual(project_b_file.stat().st_mtime_ns, before_b)
            self.assertGreater(global_file.stat().st_mtime_ns, before_global)
            self.assertIn("09:20:00Z", project_a_file.read_text(encoding="utf-8"))

    def test_outside_global_root_change_does_not_rewrite_global_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config, project_a, _, outside_project, sessions_dir, _, _ = self._build_workspace(root)

            run_sync(config)
            inside_file = project_a / "codex-sessions.md"
            outside_file = outside_project / "codex-sessions.md"
            global_file = config.global_root / "codex-sessions-index.md"
            before_inside = inside_file.stat().st_mtime_ns
            before_outside = outside_file.stat().st_mtime_ns
            before_global = global_file.stat().st_mtime_ns

            time.sleep(0.05)
            write_jsonl(
                sessions_dir / "outside.jsonl",
                [
                    {
                        "timestamp": "2026-04-01T11:00:00.000Z",
                        "type": "session_meta",
                        "payload": {"id": "outside", "cwd": str(outside_project)},
                    },
                    {
                        "timestamp": "2026-04-01T11:20:00.000Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "outside prompt"},
                    },
                ],
            )

            stats = run_sync(config)
            self.assertEqual(stats.parsed_session_files, 1)
            self.assertEqual(inside_file.stat().st_mtime_ns, before_inside)
            self.assertGreater(outside_file.stat().st_mtime_ns, before_outside)
            self.assertEqual(global_file.stat().st_mtime_ns, before_global)

    def test_session_index_title_change_rewrites_only_affected_project_and_global(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config, project_a, project_b, _, _, index_path, _ = self._build_workspace(root)

            run_sync(config)
            project_a_file = project_a / "codex-sessions.md"
            project_b_file = project_b / "codex-sessions.md"
            global_file = config.global_root / "codex-sessions-index.md"
            before_a = project_a_file.stat().st_mtime_ns
            before_b = project_b_file.stat().st_mtime_ns
            before_global = global_file.stat().st_mtime_ns

            time.sleep(0.05)
            write_jsonl(
                index_path,
                [
                    {"id": "a", "thread_name": "Alpha Updated"},
                    {"id": "b", "thread_name": "Beta"},
                    {"id": "outside", "thread_name": "Outside"},
                ],
            )

            stats = run_sync(config)
            self.assertEqual(stats.parsed_session_files, 0)
            self.assertGreater(project_a_file.stat().st_mtime_ns, before_a)
            self.assertEqual(project_b_file.stat().st_mtime_ns, before_b)
            self.assertGreater(global_file.stat().st_mtime_ns, before_global)
            self.assertIn("Alpha Updated", project_a_file.read_text(encoding="utf-8"))

    def test_deleted_session_cleans_up_empty_project_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config, project_a, _, _, sessions_dir, _, _ = self._build_workspace(root)

            run_sync(config)
            project_file = project_a / "codex-sessions.md"
            self.assertTrue(project_file.exists())
            self.assertTrue(is_managed_generated_file(project_file))

            time.sleep(0.05)
            (sessions_dir / "a.jsonl").unlink()
            stats = run_sync(config)

            self.assertEqual(stats.project_files_deleted, 1)
            self.assertFalse(project_file.exists())

    def test_project_output_is_added_to_gitignore_for_repo_root_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config, project_a, _, _, _, _, _ = self._build_workspace(root)
            self._init_git_repo(project_a)

            run_sync(config)

            gitignore = project_a / ".gitignore"
            self.assertTrue(gitignore.exists())
            self.assertIn("/codex-sessions.md", gitignore.read_text(encoding="utf-8"))

            before = gitignore.read_text(encoding="utf-8")
            run_sync(config)
            self.assertEqual(before, gitignore.read_text(encoding="utf-8"))

    def test_project_output_is_added_to_gitignore_for_nested_project_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config, project_a, _, _, _, _, _ = self._build_workspace(root)
            self._init_git_repo(config.global_root)

            run_sync(config)

            gitignore = config.global_root / ".gitignore"
            self.assertTrue(gitignore.exists())
            self.assertIn("/project-a/codex-sessions.md", gitignore.read_text(encoding="utf-8"))

    def test_config_change_deletes_old_managed_output_and_rebuilds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config, project_a, _, _, _, _, state_file = self._build_workspace(root)

            run_sync(config)
            old_output = project_a / "codex-sessions.md"
            self.assertTrue(old_output.exists())

            changed_config = SyncConfig(
                sessions_dir=config.sessions_dir,
                session_index=config.session_index,
                global_root=config.global_root,
                output_filename="history.md",
                global_output_name=config.global_output_name,
                state_file=state_file,
                dry_run=False,
                verbose=False,
                full_rebuild=False,
            )
            stats = run_sync(changed_config)
            self.assertTrue(stats.full_rebuild)
            self.assertFalse(old_output.exists())
            self.assertTrue((project_a / "history.md").exists())

    def test_corrupt_state_file_falls_back_to_full_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config, _, _, _, _, _, state_file = self._build_workspace(root)
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text("{not valid json", encoding="utf-8")

            stats = run_sync(config)
            self.assertTrue(stats.full_rebuild)
            self.assertEqual(stats.reset_reason, "corrupt")

    def test_schema_mismatch_falls_back_to_full_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config, _, _, _, _, _, state_file = self._build_workspace(root)
            overwrite_json(
                state_file,
                {
                    "schema_version": 1,
                    "config_fingerprint": "x",
                    "sessions": {},
                    "managed_project_files": [],
                    "global_output_path": None,
                },
            )

            stats = run_sync(config)
            self.assertTrue(stats.full_rebuild)
            self.assertEqual(stats.reset_reason, "schema_mismatch")

    def test_cli_generate_supports_hidden_state_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config, _, _, _, _, _, state_file = self._build_workspace(root)
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "generate",
                        "--sessions-dir",
                        str(config.sessions_dir),
                        "--session-index",
                        str(config.session_index),
                        "--global-root",
                        str(config.global_root),
                        "--state-file",
                        str(state_file),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("tracked sessions", output.getvalue())
            loaded_state, _ = load_state(state_file)
            self.assertIsNotNone(loaded_state)

    def test_cli_watch_prints_startup_and_initial_sync_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config, _, _, _, _, _, state_file = self._build_workspace(root)
            output = io.StringIO()
            with patch(
                "codex_sessions.cli.watch_forever",
                side_effect=lambda watch_config, debounce_seconds, sync_runner: sync_runner(watch_config),
            ), redirect_stdout(output):
                exit_code = main(
                    [
                        "watch",
                        "--sessions-dir",
                        str(config.sessions_dir),
                        "--session-index",
                        str(config.session_index),
                        "--global-root",
                        str(config.global_root),
                        "--state-file",
                        str(state_file),
                        "--verbose",
                    ]
                )

            self.assertEqual(exit_code, 0)
            value = output.getvalue()
            self.assertIn("Watching for Codex session changes. Press Ctrl-C to stop.", value)
            self.assertIn("Sync complete.", value)

    def test_debounced_trigger_batches_marks(self) -> None:
        trigger = DebouncedTrigger()
        trigger.mark_dirty(now=10.0)
        self.assertFalse(trigger.ready(1.0, now=10.5))
        trigger.mark_dirty(now=10.8)
        self.assertFalse(trigger.ready(1.0, now=11.5))
        self.assertTrue(trigger.ready(1.0, now=11.9))
        trigger.clear()
        self.assertFalse(trigger.ready(1.0, now=12.5))

    def test_relevant_change_handler_marks_session_and_index_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sessions_dir = root / ".codex" / "sessions"
            session_index = root / ".codex" / "session_index.jsonl"
            trigger = DebouncedTrigger()
            handler = RelevantChangeHandler(sessions_dir, session_index, trigger)

            handler.on_any_event(FileModifiedEvent(str(sessions_dir / "2026" / "04" / "01" / "a.jsonl")))
            self.assertTrue(trigger.dirty)

            trigger.clear()
            handler.on_any_event(FileModifiedEvent(str(session_index)))
            self.assertTrue(trigger.dirty)

            trigger.clear()
            handler.on_any_event(FileModifiedEvent(str(root / "notes.txt")))
            self.assertFalse(trigger.dirty)

    def test_watch_forever_runs_initial_sync_before_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config, _, _, _, _, _, _ = self._build_workspace(root)
            calls: list[SyncConfig] = []
            stop_event = threading.Event()
            stop_event.set()

            watch_forever(
                config,
                debounce_seconds=1.0,
                stop_event=stop_event,
                observer_factory=FakeObserver,
                sync_runner=lambda value: calls.append(value),
            )

            self.assertEqual(len(calls), 1)

    def _build_workspace(
        self,
        root: Path,
    ) -> tuple[SyncConfig, Path, Path, Path, Path, Path, Path]:
        sessions_dir = root / ".codex" / "sessions" / "2026" / "04" / "01"
        sessions_dir.mkdir(parents=True)
        coding_root = root / "coding"
        project_a = coding_root / "project-a"
        project_b = coding_root / "project-b"
        outside_project = root / "outside"
        project_a.mkdir(parents=True)
        project_b.mkdir(parents=True)
        outside_project.mkdir()

        write_jsonl(
            sessions_dir / "a.jsonl",
            [
                {
                    "timestamp": "2026-04-01T09:00:00.000Z",
                    "type": "session_meta",
                    "payload": {"id": "a", "cwd": str(project_a)},
                },
                {
                    "timestamp": "2026-04-01T09:10:00.000Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "alpha prompt"},
                },
            ],
        )
        write_jsonl(
            sessions_dir / "b.jsonl",
            [
                {
                    "timestamp": "2026-04-01T10:00:00.000Z",
                    "type": "session_meta",
                    "payload": {"id": "b", "cwd": str(project_b)},
                },
                {
                    "timestamp": "2026-04-01T10:15:00.000Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "beta prompt"},
                },
            ],
        )
        write_jsonl(
            sessions_dir / "outside.jsonl",
            [
                {
                    "timestamp": "2026-04-01T11:00:00.000Z",
                    "type": "session_meta",
                    "payload": {"id": "outside", "cwd": str(outside_project)},
                },
                {
                    "timestamp": "2026-04-01T11:10:00.000Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "outside prompt"},
                },
            ],
        )

        index_path = root / ".codex" / "session_index.jsonl"
        write_jsonl(
            index_path,
            [
                {"id": "a", "thread_name": "Alpha"},
                {"id": "b", "thread_name": "Beta"},
                {"id": "outside", "thread_name": "Outside"},
            ],
        )
        state_file = root / ".codex" / "codex-session-indexer-state.json"
        config = SyncConfig(
            sessions_dir=root / ".codex" / "sessions",
            session_index=index_path,
            global_root=coding_root,
            output_filename="codex-sessions.md",
            global_output_name="codex-sessions-index.md",
            state_file=state_file,
            dry_run=False,
            verbose=False,
            full_rebuild=False,
        )
        return config, project_a, project_b, outside_project, sessions_dir, index_path, state_file

    def _init_git_repo(self, path: Path) -> None:
        subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
