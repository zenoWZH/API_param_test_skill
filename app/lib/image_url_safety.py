from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from .image_validation import (
    MAX_IMAGE_DECODED_BYTES,
    MAX_IMAGE_EDGE,
    MAX_IMAGE_PAYLOAD_BYTES,
    MAX_IMAGE_PIXELS,
    inspect_image_bytes,
)


ALLOWED_IMAGE_MIME_TYPES = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}
MAX_REMOTE_IMAGE_BYTES = MAX_IMAGE_PAYLOAD_BYTES
MAX_REMOTE_IMAGE_ADDRESS_ATTEMPTS = 4
REMOTE_IMAGE_USER_AGENT = "Mozilla/5.0 (compatible; yibuapi-image-param-test/1.0)"


class UnsafeImageUrl(ValueError):
    """Raised when a remote image cannot be fetched under the SSRF policy."""


@dataclass(frozen=True)
class RemoteImage:
    data: bytes
    mime_type: str
    peer_ip: str


Resolver = Callable[..., Iterable[tuple[Any, ...]]]
ConnectionFactory = Callable[
    [str, str, int, str, float], http.client.HTTPConnection
]
Clock = Callable[[], float]


def fetch_remote_image(
    url: str,
    timeout: int,
    *,
    resolver: Resolver = socket.getaddrinfo,
    connection_factory: ConnectionFactory | None = None,
    monotonic: Clock = time.monotonic,
) -> RemoteImage:
    """Fetch one public HTTP(S) image through a DNS-pinned direct connection.

    The hostname is resolved exactly once. At most four addresses from that
    approved set are tried in resolver order under one total timeout, while HTTPS
    still verifies the original hostname via SNI and the certificate. No proxy
    environment or redirect is consulted.
    """

    bounded_timeout = min(max(int(timeout), 1), 60)
    deadline = monotonic() + bounded_timeout
    parsed, host, port = _validated_remote_target(url)
    approved_addresses = _resolve_public_addresses(host, port, resolver=resolver)
    factory = connection_factory or _direct_connection
    last_transport_error: (
        OSError | http.client.HTTPException | ssl.SSLError | None
    ) = None
    for pinned_ip in approved_addresses[:MAX_REMOTE_IMAGE_ADDRESS_ATTEMPTS]:
        try:
            attempt_timeout = _remaining_timeout(deadline, monotonic)
            return _fetch_from_pinned_address(
                parsed,
                host,
                port,
                pinned_ip,
                attempt_timeout,
                factory,
                deadline,
                monotonic,
            )
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            last_transport_error = exc
            if monotonic() >= deadline:
                break
    if last_transport_error is None:  # Defensive: resolver already rejects this state.
        raise UnsafeImageUrl("remote image hostname resolved to no usable address")
    raise UnsafeImageUrl(
        f"remote image fetch failed: {last_transport_error.__class__.__name__}"
    ) from last_transport_error


def _fetch_from_pinned_address(
    parsed: Any,
    host: str,
    port: int,
    pinned_ip: str,
    timeout: float,
    factory: ConnectionFactory,
    deadline: float,
    monotonic: Clock,
) -> RemoteImage:
    connection: http.client.HTTPConnection | None = None
    response: Any | None = None
    try:
        connection = factory(
            parsed.scheme.casefold(),
            host,
            port,
            pinned_ip,
            timeout,
        )
        _apply_connection_timeout(connection, deadline, monotonic)
        request_target = parsed.path or "/"
        if parsed.query:
            request_target += f"?{parsed.query}"
        connection.request(
            "GET",
            request_target,
            headers={
                "Accept": "image/png, image/jpeg, image/webp",
                "User-Agent": REMOTE_IMAGE_USER_AGENT,
            },
        )
        _apply_connection_timeout(connection, deadline, monotonic)
        response = connection.getresponse()
        _apply_connection_timeout(connection, deadline, monotonic)
        status_code = int(getattr(response, "status_code", 0) or 0)
        if not status_code:
            status_code = int(getattr(response, "status", 0) or 0)
        if 300 <= status_code < 400:
            raise UnsafeImageUrl("remote image redirects are not allowed")
        if not 200 <= status_code < 300:
            raise UnsafeImageUrl(
                f"remote image returned unexpected HTTP status {status_code}"
            )

        peer_ip = _connected_peer_ip(connection)
        peer = _public_ip(peer_ip, context="connected peer")
        if peer.compressed != pinned_ip:
            raise UnsafeImageUrl(
                "connected peer does not match the DNS-pinned address"
            )

        mime_type = _validated_mime_type(response)
        content_length = _content_length(response)
        if content_length is not None and content_length > MAX_REMOTE_IMAGE_BYTES:
            raise UnsafeImageUrl(
                f"remote image exceeds {MAX_REMOTE_IMAGE_BYTES} byte limit"
            )

        chunks: list[bytes] = []
        size = 0
        while True:
            raw_chunk = response.read(64 * 1024)
            _apply_connection_timeout(connection, deadline, monotonic)
            if not raw_chunk:
                break
            chunk = bytes(raw_chunk)
            size += len(chunk)
            if size > MAX_REMOTE_IMAGE_BYTES:
                raise UnsafeImageUrl(
                    f"remote image exceeds {MAX_REMOTE_IMAGE_BYTES} byte limit"
                )
            chunks.append(chunk)
        data = b"".join(chunks)
        _validate_downloaded_image(data, mime_type)
        _remaining_timeout(deadline, monotonic)
        return RemoteImage(data=data, mime_type=mime_type, peer_ip=peer.compressed)
    finally:
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()


def _remaining_timeout(deadline: float, monotonic: Clock) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("remote image fetch deadline exceeded")
    return remaining


def _apply_connection_timeout(
    connection: http.client.HTTPConnection,
    deadline: float,
    monotonic: Clock,
) -> None:
    remaining = _remaining_timeout(deadline, monotonic)
    if hasattr(connection, "timeout"):
        connection.timeout = remaining
    sock = getattr(connection, "sock", None)
    settimeout = getattr(sock, "settimeout", None)
    if callable(settimeout):
        settimeout(remaining)


def _validated_remote_target(url: str) -> tuple[Any, str, int]:
    try:
        parsed = urlsplit(str(url))
        port = parsed.port
    except ValueError as exc:
        raise UnsafeImageUrl(f"invalid remote image URL: {exc}") from exc
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise UnsafeImageUrl("remote image URL must use http or https")
    if not parsed.hostname:
        raise UnsafeImageUrl("remote image URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeImageUrl("remote image URL must not include userinfo")
    authority = parsed.netloc.rsplit("@", 1)[-1]
    if authority.endswith(":"):
        raise UnsafeImageUrl("remote image URL must not contain an empty port")
    host = parsed.hostname.rstrip(".").casefold()
    if not host or host == "localhost" or host.endswith(".localhost"):
        raise UnsafeImageUrl("localhost image URLs are not allowed")
    effective_port = (
        443 if parsed.scheme.casefold() == "https" else 80
    ) if port is None else int(port)
    if not 1 <= effective_port <= 65535:
        raise UnsafeImageUrl("remote image URL port must be between 1 and 65535")
    return parsed, host, effective_port


def _resolve_public_addresses(
    host: str,
    port: int,
    *,
    resolver: Resolver,
) -> list[str]:
    try:
        rows = resolver(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise UnsafeImageUrl(f"remote image hostname resolution failed: {exc}") from exc
    addresses: list[str] = []
    seen: set[str] = set()
    for row in rows:
        try:
            raw_address = str(row[4][0])
        except (IndexError, TypeError):
            continue
        address = _public_ip(raw_address, context="DNS result").compressed
        if address not in seen:
            seen.add(address)
            addresses.append(address)
    if not addresses:
        raise UnsafeImageUrl("remote image hostname resolved to no usable address")
    return addresses


def _public_ip(value: str, *, context: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        address = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError as exc:
        raise UnsafeImageUrl(f"{context} is not an IP address") from exc
    if (
        not address.is_global
        or address.is_multicast
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_reserved
        or getattr(address, "is_site_local", False)
    ):
        raise UnsafeImageUrl(f"{context} must be a public global address")
    return address


def _direct_connection(
    scheme: str,
    host: str,
    port: int,
    pinned_ip: str,
    timeout: float,
) -> http.client.HTTPConnection:
    if scheme == "https":
        return _PinnedHTTPSConnection(
            host,
            port,
            pinned_ip=pinned_ip,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    return _PinnedHTTPConnection(
        host,
        port,
        pinned_ip=pinned_ip,
        timeout=timeout,
    )


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, *, pinned_ip: str, timeout: float, **kwargs: Any) -> None:
        super().__init__(host, port, timeout=timeout, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, *, pinned_ip: str, timeout: float, **kwargs: Any) -> None:
        super().__init__(host, port, timeout=timeout, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
        except Exception:
            raw_socket.close()
            raise


def _connected_peer_ip(connection: Any) -> str:
    explicit = getattr(connection, "peer_ip", None)
    if explicit:
        return str(explicit)
    sock = getattr(connection, "sock", None)
    try:
        peer = sock.getpeername()
    except (AttributeError, OSError):
        peer = None
    if isinstance(peer, tuple) and peer:
        return str(peer[0])
    raise UnsafeImageUrl("connected peer address is unavailable")


def _header(response: Any, name: str) -> str:
    getter = getattr(response, "getheader", None)
    if callable(getter):
        return str(getter(name) or "")
    headers = getattr(response, "headers", {}) or {}
    return str(headers.get(name) or headers.get(name.casefold()) or "")


def _validated_mime_type(response: Any) -> str:
    value = _header(response, "Content-Type").split(";", 1)[0]
    mime_type = value.strip().casefold()
    if mime_type not in ALLOWED_IMAGE_MIME_TYPES:
        raise UnsafeImageUrl(
            "remote image Content-Type must be image/png, image/jpeg, or image/webp"
        )
    return mime_type


def _content_length(response: Any) -> int | None:
    raw = _header(response, "Content-Length").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise UnsafeImageUrl("remote image Content-Length must be an integer") from exc
    if value < 0:
        raise UnsafeImageUrl("remote image Content-Length must not be negative")
    return value


def _validate_downloaded_image(data: bytes, mime_type: str) -> None:
    validate_image_payload(
        data,
        expected_format=ALLOWED_IMAGE_MIME_TYPES[mime_type],
    )


def validate_image_payload(data: bytes, *, expected_format: str | None = None) -> None:
    """Fully decode one bounded static image, independent of delivery form."""

    try:
        info = inspect_image_bytes(data, visual_forensics=False)
    except Exception as exc:
        raise UnsafeImageUrl(
            f"remote image decoding failed: {exc.__class__.__name__}: {exc}"
        ) from exc
    if expected_format is not None and info.format != expected_format:
        raise UnsafeImageUrl(
            f"image format mismatch: expected={expected_format}, actual={info.format}"
        )
    try:
        from PIL import Image
    except ImportError as exc:
        raise UnsafeImageUrl(
            "Pillow is required to fully decode remote PNG/JPEG/WebP images"
        ) from exc
    try:
        import io
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with Image.open(io.BytesIO(data)) as image:
                if str(image.format or "").upper() != info.format:
                    raise UnsafeImageUrl("remote image decoder format mismatch")
                _validate_decoder_shape(image, info)
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                _validate_decoder_shape(image, info)
                image.load()
                _validate_decoder_shape(image, info)
    except UnsafeImageUrl:
        raise
    except Exception as exc:
        raise UnsafeImageUrl(
            f"remote image decoding failed: {exc.__class__.__name__}: {exc}"
        ) from exc


def _validate_decoder_shape(image: Any, info: Any) -> None:
    frame_count = getattr(image, "n_frames", 1)
    if (
        isinstance(frame_count, bool)
        or not isinstance(frame_count, int)
        or frame_count != 1
    ):
        raise UnsafeImageUrl("animated images are not allowed")
    width, height = image.size
    if (width, height) != (info.width, info.height):
        raise UnsafeImageUrl("remote image decoder dimensions mismatch")
    if width <= 0 or height <= 0 or max(width, height) > MAX_IMAGE_EDGE:
        raise UnsafeImageUrl("remote image decoder edge limit exceeded")
    pixels = width * height
    if pixels > MAX_IMAGE_PIXELS:
        raise UnsafeImageUrl("remote image decoder pixel limit exceeded")
    bytes_per_channel = (
        4
        if image.mode in {"I", "F"}
        else 2
        if ";16" in str(image.mode)
        else 1
    )
    decoded_bytes = pixels * max(len(image.getbands()), 1) * bytes_per_channel
    if decoded_bytes > MAX_IMAGE_DECODED_BYTES:
        raise UnsafeImageUrl("remote image decoder byte limit exceeded")
