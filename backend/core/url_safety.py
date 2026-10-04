"""Destination policy for URLs the backend fetches on a caller's behalf.

URL imports (video ingest, voice-gallery clips) hand a caller-supplied URL to
yt-dlp. Without a destination policy that turns the backend into a proxy into
the user's own machine and network: loopback services, the LAN, router admin
pages and cloud metadata endpoints. Every URL import therefore goes through
three layers, all defined here so the policy cannot drift between callers:

1. :func:`check_public_url` — up-front validation for a clear 400: http(s)
   only, and every DNS answer for the host must be a public address.
2. :func:`guard_outbound_connections` — connect-time enforcement. While it is
   active, ``socket.getaddrinfo`` drops non-public answers for the current
   thread (or the whole process, in the yt-dlp subprocess). This covers what
   up-front validation cannot: HTTP redirects, URLs discovered inside the
   fetched page or manifest, and DNS answers that change between the check
   and the connection.
3. :func:`check_resolved_media_urls` — the media URLs yt-dlp resolved are
   validated before download, because ffmpeg (clip sections, some stream
   types) fetches them in its own process, outside the socket guard.

Private destinations are allowed only when the user opts in with
``OMNIVOICE_ALLOW_PRIVATE_URL_IMPORTS=1`` (for example to import from a media
server on their own network). Configured HTTP(S)/SOCKS proxies remain
reachable: with a proxy the proxy resolves the destination, so only layer 1
and 3 apply there.

Stdlib-only on purpose: the guarded yt-dlp subprocess imports this module
before anything else.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import socket
import threading
import urllib.request
from collections.abc import Iterator
from urllib.parse import urlsplit

ALLOW_PRIVATE_ENV = "OMNIVOICE_ALLOW_PRIVATE_URL_IMPORTS"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Carrier-grade NAT (RFC 6598) and the NAT64 well-known prefix (RFC 6052).
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

PRIVATE_DESTINATION_DETAIL = (
    "This URL points to a private network address (this computer, the local "
    "network, or a link-local service), which URL imports don't fetch. Use a "
    "public link or download the file and add it directly. To import from a "
    "server on your own network, set "
    f"{ALLOW_PRIVATE_ENV}=1 and restart VoiceStudio."
)
SCHEME_DETAIL = (
    "URL must start with http:// or https://. Paste a full link "
    "(e.g. https://youtube.com/watch?v=…) or add a local file instead."
)


class UnsafeURLError(ValueError):
    """A URL import was refused by the destination policy."""


def private_url_imports_allowed() -> bool:
    """Whether the user opted in to URL imports from private addresses."""
    return os.environ.get(ALLOW_PRIVATE_ENV, "").strip().lower() in _TRUTHY


def _embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 transition form actually reaches, if any."""
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        return address.teredo[1]
    if address in _NAT64:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def is_public_address(value: str) -> bool:
    """True only for globally routable unicast addresses.

    Refuses loopback, RFC 1918/ULA private ranges, link-local (including the
    169.254.169.254 metadata service), CGNAT, multicast, unspecified,
    reserved and documentation ranges, and every IPv6 form that embeds one of
    those IPv4 addresses (IPv4-mapped, 6to4, Teredo, NAT64).
    """
    try:
        address = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(address)
        if embedded is not None:
            address = embedded
    if (
        address.is_multicast
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_reserved
    ):
        return False
    if isinstance(address, ipaddress.IPv4Address) and address in _CGNAT:
        return False
    return address.is_global


def _split_http_url(url: str):
    try:
        parsed = urlsplit(str(url).strip())
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise UnsafeURLError(SCHEME_DETAIL) from exc
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES or not parsed.hostname:
        raise UnsafeURLError(SCHEME_DETAIL)
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.hostname, port


def check_public_url(url: str) -> str:
    """Validate a caller-supplied import URL; return it stripped.

    Raises :class:`UnsafeURLError` (message suitable for a 400) when the
    scheme is not http(s) or any DNS answer is a non-public address. A host
    that does not resolve here is let through: DNS may only work through a
    configured proxy, and the connect-time guard still applies to any direct
    connection. Blocking — call from a worker thread in async code.
    """
    url = str(url or "").strip()
    host, port = _split_http_url(url)
    if private_url_imports_allowed():
        return url
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return url
    if any(not is_public_address(answer[4][0]) for answer in answers):
        raise UnsafeURLError(PRIVATE_DESTINATION_DETAIL)
    return url


def check_resolved_media_urls(info: dict) -> None:
    """Validate every media URL yt-dlp resolved for one download.

    Run before download (a ``before_dl`` post-processor) so URLs that ffmpeg
    fetches directly are held to the same policy as the page URL.
    """
    if private_url_imports_allowed():
        return
    formats = info.get("requested_formats") or [info]
    for fmt in formats:
        for key in ("url", "manifest_url", "fragment_base_url"):
            value = fmt.get(key)
            if value:
                check_public_url(value)


# ── Connect-time guard ──────────────────────────────────────────────────

_real_getaddrinfo = socket.getaddrinfo
_install_lock = threading.Lock()
_installed = False
_process_wide = False
_thread_state = threading.local()


def _proxy_hosts() -> frozenset[str]:
    """Hostnames of the system/environment proxies yt-dlp would use."""
    hosts = set()
    try:
        proxies = urllib.request.getproxies()
    except Exception:  # noqa: BLE001 - a broken proxy config must not break imports
        proxies = {}
    for name, value in proxies.items():
        if name == "no" or not value:
            continue
        try:
            host = urlsplit(value if "://" in value else f"http://{value}").hostname
        except ValueError:
            continue
        if host:
            hosts.add(host.lower())
    return frozenset(hosts)


def _guard_active() -> bool:
    return _process_wide or getattr(_thread_state, "depth", 0) > 0


def _guarded_getaddrinfo(host, port, *args, **kwargs):
    answers = _real_getaddrinfo(host, port, *args, **kwargs)
    if not _guard_active() or private_url_imports_allowed():
        return answers
    name = host.decode("idna") if isinstance(host, bytes) else str(host or "")
    proxies = getattr(_thread_state, "proxy_hosts", None)
    if proxies is None:
        proxies = _proxy_hosts()
    if name.strip("[]").lower() in proxies:
        return answers
    public = [answer for answer in answers if is_public_address(answer[4][0])]
    if not public:
        raise socket.gaierror(
            socket.EAI_NONAME,
            f"refusing to connect to a private network address ({ALLOW_PRIVATE_ENV}=1 allows it)",
        )
    return public


def _install() -> None:
    global _installed
    with _install_lock:
        if not _installed:
            socket.getaddrinfo = _guarded_getaddrinfo
            _installed = True


@contextlib.contextmanager
def guard_outbound_connections() -> Iterator[None]:
    """Refuse non-public destinations for connections made by this thread."""
    _install()
    depth = getattr(_thread_state, "depth", 0)
    if depth == 0:
        _thread_state.proxy_hosts = _proxy_hosts()
    _thread_state.depth = depth + 1
    try:
        yield
    finally:
        _thread_state.depth = depth
        if depth == 0:
            _thread_state.proxy_hosts = None


def guard_process_connections() -> None:
    """Refuse non-public destinations for every connection in this process.

    For the dedicated yt-dlp subprocess only — never call it in the backend.
    """
    global _process_wide
    _install()
    _process_wide = True


def ytdlp_url_guard_postprocessor():
    """A ``before_dl`` yt-dlp post-processor enforcing the media URL policy."""
    from yt_dlp.postprocessor.common import PostProcessor
    from yt_dlp.utils import DownloadError

    class _MediaURLGuard(PostProcessor):
        def run(self, info):
            try:
                check_resolved_media_urls(info)
            except UnsafeURLError as exc:
                raise DownloadError(str(exc)) from exc
            return [], info

    return _MediaURLGuard()


def run_guarded_ytdlp(argv: list[str]) -> int:
    """yt-dlp CLI entry point with the destination policy enforced.

    Used by ``python -c`` in the gallery subprocess (see
    ``services.media_tools.guarded_ytdlp_invocation``).
    """
    guard_process_connections()
    import yt_dlp

    parsed = yt_dlp.parse_options(argv)
    with yt_dlp.YoutubeDL(parsed.ydl_opts) as ydl:
        ydl.add_post_processor(ytdlp_url_guard_postprocessor(), when="before_dl")
        return ydl.download(parsed.urls)
