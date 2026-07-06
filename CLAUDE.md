# CLAUDE.md

## Autonomy Rules

Work autonomously. Do not ask for confirmation before making changes to any file, folder, or dependency that is part of this project.

**Only stop and ask before:**
- Deleting any file or folder
- Modifying, installing, or uninstalling anything outside the project directory (`D:\Dev\dual-agent-core\case-manager-agent-dev\`)
- Committing to git (user verifies live behavior first)
- Restarting the production server (Claire actively messages the real Chat space)

Everything else — editing code, adding files, installing packages listed in requirements.txt, running tests — just do it.

## Project

Local HIPAA-compliant AI case manager for a chiropractic/PT clinic. FastAPI + Ollama (glm-4.7-flash for Claire, qwen3:32b for drafts) + ChromaDB + Gmail API + Google Chat API. All PHI stays on-machine.

See `memory.md` for full project state — especially the **"Current state / handoff"** section at the bottom for the latest server status, uncommitted work, and pending verification steps.

## Server Launch

PowerShell:
```
$env:PYTHONUTF8=1; .venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```
Detached from Git Bash:
```
PYTHONUTF8=1 REQUESTS_CA_BUNDLE="$(pwd)/windows_cacerts.pem" nohup .venv/Scripts/python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 >/dev/null 2>&1 &
```
Do NOT redirect stdout to a log file — logs rotate at `logs/app.log` (config.setup_logging()).
Check first: `netstat -ano | findstr :8000` — the server is usually already running.
Health: GET http://127.0.0.1:8000/health

## Testing

```
.venv\Scripts\python.exe -m pytest tests/
```
101 tests passing as of 2026-07-03. Tests are unit-only (no network); retry state is isolated to tmp_path via conftest.py.
