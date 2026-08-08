"""Canonical URL identity shared by ingestion deduplication and caches."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_QUERY_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref_src",
    }
)


def canonicalize_article_url(url):
    """Remove fragments/tracking data while retaining semantic query fields."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in TRACKING_QUERY_KEYS
        ),
        doseq=True,
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, query, "")
    )
