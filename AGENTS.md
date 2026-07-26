# PROJECT KNOWLEDGE BASE

## OVERVIEW

Resumable Python crawler and artifact pipeline for PETY and SearchThema gazette metadata/PDFs. Runtime collection, OCR metadata, exports, and source code share one repository but have separate boundaries.

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Run collection | `crawl.py` | `pety`, `search-thema`, and `batch` modes |
| PETY collection | `src/crawler.py` | Playwright/session-backed AJAX and PDF access |
| SearchThema collection | `src/crawler_search_thema.py` | Search API, resume logic, PDF fallback |
| Shared network behavior | `src/base_crawler.py` | Throttling, retries, downloads |
| Metadata contract | `src/metadata_schema.py` | Shared envelope and synchronization rules |
| Persist metadata | `src/metadata_manager.py` | Item files and aggregate indexes |
| Resume state | `src/crawl_state.py` | State must survive interrupted runs |
| Tune collection | `config/config.yaml` | URLs, dates, windows, concurrency, timeouts |
| Export/diagnose | `scripts/` | Some legacy workflows still consume `data/searchThema/` |
| Validate behavior | `tests/` | Async tests, fixtures, crawler/OCR metadata coverage |

## CONVENTIONS

- Treat `src/`, `crawl.py`, `validate_pdfs.py`, `scripts/`, `config/`, tests, and docs as source/configuration.
- `artifacts/` is the canonical runtime-output root: metadata, PDFs, OCR-ready files, state, and validation reports.
- `data/searchThema/` is a legacy/export boundary used by selected batch, DuckDB, and Hugging Face scripts; do not make it the default runtime path.
- Change date ranges, throttling, retries, windows, pagination, and concurrency in `config/config.yaml` before changing crawler code.
- Preserve per-item metadata, aggregate indexes, hashes, and resumable state together.
- Keep Korean source values and identifiers unchanged.

## ANTI-PATTERNS

- Do not recursively inspect or normalize `artifacts/`, `data/`, `datasets/`, `logs/`, or `sync_work/` during source changes.
- Do not run long crawls, broad downloads, Playwright installs, Hugging Face uploads, or artifact cleanup without explicit operator intent.
- Do not bypass retry, timeout, throttling, file-size, integrity, or resume gates.
- Do not trigger OCR fallback/peer review when digital-PDF evidence or confidence gates say it is unnecessary.
- Do not reintroduce stale `src/pdf_handler.py` guidance; current OCR metadata modules are `pdf_text_metadata.py`, `pdf_layout_metadata.py`, and `pdf_extraction_peer_review.py`.

## COMMANDS

```bash
pytest tests/
python crawl.py --metadata-only --start-date 2026-04-24 --end-date 2026-04-24 --limit 1
python crawl.py search-thema --resume
python crawl.py batch --help
python validate_pdfs.py
```

Full collection and upload commands are operational actions, not routine validation.
