import feedparser
import requests
import cloudscraper


URLS = [
    "https://www.marktechpost.com/feed/",
    "https://strictlyvc.com/feed/",
    "https://a16z.com/feed/",
]


def main():
    """Run the opt-in live feed diagnostic without import-time network I/O."""
    scraper = cloudscraper.create_scraper()
    failures = 0
    for url in URLS:
        try:
            response = scraper.get(
                url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            if feed.entries:
                print(f"SUCCESS: {url} - {len(feed.entries)} entries")
            else:
                failures += 1
                print(f"FAILED: {url} - no entries")
        except Exception as error:
            failures += 1
            print(f"FAILED: {url} - {error}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
