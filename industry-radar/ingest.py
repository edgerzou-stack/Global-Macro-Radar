import feedparser
import requests
import concurrent.futures
from datetime import datetime, timedelta

def fetch_rss_feeds(feeds, hours_back=168):
    """Fetches articles from a list of RSS feeds within the last X hours, in parallel."""
    time_limit = datetime.now() - timedelta(hours=hours_back)

    def fetch_single_feed(feed_url):
        local_articles = []
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
            res = requests.get(feed_url, headers=headers, timeout=10)
            feed = feedparser.parse(res.content)
            for entry in feed.entries:
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    pub_date = datetime(*entry.published_parsed[:6])
                elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                    pub_date = datetime(*entry.updated_parsed[:6])
                else:
                    pub_date = datetime.now()

                if pub_date >= time_limit:
                    local_articles.append({
                        'title': entry.title,
                        'link': entry.link,
                        'summary': getattr(entry, 'summary', ''),
                        'source': feed.feed.title if hasattr(feed.feed, 'title') else feed_url,
                        'published_at': pub_date.isoformat()
                    })
            source_name = feed.feed.title if hasattr(feed.feed, 'title') else feed_url
            print(f"  ✓ {source_name}: {len(local_articles)} articles")
        except Exception as e:
            print(f"  ✗ {feed_url}: {e}", flush=True)
        return local_articles

    print(f"Fetching {len(feeds)} RSS feeds in parallel...")
    articles = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_single_feed, url): url for url in feeds}
        for future in concurrent.futures.as_completed(futures):
            articles.extend(future.result())

    print(f"Total: {len(articles)} articles fetched.")
    return articles

if __name__ == "__main__":
    feeds = ["https://techcrunch.com/feed/"]
    print(fetch_rss_feeds(feeds, hours_back=168))
