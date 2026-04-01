from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

MANAGED_MARKER = "<!-- codex-sessions:managed -->"


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    thread_name: str
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


def parse_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(UTC)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def format_duration(total_seconds: float) -> str:
    seconds = max(0, int(round(total_seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def load_thread_names(index_path: Path) -> dict[str, str]:
    thread_names: dict[str, str] = {}
    if not index_path.exists():
        return thread_names

    with index_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            payload = json.loads(line)
            session_id = payload.get("id")
            thread_name = payload.get("thread_name")
            if session_id and thread_name:
                thread_names[session_id] = thread_name.strip()
    return thread_names


def iter_session_files(sessions_dir: Path) -> Iterable[Path]:
    yield from sorted(sessions_dir.rglob("*.jsonl"))


def extract_user_prompt_snippet(message: str, limit: int = 80) -> str:
    condensed = " ".join(message.split())
    if len(condensed) <= limit:
        return condensed
    return condensed[: limit - 3].rstrip() + "..."


def resolve_thread_name(
    *,
    session_id: str,
    thread_names: dict[str, str],
    first_user_prompt_snippet: str | None,
    filename_stem: str,
) -> tuple[str, str]:
    thread_name = thread_names.get(session_id, "").strip()
    if thread_name:
        return thread_name, "session_index"
    if first_user_prompt_snippet:
        return first_user_prompt_snippet, "first_user_prompt"
    return filename_stem, "filename"


def parse_session_file(session_file_path: Path, thread_names: dict[str, str]) -> SessionRecord:
    session_id = ""
    cwd: Path | None = None
    started_at: datetime | None = None
    last_updated_at: datetime | None = None
    first_user_prompt_snippet: str | None = None
    prompt_count = 0
    event_count = 0
    filename_stem = session_file_path.stem

    with session_file_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            event_count += 1
            record = json.loads(line)
            timestamp_raw = record.get("timestamp")
            if timestamp_raw:
                timestamp = parse_timestamp(timestamp_raw)
                if started_at is None:
                    started_at = timestamp
                last_updated_at = timestamp

            record_type = record.get("type")
            payload = record.get("payload", {})

            if record_type == "session_meta":
                session_id = payload.get("id", session_id)
                cwd_value = payload.get("cwd")
                if cwd_value:
                    cwd = Path(cwd_value).expanduser()
            elif record_type == "event_msg" and payload.get("type") == "user_message":
                prompt_count += 1
                if not first_user_prompt_snippet:
                    message = payload.get("message", "")
                    if message.strip():
                        first_user_prompt_snippet = extract_user_prompt_snippet(message)

    if not session_id:
        raise ValueError(f"Missing session id in {session_file_path}")
    if cwd is None:
        raise ValueError(f"Missing cwd in {session_file_path}")
    if started_at is None or last_updated_at is None:
        raise ValueError(f"Missing timestamps in {session_file_path}")

    thread_name, fallback_source = resolve_thread_name(
        session_id=session_id,
        thread_names=thread_names,
        first_user_prompt_snippet=first_user_prompt_snippet,
        filename_stem=filename_stem,
    )

    return SessionRecord(
        session_id=session_id,
        thread_name=thread_name,
        cwd=cwd,
        started_at=started_at,
        last_updated_at=last_updated_at,
        lifetime_seconds=(last_updated_at - started_at).total_seconds(),
        prompt_count=prompt_count,
        event_count=event_count,
        session_file_path=session_file_path,
        fallback_source=fallback_source,
        first_user_prompt_snippet=first_user_prompt_snippet,
        filename_stem=filename_stem,
    )


def _session_sort_key(session: SessionRecord) -> tuple[datetime, str]:
    return (session.last_updated_at, str(session.session_file_path))


def collect_sessions(sessions_dir: Path, index_path: Path) -> list[SessionRecord]:
    thread_names = load_thread_names(index_path)
    sessions = [parse_session_file(path, thread_names) for path in iter_session_files(sessions_dir)]
    sessions.sort(key=_session_sort_key, reverse=True)
    return sessions


def group_sessions_by_cwd(sessions: Iterable[SessionRecord]) -> dict[Path, list[SessionRecord]]:
    grouped: dict[Path, list[SessionRecord]] = defaultdict(list)
    for session in sessions:
        grouped[session.cwd].append(session)
    for cwd_sessions in grouped.values():
        cwd_sessions.sort(key=_session_sort_key, reverse=True)
    return dict(grouped)


def is_within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def render_session_table(sessions: Iterable[SessionRecord]) -> str:
    lines = [
        "| Session | Last Updated | Started | Lifetime | Prompts | Events | Session File |",
        "| --- | --- | --- | --- | ---: | ---: | --- |",
    ]
    for session in sessions:
        lines.append(
            "| "
            + " | ".join(
                [
                    escape_cell(session.thread_name),
                    format_timestamp(session.last_updated_at),
                    format_timestamp(session.started_at),
                    format_duration(session.lifetime_seconds),
                    str(session.prompt_count),
                    str(session.event_count),
                    escape_cell(str(session.session_file_path)),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def _format_highlight_line(session: SessionRecord, *, include_updated: bool) -> str:
    parts = [f"**{session.thread_name}**"]
    if include_updated:
        parts.append(f"updated {format_timestamp(session.last_updated_at)}")
    prompt_label = "prompt" if session.prompt_count == 1 else "prompts"
    parts.append(f"{session.prompt_count} {prompt_label}")
    parts.append(format_duration(session.lifetime_seconds))
    return "- " + " | ".join(parts)


def render_highlights(sessions: list[SessionRecord], *, top_n: int = 3) -> str:
    most_recent = sessions[:top_n]
    most_active = sorted(
        sessions,
        key=lambda session: (session.prompt_count, session.last_updated_at, str(session.session_file_path)),
        reverse=True,
    )[:top_n]

    lines = ["## Highlights", "", "### Most Recent Sessions"]
    for session in most_recent:
        lines.append(_format_highlight_line(session, include_updated=True))

    lines.extend(["", "### Most Active Sessions"])
    for session in most_active:
        lines.append(_format_highlight_line(session, include_updated=False))

    lines.append("")
    return "\n".join(lines)


def render_project_markdown(
    *,
    cwd: Path,
    sessions: list[SessionRecord],
    generated_at: datetime,
) -> str:
    return "\n".join(
        [
            MANAGED_MARKER,
            "# Codex Sessions",
            "",
            f"Generated: {format_timestamp(generated_at)}",
            "",
            "This file is fully generated by `codex-sessions generate`.",
            "",
            f"Directory: `{cwd}`",
            f"Session count: {len(sessions)}",
            "",
            render_highlights(sessions),
            render_session_table(sessions),
            "",
        ]
    )


def render_global_markdown(
    *,
    global_root: Path,
    sessions: list[SessionRecord],
    generated_at: datetime,
) -> str:
    lines = [
        MANAGED_MARKER,
        "# Codex Sessions Index",
        "",
        f"Generated: {format_timestamp(generated_at)}",
        "",
        "This file is fully generated by `codex-sessions generate`.",
        "",
        f"Global root: `{global_root}`",
        f"Session count: {len(sessions)}",
        "",
        render_highlights(sessions),
        "| Session | Project Path | Last Updated | Started | Lifetime | Prompts | Events | Session File |",
        "| --- | --- | --- | --- | --- | ---: | ---: | --- |",
    ]
    for session in sessions:
        project_label = str(session.cwd)
        if not session.cwd.exists():
            project_label += " (missing)"
        lines.append(
            "| "
            + " | ".join(
                [
                    escape_cell(session.thread_name),
                    escape_cell(project_label),
                    format_timestamp(session.last_updated_at),
                    format_timestamp(session.started_at),
                    format_duration(session.lifetime_seconds),
                    str(session.prompt_count),
                    str(session.event_count),
                    escape_cell(str(session.session_file_path)),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def write_text(path: Path, content: str, *, dry_run: bool) -> bool:
    existing_content: str | None = None
    if path.exists():
        existing_content = path.read_text(encoding="utf-8")
        if existing_content == content:
            return False

    if dry_run:
        return True

    path.write_text(content, encoding="utf-8")
    return True


def is_managed_generated_file(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        with path.open(encoding="utf-8") as handle:
            head = handle.read(512)
    except OSError:
        return False
    return MANAGED_MARKER in head


def delete_file(path: Path, *, dry_run: bool) -> bool:
    if not path.exists():
        return False
    if dry_run:
        return True
    path.unlink()
    return True
