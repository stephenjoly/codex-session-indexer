from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .sync import SyncConfig, run_sync


@dataclass
class DebouncedTrigger:
    last_change_monotonic: float | None = None
    dirty: bool = False

    def mark_dirty(self, now: float | None = None) -> None:
        self.dirty = True
        self.last_change_monotonic = time.monotonic() if now is None else now

    def ready(self, debounce_seconds: float, now: float | None = None) -> bool:
        if not self.dirty or self.last_change_monotonic is None:
            return False
        current = time.monotonic() if now is None else now
        return current - self.last_change_monotonic >= debounce_seconds

    def clear(self) -> None:
        self.dirty = False
        self.last_change_monotonic = None


class RelevantChangeHandler(FileSystemEventHandler):
    def __init__(self, sessions_dir: Path, session_index: Path, trigger: DebouncedTrigger) -> None:
        super().__init__()
        self.sessions_dir = sessions_dir.resolve()
        self.session_index = session_index.resolve()
        self.trigger = trigger

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path).resolve()
        if self._is_relevant(path):
            self.trigger.mark_dirty()

    def _is_relevant(self, path: Path) -> bool:
        if path == self.session_index:
            return True
        if path.suffix != ".jsonl":
            return False
        try:
            path.relative_to(self.sessions_dir)
            return True
        except ValueError:
            return False


def watch_forever(
    config: SyncConfig,
    *,
    debounce_seconds: float,
    stop_event: Event | None = None,
    observer_factory: type[Observer] = Observer,
    sync_runner=run_sync,
    sleep_interval: float = 0.1,
) -> None:
    sync_runner(config)
    if stop_event is not None and stop_event.is_set():
        return

    trigger = DebouncedTrigger()
    handler = RelevantChangeHandler(config.sessions_dir, config.session_index, trigger)
    observer = observer_factory()
    observer.schedule(handler, str(config.sessions_dir), recursive=True)
    if config.session_index.parent.resolve() != config.sessions_dir.resolve():
        observer.schedule(handler, str(config.session_index.parent), recursive=False)
    observer.start()

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if trigger.ready(debounce_seconds):
                trigger.clear()
                sync_runner(config)
            time.sleep(sleep_interval)
    except KeyboardInterrupt:
        if config.verbose:
            print("watch stopped")
    finally:
        observer.stop()
        observer.join()
