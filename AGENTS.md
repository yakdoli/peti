# Codex project instructions: peti

## Project purpose

This repository is a Python crawler for Korean electronic gazette data. It collects public-official asset disclosure metadata and PDFs from `petyListAjax` and `SearchRestApi`, writes resumable crawler state, and prepares artifacts for OCR and Hugging Face dataset export.

## Repository boundaries

- Treat `src/`, root crawler entrypoints, `scripts/`, `config/`, tests, and documentation as source/configuration.
- Treat `artifacts/`, `datasets/`, `logs/`, `data/`, generated PDFs, OCR images, DuckDB exports, and upload checkpoints as generated outputs unless the user explicitly asks to inspect or modify them.
- Do not commit or normalize large generated artifacts as part of code changes.
- Preserve the separation described in `PROJECT_LAYOUT.md`: crawler source, collection artifacts, OCR-ready artifacts, and Hugging Face dataset exports are separate concerns.

## Main modules

- `src/crawler.py`: `pety` crawler using Playwright/session-backed access to the public Gwanbo screen and AJAX endpoints.
- `src/crawler_search_thema.py`: `searchThema` crawler using HTTP POST against Gwanbo search APIs.
- `src/base_crawler.py`: shared async HTTP and PDF download behavior.
- `src/metadata_manager.py`: metadata item/index read/write behavior.
- `src/crawl_state.py`: resumable crawl state.
- `src/pety_parser.py` and `src/search_thema_parser.py`: source-specific parsing.
- `src/pdf_validator.py` and `validate_pdfs.py`: PDF validation.
- `scripts/final_upload_chunks.py`, `scripts/batched_upload_pdfs.py`, and related scripts: dataset/export/diagnostic workflows.

## Configuration

- Primary crawler configuration is `config/config.yaml`.
- Date ranges, throttling, timeouts, `window_days`, `row_per_page`, and `max_concurrent_downloads` should be changed in configuration before changing crawler code.
- Keep external URLs and source-specific API parameters centralized in `config/config.yaml` unless there is a clear reason not to.

## Working rules for Codex

- Prefer small, targeted edits that preserve crawler resumability and existing artifact layout.
- Before changing network behavior, account for retry limits, timeout behavior, server load, and resumability from `artifacts/state/`.
- Do not run long crawls, broad downloads, Playwright browser installs, Hugging Face uploads, or destructive artifact cleanup unless the user explicitly asks.
- Do not inspect generated artifacts by default; they can be large.
- When library or API usage is uncertain, use Context7 MCP for current docs before changing code.
- When working on Hugging Face dataset upload/export scripts, prefer the Hugging Face plugin/tools when current Hub behavior matters.

## Common commands

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python crawl.py
python crawl.py --metadata-only --start-date 2026-04-24 --end-date 2026-04-24
python crawl.py --rebuild-index
python crawl_search_thema.py --resume
python validate_pdfs.py
pytest tests/
```

Do not run these commands unless the user asks for execution or validation.
