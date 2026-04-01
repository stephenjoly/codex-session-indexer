from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .sync import SyncConfig, run_sync
from .watch import watch_forever


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-sessions")
    subparsers = parser.add_subparsers(dest="command", required=True)
    home = Path.home()
    default_sessions_dir = home / ".codex" / "sessions"
    default_session_index = home / ".codex" / "session_index.jsonl"
    default_global_root = home / "Documents" / "Coding"
    default_state_file = home / ".codex" / "codex-session-indexer-state.json"

    def add_common_arguments(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--sessions-dir",
            default=str(default_sessions_dir),
            help="Directory containing Codex session JSONL files.",
        )
        command_parser.add_argument(
            "--session-index",
            default=str(default_session_index),
            help="Path to Codex session_index.jsonl.",
        )
        command_parser.add_argument(
            "--global-root",
            default=str(default_global_root),
            help="Only sessions under this directory are included in the global index.",
        )
        command_parser.add_argument(
            "--output-filename",
            default="codex-sessions.md",
            help="Filename written into each discovered cwd.",
        )
        command_parser.add_argument(
            "--global-output-name",
            default="codex-sessions-index.md",
            help="Filename for the global recent-sessions index under --global-root.",
        )
        command_parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Compute outputs without writing files or state.",
        )
        command_parser.add_argument(
            "--verbose",
            action="store_true",
            help="Print incremental write, delete, and skip actions.",
        )
        command_parser.add_argument(
            "--state-file",
            default=str(default_state_file),
            help=argparse.SUPPRESS,
        )

    generate = subparsers.add_parser("generate", help="Generate Markdown session indexes.")
    add_common_arguments(generate)
    generate.add_argument(
        "--full-rebuild",
        action="store_true",
        help="Ignore incremental state and rebuild every managed output.",
    )

    watch = subparsers.add_parser("watch", help="Watch for Codex session changes and update indexes.")
    add_common_arguments(watch)
    watch.add_argument(
        "--debounce-seconds",
        type=float,
        default=1.0,
        help="Quiet period before a batch of filesystem events triggers an incremental sync.",
    )

    return parser


def _build_config(args: argparse.Namespace, *, full_rebuild: bool) -> SyncConfig:
    return SyncConfig(
        sessions_dir=Path(args.sessions_dir).expanduser(),
        session_index=Path(args.session_index).expanduser(),
        global_root=Path(args.global_root).expanduser(),
        output_filename=args.output_filename,
        global_output_name=args.global_output_name,
        state_file=Path(args.state_file).expanduser(),
        dry_run=args.dry_run,
        verbose=args.verbose,
        full_rebuild=full_rebuild,
    )


def _print_summary(stats, *, dry_run: bool) -> None:
    prefix = "Dry run complete." if dry_run else "Sync complete."
    suffix_parts = [
        f"tracked sessions: {stats.tracked_sessions}.",
        f"parsed session files: {stats.parsed_session_files}.",
        f"project files written: {stats.project_files_written}.",
        f"project files deleted: {stats.project_files_deleted}.",
        f"skipped missing directories: {stats.skipped_missing_directories}.",
        f"global files written: {stats.global_files_written}.",
    ]
    if stats.full_rebuild:
        suffix_parts.append("mode: full rebuild.")
        if stats.reset_reason:
            suffix_parts.append(f"reason: {stats.reset_reason}.")
    else:
        suffix_parts.append("mode: incremental.")
    print(" ".join([prefix, *suffix_parts]), file=sys.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "generate":
        config = _build_config(args, full_rebuild=args.full_rebuild)
        stats = run_sync(config)
        _print_summary(stats, dry_run=config.dry_run)
        return 0

    if args.command == "watch":
        config = _build_config(args, full_rebuild=False)
        watch_forever(config, debounce_seconds=args.debounce_seconds)
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
