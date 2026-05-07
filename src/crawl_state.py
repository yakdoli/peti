"""Persistent crawl state for resumable runs."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


class CrawlState:
    """Small JSON-backed state store."""

    def __init__(self, state_file: str = "data/state/crawl_state.json"):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state: Dict[str, Any] = {
            "completed_windows": {},
            "last_updated": "",
        }
        self.load()

    def load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.state.update(loaded)
                self._migrate_legacy_window_keys()
        except (OSError, json.JSONDecodeError):
            return

    def is_window_completed(self, theme: str, start_date: str, end_date: str, mode: str = "pdf") -> bool:
        return self._window_key(theme, start_date, end_date, mode) in self.state.get("completed_windows", {})

    def mark_window_completed(
        self,
        theme: str,
        start_date: str,
        end_date: str,
        stats: Dict[str, Any],
        mode: str = "pdf",
    ) -> None:
        key = self._window_key(theme, start_date, end_date, mode)
        self.state.setdefault("completed_windows", {})[key] = {
            "theme": theme,
            "mode": mode,
            "start_date": start_date,
            "end_date": end_date,
            "stats": stats,
            "completed_at": datetime.now().isoformat(),
        }
        self.state["last_updated"] = datetime.now().isoformat()
        self.save()

    def save(self) -> None:
        temp_file = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2, default=str)
        temp_file.replace(self.state_file)

    def _migrate_legacy_window_keys(self) -> None:
        completed = self.state.get("completed_windows", {})
        if not isinstance(completed, dict):
            self.state["completed_windows"] = {}
            return

        migrated = {}
        changed = False
        for key, value in completed.items():
            if len(key.split(":")) == 3:
                theme, start_date, end_date = key.split(":")
                new_key = self._window_key(theme, start_date, end_date, "pdf")
                if isinstance(value, dict):
                    value = dict(value)
                    value.setdefault("mode", "pdf")
                migrated[new_key] = value
                changed = True
            else:
                migrated[key] = value

        if changed:
            self.state["completed_windows"] = migrated

    @staticmethod
    def _window_key(theme: str, start_date: str, end_date: str, mode: str = "pdf") -> str:
        return f"{theme}:{mode}:{start_date}:{end_date}"
