# Codex Cloud Environment

This repository is prepared for Codex Cloud with a reproducible Python/Playwright setup.

## Repository files

- `.devcontainer/`: local VS Code, Dev Containers, and GitHub Codespaces environment.
- `.codex/setup.sh`: setup script to paste into the Codex Cloud environment settings.
- `.codex/maintenance.sh`: optional cache maintenance script for Codex Cloud.
- `AGENTS.md`: project instructions Codex should read before changing crawler code.

## Codex Cloud setup

Create the environment in Codex settings:

1. Open `https://chatgpt.com/codex`.
2. Connect GitHub and select `yakdoli/peti`.
3. Create an environment named `peti-python-playwright`.
4. Set the package/runtime version to Python `3.11.12`.
5. Use `.codex/setup.sh` as the setup script.
6. Use `.codex/maintenance.sh` as the maintenance script.
7. Keep agent internet access off for ordinary coding and test tasks.

Environment variables:

```text
CODEX_ENV_PYTHON_VERSION=3.11.12
PYTHONUNBUFFERED=1
PIP_DISABLE_PIP_VERSION_CHECK=1
```

No secrets are required for normal source changes and test runs. Do not add Hugging Face or other upload tokens unless a task explicitly requires an upload workflow.

## Validation command

Use this as the default validation command after Codex changes source code:

```bash
source .venv/bin/activate
pytest tests/
```

Do not run full crawls, broad downloads, Hugging Face uploads, or artifact cleanup from Codex Cloud unless the task explicitly asks for it.

## Network policy

Setup scripts run with internet access so dependencies and Playwright Chromium can be installed. The agent phase should stay offline by default.

If a task explicitly needs a narrow crawler smoke test, enable agent internet access only for that environment and restrict it to the Gwanbo hosts:

```text
open.gwanbo.go.kr
gwanbo.go.kr
```

Allow `GET`, `HEAD`, and `POST` only for that smoke-test environment because the crawler uses POST endpoints. Turn agent internet access off again for normal code tasks.

Example narrow smoke test:

```bash
source .venv/bin/activate
python crawl.py --metadata-only --start-date 2026-04-24 --end-date 2026-04-24 --limit 1
```
