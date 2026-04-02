# Releasing Codex Session Indexer

This project currently ships from GitHub `main`, but releases should still be tagged so installs can be traced back to a known version.

## Checklist

1. Update the package version in:
   - `pyproject.toml`
   - `src/codex_sessions/__init__.py`
2. Run tests:

```bash
/Users/stephenjoly/Documents/Coding/codex-session-indexer/.venv/bin/python -m unittest discover -s tests -v
```

3. Commit the release changes:

```bash
git add .
git commit -m "Release v0.1.0"
```

4. Push `main`:

```bash
git push origin main
```

5. Create and push the release tag:

```bash
git tag v0.1.0
git push origin v0.1.0
```

6. Optionally publish a GitHub Release from that tag.

## Updating Users

Users can update by rerunning the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/stephenjoly/codex-session-indexer/main/install.sh | bash
```

Or, if they installed with `pipx`:

```bash
pipx upgrade codex-session-indexer
```

If they use the macOS background watcher, they can rerun:

```bash
curl -fsSL https://raw.githubusercontent.com/stephenjoly/codex-session-indexer/main/install.sh | bash -s -- --daemon
```
