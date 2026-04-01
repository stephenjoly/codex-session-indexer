from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from .indexer import (
    SessionRecord,
    delete_file,
    group_sessions_by_cwd,
    is_managed_generated_file,
    is_within_root,
    iter_session_files,
    load_thread_names,
    parse_session_file,
    parse_timestamp,
    render_global_markdown,
    render_project_markdown,
    resolve_thread_name,
    write_text,
)

STATE_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class FileSignature:
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class CachedSession:
    signature: FileSignature
    session_id: str
    cwd: Path
    started_at: datetime
    last_updated_at: datetime
    lifetime_seconds: float
    prompt_count: int
    event_count: int
    session_file_path: Path
    fallback_source: str
    first_user_prompt_snippet: str | None
    filename_stem: str
    thread_name: str


@dataclass(frozen=True)
class SyncState:
    schema_version: int
    config_fingerprint: str
    session_index_signature: FileSignature | None
    sessions: dict[str, CachedSession]
    managed_project_files: set[str]
    global_output_path: str | None


@dataclass(frozen=True)
class SyncConfig:
    sessions_dir: Path
    session_index: Path
    global_root: Path
    output_filename: str
    global_output_name: str
    state_file: Path
    dry_run: bool = False
    verbose: bool = False
    full_rebuild: bool = False


@dataclass(frozen=True)
class SyncStats:
    tracked_sessions: int
    parsed_session_files: int
    project_files_written: int
    project_files_deleted: int
    skipped_missing_directories: int
    global_files_written: int
    full_rebuild: bool
    reset_reason: str | None


def build_signature(path: Path) -> FileSignature | None:
    if not path.exists():
        return None
    stat_result = path.stat()
    return FileSignature(mtime_ns=stat_result.st_mtime_ns, size=stat_result.st_size)


def build_config_fingerprint(config: SyncConfig) -> str:
    payload = json.dumps(
        {
            "sessions_dir": str(config.sessions_dir.resolve()),
            "session_index": str(config.session_index.resolve()),
            "global_root": str(config.global_root.resolve()),
            "output_filename": config.output_filename,
            "global_output_name": config.global_output_name,
        },
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _serialize_signature(signature: FileSignature | None) -> dict[str, int] | None:
    if signature is None:
        return None
    return {"mtime_ns": signature.mtime_ns, "size": signature.size}


def _deserialize_signature(payload: dict[str, Any] | None) -> FileSignature | None:
    if payload is None:
        return None
    return FileSignature(mtime_ns=int(payload["mtime_ns"]), size=int(payload["size"]))


def _serialize_cached_session(session: CachedSession) -> dict[str, Any]:
    return {
        "signature": _serialize_signature(session.signature),
        "session_id": session.session_id,
        "cwd": str(session.cwd),
        "started_at": session.started_at.astimezone(UTC).isoformat(),
        "last_updated_at": session.last_updated_at.astimezone(UTC).isoformat(),
        "lifetime_seconds": session.lifetime_seconds,
        "prompt_count": session.prompt_count,
        "event_count": session.event_count,
        "session_file_path": str(session.session_file_path),
        "fallback_source": session.fallback_source,
        "first_user_prompt_snippet": session.first_user_prompt_snippet,
        "filename_stem": session.filename_stem,
        "thread_name": session.thread_name,
    }


def _deserialize_cached_session(payload: dict[str, Any]) -> CachedSession:
    return CachedSession(
        signature=_deserialize_signature(payload["signature"]),
        session_id=str(payload["session_id"]),
        cwd=Path(payload["cwd"]),
        started_at=parse_timestamp(payload["started_at"]),
        last_updated_at=parse_timestamp(payload["last_updated_at"]),
        lifetime_seconds=float(payload["lifetime_seconds"]),
        prompt_count=int(payload["prompt_count"]),
        event_count=int(payload["event_count"]),
        session_file_path=Path(payload["session_file_path"]),
        fallback_source=str(payload["fallback_source"]),
        first_user_prompt_snippet=payload.get("first_user_prompt_snippet"),
        filename_stem=str(payload["filename_stem"]),
        thread_name=str(payload["thread_name"]),
    )


def load_state(state_file: Path) -> tuple[SyncState | None, str | None]:
    if not state_file.exists():
        return None, "missing"

    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
        if int(payload["schema_version"]) != STATE_SCHEMA_VERSION:
            return None, "schema_mismatch"
        sessions = {
            path: _deserialize_cached_session(session_payload)
            for path, session_payload in payload.get("sessions", {}).items()
        }
        return (
            SyncState(
                schema_version=STATE_SCHEMA_VERSION,
                config_fingerprint=str(payload["config_fingerprint"]),
                session_index_signature=_deserialize_signature(payload.get("session_index_signature")),
                sessions=sessions,
                managed_project_files=set(payload.get("managed_project_files", [])),
                global_output_path=payload.get("global_output_path"),
            ),
            None,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None, "corrupt"


def save_state(state_file: Path, state: SyncState, *, dry_run: bool) -> None:
    if dry_run:
        return

    state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": state.schema_version,
        "config_fingerprint": state.config_fingerprint,
        "session_index_signature": _serialize_signature(state.session_index_signature),
        "sessions": {
            path: _serialize_cached_session(session)
            for path, session in sorted(state.sessions.items())
        },
        "managed_project_files": sorted(state.managed_project_files),
        "global_output_path": state.global_output_path,
    }

    with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=state_file.parent) as handle:
        temp_path = Path(handle.name)
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")

    temp_path.replace(state_file)


def record_to_cached_session(record: SessionRecord, signature: FileSignature) -> CachedSession:
    return CachedSession(
        signature=signature,
        session_id=record.session_id,
        cwd=record.cwd,
        started_at=record.started_at,
        last_updated_at=record.last_updated_at,
        lifetime_seconds=record.lifetime_seconds,
        prompt_count=record.prompt_count,
        event_count=record.event_count,
        session_file_path=record.session_file_path,
        fallback_source=record.fallback_source,
        first_user_prompt_snippet=record.first_user_prompt_snippet,
        filename_stem=record.filename_stem,
        thread_name=record.thread_name,
    )


def refresh_cached_session_title(session: CachedSession, thread_names: dict[str, str]) -> CachedSession:
    thread_name, fallback_source = resolve_thread_name(
        session_id=session.session_id,
        thread_names=thread_names,
        first_user_prompt_snippet=session.first_user_prompt_snippet,
        filename_stem=session.filename_stem,
    )
    return CachedSession(
        signature=session.signature,
        session_id=session.session_id,
        cwd=session.cwd,
        started_at=session.started_at,
        last_updated_at=session.last_updated_at,
        lifetime_seconds=session.lifetime_seconds,
        prompt_count=session.prompt_count,
        event_count=session.event_count,
        session_file_path=session.session_file_path,
        fallback_source=fallback_source,
        first_user_prompt_snippet=session.first_user_prompt_snippet,
        filename_stem=session.filename_stem,
        thread_name=thread_name,
    )


def cached_to_record(session: CachedSession) -> SessionRecord:
    return SessionRecord(
        session_id=session.session_id,
        thread_name=session.thread_name,
        cwd=session.cwd,
        started_at=session.started_at,
        last_updated_at=session.last_updated_at,
        lifetime_seconds=session.lifetime_seconds,
        prompt_count=session.prompt_count,
        event_count=session.event_count,
        session_file_path=session.session_file_path,
        fallback_source=session.fallback_source,
        first_user_prompt_snippet=session.first_user_prompt_snippet,
        filename_stem=session.filename_stem,
    )


def _output_identity(session: CachedSession) -> tuple[Any, ...]:
    return (
        str(session.cwd),
        session.started_at.astimezone(UTC).isoformat(),
        session.last_updated_at.astimezone(UTC).isoformat(),
        session.lifetime_seconds,
        session.prompt_count,
        session.event_count,
        str(session.session_file_path),
        session.thread_name,
    )


def _is_global_relevant(cwd: Path, global_root: Path) -> bool:
    return is_within_root(cwd, global_root)


def _project_output_path(cwd: Path, output_filename: str) -> Path:
    return cwd / output_filename


def _git_repo_root(path: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    repo_root = result.stdout.strip()
    return Path(repo_root).resolve() if repo_root else None


def _gitignore_entry_for_output(repo_root: Path, output_path: Path) -> str | None:
    try:
        relative_path = output_path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return None
    return f"/{relative_path}"


def ensure_gitignore_contains(output_path: Path, *, dry_run: bool) -> Path | None:
    repo_root = _git_repo_root(output_path.parent)
    if repo_root is None:
        return None

    entry = _gitignore_entry_for_output(repo_root, output_path)
    if entry is None:
        return None

    gitignore_path = repo_root / ".gitignore"
    existing = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
    lines = {line.strip() for line in existing.splitlines()}
    if entry in lines or entry.removeprefix("/") in lines:
        return None

    updated = existing
    if updated and not updated.endswith("\n"):
        updated += "\n"
    updated += f"{entry}\n"

    if dry_run:
        return gitignore_path

    gitignore_path.write_text(updated, encoding="utf-8")
    return gitignore_path


def _session_sort_key(session: CachedSession) -> tuple[datetime, str]:
    return (session.last_updated_at, str(session.session_file_path))


def _build_grouped_records(sessions: dict[str, CachedSession]) -> dict[Path, list[SessionRecord]]:
    records = [cached_to_record(session) for session in sessions.values()]
    grouped = group_sessions_by_cwd(records)
    return grouped


def run_sync(config: SyncConfig) -> SyncStats:
    if not config.sessions_dir.exists():
        raise SystemExit(f"Sessions directory does not exist: {config.sessions_dir}")

    thread_names = load_thread_names(config.session_index)
    current_paths = {
        str(path): build_signature(path)
        for path in iter_session_files(config.sessions_dir)
    }
    current_paths = {path: signature for path, signature in current_paths.items() if signature is not None}

    previous_state, load_reason = load_state(config.state_file)
    fingerprint = build_config_fingerprint(config)

    full_rebuild = config.full_rebuild
    reset_reason: str | None = None
    if previous_state is None:
        full_rebuild = True
        reset_reason = load_reason
    elif previous_state.config_fingerprint != fingerprint:
        full_rebuild = True
        reset_reason = "config_changed"

    parsed_session_files = 0
    affected_cwds: set[Path] = set()
    global_affected = full_rebuild
    previous_sessions = previous_state.sessions if previous_state else {}
    next_sessions: dict[str, CachedSession] = {}

    if full_rebuild:
        for path_str, signature in current_paths.items():
            record = parse_session_file(Path(path_str), thread_names)
            next_sessions[path_str] = record_to_cached_session(record, signature)
            parsed_session_files += 1
        if previous_state:
            affected_cwds.update(session.cwd for session in previous_state.sessions.values())
        affected_cwds.update(session.cwd for session in next_sessions.values())
    else:
        deleted_paths = set(previous_sessions) - set(current_paths)
        for path_str in deleted_paths:
            deleted_session = previous_sessions[path_str]
            affected_cwds.add(deleted_session.cwd)
            if _is_global_relevant(deleted_session.cwd, config.global_root):
                global_affected = True

        for path_str, signature in current_paths.items():
            previous_session = previous_sessions.get(path_str)
            if previous_session is None or previous_session.signature != signature:
                record = parse_session_file(Path(path_str), thread_names)
                current_session = record_to_cached_session(record, signature)
                parsed_session_files += 1
                next_sessions[path_str] = current_session
                if previous_session is None:
                    affected_cwds.add(current_session.cwd)
                    if _is_global_relevant(current_session.cwd, config.global_root):
                        global_affected = True
                elif _output_identity(previous_session) != _output_identity(current_session):
                    affected_cwds.update({previous_session.cwd, current_session.cwd})
                    if _is_global_relevant(previous_session.cwd, config.global_root) or _is_global_relevant(
                        current_session.cwd, config.global_root
                    ):
                        global_affected = True
            else:
                refreshed_session = refresh_cached_session_title(previous_session, thread_names)
                next_sessions[path_str] = refreshed_session
                if previous_session.thread_name != refreshed_session.thread_name:
                    affected_cwds.add(previous_session.cwd)
                    if _is_global_relevant(previous_session.cwd, config.global_root):
                        global_affected = True

    grouped_records = _build_grouped_records(next_sessions)
    managed_project_files: set[str] = set()
    project_files_written = 0
    project_files_deleted = 0
    skipped_missing_directories = 0
    generated_at = datetime.now(UTC)

    for cwd in sorted(grouped_records):
        if cwd.exists() and cwd.is_dir():
            managed_project_files.add(str(_project_output_path(cwd, config.output_filename)))

    old_managed_project_files = previous_state.managed_project_files if previous_state else set()
    stale_project_paths = {Path(path) for path in old_managed_project_files - managed_project_files}

    for path in stale_project_paths:
        if path.exists() and is_managed_generated_file(path):
            deleted = delete_file(path, dry_run=config.dry_run)
            if deleted:
                project_files_deleted += 1
                if config.verbose:
                    prefix = "would delete" if config.dry_run else "deleted"
                    print(f"{prefix} {path}")

    for cwd in sorted(affected_cwds, key=str):
        output_path = _project_output_path(cwd, config.output_filename)
        cwd_sessions = grouped_records.get(cwd, [])
        if cwd_sessions:
            if not cwd.exists() or not cwd.is_dir():
                skipped_missing_directories += 1
                if config.verbose:
                    print(f"skip missing cwd: {cwd}")
                continue

            content = render_project_markdown(cwd=cwd, sessions=cwd_sessions, generated_at=generated_at)
            wrote = write_text(output_path, content, dry_run=config.dry_run)
            if wrote:
                project_files_written += 1
                if config.verbose:
                    prefix = "would write" if config.dry_run else "wrote"
                    print(f"{prefix} {output_path}")
            gitignore_path = ensure_gitignore_contains(output_path, dry_run=config.dry_run)
            if gitignore_path is not None and config.verbose:
                prefix = "would update" if config.dry_run else "updated"
                print(f"{prefix} {gitignore_path}")
        else:
            if output_path.exists() and is_managed_generated_file(output_path):
                deleted = delete_file(output_path, dry_run=config.dry_run)
                if deleted:
                    project_files_deleted += 1
                    if config.verbose:
                        prefix = "would delete" if config.dry_run else "deleted"
                        print(f"{prefix} {output_path}")

    global_output_path = config.global_root / config.global_output_name if config.global_root.exists() else None
    old_global_output = Path(previous_state.global_output_path) if previous_state and previous_state.global_output_path else None
    if old_global_output and (global_output_path is None or old_global_output != global_output_path):
        if old_global_output.exists() and is_managed_generated_file(old_global_output):
            deleted = delete_file(old_global_output, dry_run=config.dry_run)
            if deleted and config.verbose:
                prefix = "would delete" if config.dry_run else "deleted"
                print(f"{prefix} {old_global_output}")

    global_files_written = 0
    global_records = [
        cached_to_record(session)
        for session in sorted(next_sessions.values(), key=_session_sort_key, reverse=True)
        if _is_global_relevant(session.cwd, config.global_root)
    ]
    if global_output_path is not None and config.global_root.exists() and config.global_root.is_dir():
        if full_rebuild or global_affected or old_global_output != global_output_path:
            content = render_global_markdown(
                global_root=config.global_root,
                sessions=global_records,
                generated_at=generated_at,
            )
            wrote = write_text(global_output_path, content, dry_run=config.dry_run)
            if wrote:
                global_files_written += 1
                if config.verbose:
                    prefix = "would write" if config.dry_run else "wrote"
                    print(f"{prefix} {global_output_path}")
    elif config.verbose and global_output_path is not None:
        print(f"skip missing global root: {config.global_root}")

    next_state = SyncState(
        schema_version=STATE_SCHEMA_VERSION,
        config_fingerprint=fingerprint,
        session_index_signature=build_signature(config.session_index),
        sessions=next_sessions,
        managed_project_files=managed_project_files,
        global_output_path=str(global_output_path) if global_output_path is not None else None,
    )

    save_state(config.state_file, next_state, dry_run=config.dry_run)

    return SyncStats(
        tracked_sessions=len(next_sessions),
        parsed_session_files=parsed_session_files,
        project_files_written=project_files_written,
        project_files_deleted=project_files_deleted,
        skipped_missing_directories=skipped_missing_directories,
        global_files_written=global_files_written,
        full_rebuild=full_rebuild,
        reset_reason=reset_reason,
    )
