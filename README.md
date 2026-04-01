# Codex Session Indexer

Generate Markdown indexes from your local Codex session history.

The tool scans `~/.codex/sessions` and `~/.codex/session_index.jsonl`, then writes:

- A fully generated `codex-sessions.md` in each discovered session working directory
- A global recent-sessions index in a chosen root directory
- A persistent incremental sync cache in `~/.codex/codex-session-indexer-state.json`

## Why

Codex keeps session history in `~/.codex/sessions`, but sessions are grouped together across projects. This tool projects that history back into the directories where you worked so you can see recent conversations without opening the raw session store.

## Install

One-command install after you publish the repo:

```bash
curl -fsSL https://raw.githubusercontent.com/stephenjoly/codex-session-indexer/main/install.sh | bash
```

One-command install plus background watcher on macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/stephenjoly/codex-session-indexer/main/install.sh | bash -s -- --daemon
```

During local development, you can test the installer against a local checkout:

```bash
bash install.sh --repo-url /absolute/path/to/codex-session-indexer
```

Manual install:

```bash
cd codex-session-indexer
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
```

## Usage

Default run:

```bash
codex-sessions generate
```

`generate` is incremental by default. It reparses only changed session files and rewrites only affected Markdown outputs.

Useful flags:

```bash
codex-sessions generate \
  --sessions-dir ~/.codex/sessions \
  --session-index ~/.codex/session_index.jsonl \
  --global-root ~/Documents/Coding \
  --output-filename codex-sessions.md \
  --global-output-name codex-sessions-index.md \
  --full-rebuild \
  --verbose
```

Preview without writing:

```bash
codex-sessions generate --dry-run --verbose
```

Force a full rebuild:

```bash
codex-sessions generate --full-rebuild
```

Watch for new and updated Codex sessions:

```bash
codex-sessions watch \
  --global-root ~/Documents/Coding \
  --debounce-seconds 1.0 \
  --verbose
```

The installer creates a `codex-sessions` symlink in `~/.local/bin` by default.

## Derived Metadata

For each session, the tool records:

- Session name
- `cwd`
- Started time
- Last updated time
- Lifetime
- Prompt count
- Event count
- Session JSONL file path

Thread name fallback order:

1. `thread_name` from `~/.codex/session_index.jsonl`
2. First user prompt snippet
3. Session filename

## Output Files

- Per-project files are written to the exact discovered `cwd`
- The global index only includes sessions whose `cwd` is inside `--global-root`
- Missing directories are skipped for per-project output and reported in CLI output
- Generated project files for directories that no longer have any sessions are deleted automatically
- Files are only deleted if they contain the tool's managed marker

By default, the global file is `codex-sessions-index.md` to avoid colliding with the root directory's own per-project `codex-sessions.md`.

## Incremental State

The sync cache is stored at:

```text
~/.codex/codex-session-indexer-state.json
```

This lets `generate` avoid reparsing unchanged session JSONL files and prevents unnecessary rewrites on no-op runs.

## Scheduling

Refresh every 15 minutes on macOS or Linux:

```cron
*/15 * * * * /usr/bin/env PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH" codex-sessions generate --global-root "$HOME/Documents/Coding" >> "$HOME/.codex/codex-session-indexer.log" 2>&1
```

Adjust the `PATH` to match where `codex-sessions` is installed.

If you want near-real-time updates instead, run `codex-sessions watch` in a long-lived shell session or service manager.

On macOS, the installer can also register a `launchd` watcher for you:

```bash
curl -fsSL https://raw.githubusercontent.com/stephenjoly/codex-session-indexer/main/install.sh | bash -s -- --daemon
```

## Development

Run tests:

```bash
python3 -m unittest discover -s tests -v
```

Run against your real session store:

```bash
source .venv/bin/activate
codex-sessions generate --verbose
```
