#!/usr/bin/env python3
"""Compatibility entrypoint for the Playwright-based crawler."""

from __future__ import annotations

import asyncio

try:
    from .config import get_config
    from .crawler import GwanboCrawler
except ImportError:
    from config import get_config
    from crawler import GwanboCrawler


class GwanboCrawlerBrowser(GwanboCrawler):
    """Alias kept for existing imports."""

    async def crawl_date_range(self, start_date: str, end_date: str):
        self.start_date = self._parse_date(start_date)
        self.end_date = self._parse_date(end_date)
        return await self.crawl()


async def main():
    config = get_config()
    crawler_config = config.get_crawler_config()
    crawler = GwanboCrawlerBrowser(
        start_date=crawler_config.get("start_date", "1994-01-01"),
        end_date=crawler_config.get("end_date", "today"),
    )
    stats = await crawler.crawl()
    print("\n=== Playwright 크롤링 통계 ===")
    for key, value in stats.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    asyncio.run(main())
