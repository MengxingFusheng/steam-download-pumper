"""Ookla download templates shared by discovery and publisher selection."""
from urllib.parse import parse_qs, urlsplit, urlunsplit

DOWNLOAD_QUERY = "size=67108864&guid={{guid}}&nocache={{nonce}}"


def is_ookla_download(url: str) -> bool:
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    return (parsed.path == "/download" and query.get("size") == ["67108864"]
            and query.get("guid") == ["{{guid}}"] and query.get("nocache") == ["{{nonce}}"])


def download_url(server: dict) -> str:
    # Use the advertised download host, including its actual port. The legacy
    # upload URL can have a different hostname from the current server endpoint.
    legacy = urlsplit(server.get("url", ""))
    host = server.get("host") or legacy.netloc
    if not isinstance(host, str) or not host or any(c in host for c in "/?#@"):
        raise ValueError("invalid Ookla host")
    scheme = "https" if server.get("https_functional") in (1, True, "1") else legacy.scheme
    if scheme not in {"http", "https"}:
        raise ValueError("missing Ookla HTTP transport")
    return urlunsplit((scheme, host, "/download", DOWNLOAD_QUERY, ""))
