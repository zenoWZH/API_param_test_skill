from __future__ import annotations

import http.client
import socket
import ssl
import struct
import sys
import unittest
import zlib
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from lib.image_url_safety import (
    MAX_REMOTE_IMAGE_ADDRESS_ATTEMPTS,
    MAX_REMOTE_IMAGE_BYTES,
    UnsafeImageUrl,
    _PinnedHTTPSConnection,
    _validate_downloaded_image,
    fetch_remote_image,
)
from lib.image_validation import inspect_image_bytes


PUBLIC_IP = "93.184.216.34"
OTHER_PUBLIC_IP = "93.184.216.35"


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _resolver_for(address: str):
    def resolve(host: str, port: int, *, type: int):
        del host
        return [(socket.AF_INET, type, socket.IPPROTO_TCP, "", (address, port))]

    return resolve


def _png(width: int, height: int) -> bytes:
    signature = bytes.fromhex("89504e470d0a1a0a")
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = bytes([0]) + bytes([32, 96, 160]) * width
    raw_pixels = row * height
    return b"".join(
        [
            signature,
            _png_chunk(b"IHDR", ihdr),
            _png_chunk(b"IDAT", zlib.compress(raw_pixels)),
            _png_chunk(b"IEND", b""),
        ]
    )


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


class FakeResponse:
    def __init__(
        self,
        data: bytes,
        *,
        status_code: int = 200,
        content_type: str = "image/png",
        content_length: str | None = None,
    ) -> None:
        self._data = data
        self._offset = 0
        self.status = status_code
        self.headers = {"content-type": content_type}
        if content_length is not None:
            self.headers["content-length"] = content_length
        self.closed = False

    def getheader(self, name: str) -> str | None:
        return self.headers.get(name.casefold())

    def read(self, size: int) -> bytes:
        result = self._data[self._offset : self._offset + size]
        self._offset += len(result)
        return result

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, response: FakeResponse, peer_ip: str | None = PUBLIC_IP) -> None:
        self.response = response
        self.peer_ip = peer_ip
        self.closed = False
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, target: str, *, headers: dict) -> None:
        self.calls.append((method, target, headers))

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class ImageUrlSafetyTest(unittest.TestCase):
    def test_public_png_fetch_is_streamed_without_auth_redirects_or_proxy_env(self) -> None:
        raw = _png(8, 8)
        response = FakeResponse(raw)
        connection = FakeConnection(response)
        factory = Mock(return_value=connection)

        with patch("lib.image_url_safety._validate_downloaded_image") as decoder:
            fetched = fetch_remote_image(
                "https://images.example/output.png?token=opaque",
                300,
                resolver=_resolver_for(PUBLIC_IP),
                connection_factory=factory,
            )

        self.assertEqual(fetched.data, raw)
        self.assertEqual(fetched.mime_type, "image/png")
        self.assertEqual(fetched.peer_ip, PUBLIC_IP)
        decoder.assert_called_once_with(raw, "image/png")
        scheme, host, port, pinned_ip, attempt_timeout = factory.call_args.args
        self.assertEqual((scheme, host, port, pinned_ip), (
            "https", "images.example", 443, PUBLIC_IP
        ))
        self.assertGreater(attempt_timeout, 0)
        self.assertLessEqual(attempt_timeout, 60)
        self.assertTrue(connection.closed)
        self.assertTrue(response.closed)
        method, target, headers = connection.calls[0]
        self.assertEqual((method, target), ("GET", "/output.png?token=opaque"))
        self.assertNotIn("Authorization", headers)
        self.assertIn("image/png", headers["Accept"])

    def test_non_http_userinfo_local_and_private_targets_fail_before_fetch(self) -> None:
        cases = (
            ("file:///etc/passwd", _resolver_for(PUBLIC_IP)),
            ("ftp://images.example/a.png", _resolver_for(PUBLIC_IP)),
            ("https://user:secret@images.example/a.png", _resolver_for(PUBLIC_IP)),
            ("http://images.example:0/a.png", _resolver_for(PUBLIC_IP)),
            ("https://images.example:/a.png", _resolver_for(PUBLIC_IP)),
            ("http://localhost/a.png", _resolver_for(PUBLIC_IP)),
            ("http://127.0.0.1/a.png", _resolver_for("127.0.0.1")),
            ("http://169.254.169.254/latest/meta-data", _resolver_for("169.254.169.254")),
            ("http://10.0.0.7/a.png", _resolver_for("10.0.0.7")),
            ("http://[::1]/a.png", _resolver_for("::1")),
            ("http://multicast.example/a.png", _resolver_for("224.0.0.1")),
            ("http://multicast-v6.example/a.png", _resolver_for("ff02::1")),
            ("http://site-local-v6.example/a.png", _resolver_for("fec0::1")),
        )
        for url, resolver in cases:
            connection_factory = Mock()
            with self.subTest(url=url), self.assertRaises(UnsafeImageUrl):
                fetch_remote_image(
                    url,
                    10,
                    resolver=resolver,
                    connection_factory=connection_factory,
                )
            connection_factory.assert_not_called()

    def test_redirects_are_rejected_instead_of_followed(self) -> None:
        response = FakeResponse(b"", status_code=302)
        connection = FakeConnection(response)
        with self.assertRaisesRegex(UnsafeImageUrl, "redirect"):
            fetch_remote_image(
                "https://images.example/a.png",
                10,
                resolver=_resolver_for(PUBLIC_IP),
                connection_factory=lambda *_args: connection,  # type: ignore[arg-type]
            )
        self.assertEqual(len(connection.calls), 1)

    def test_dns_is_resolved_once_and_only_the_approved_ip_reaches_transport(self) -> None:
        resolver = Mock(side_effect=_resolver_for(PUBLIC_IP))
        response = FakeResponse(_png(2, 2))
        connection = FakeConnection(response)
        factory = Mock(return_value=connection)
        with patch("lib.image_url_safety._validate_downloaded_image"):
            fetch_remote_image(
                "https://images.example/a.png",
                10,
                resolver=resolver,
                connection_factory=factory,
            )
        resolver.assert_called_once_with(
            "images.example", 443, type=socket.SOCK_STREAM
        )
        scheme, host, port, pinned_ip, attempt_timeout = factory.call_args.args
        self.assertEqual((scheme, host, port, pinned_ip), (
            "https", "images.example", 443, PUBLIC_IP
        ))
        self.assertGreater(attempt_timeout, 0)
        self.assertLessEqual(attempt_timeout, 10)

    def test_transport_falls_back_across_one_pinned_dns_result_set(self) -> None:
        ipv6 = "2606:2800:220:1:248:1893:25c8:1946"
        resolver = Mock(
            return_value=[
                (
                    socket.AF_INET6,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (ipv6, 443, 0, 0),
                ),
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (PUBLIC_IP, 443),
                ),
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (PUBLIC_IP, 443),
                ),
            ]
        )
        response = FakeResponse(_png(2, 2))
        connection = FakeConnection(response, peer_ip=PUBLIC_IP)
        attempts: list[str] = []

        def factory(
            scheme: str,
            host: str,
            port: int,
            pinned_ip: str,
            timeout: int,
        ) -> FakeConnection:
            del scheme, host, port, timeout
            attempts.append(pinned_ip)
            if pinned_ip == ipv6:
                raise OSError("offline IPv6 route unavailable")
            return connection

        with patch("lib.image_url_safety._validate_downloaded_image"):
            fetched = fetch_remote_image(
                "https://images.example/a.png",
                10,
                resolver=resolver,
                connection_factory=factory,
            )

        self.assertEqual(fetched.peer_ip, PUBLIC_IP)
        self.assertEqual(attempts, [ipv6, PUBLIC_IP])
        resolver.assert_called_once_with(
            "images.example", 443, type=socket.SOCK_STREAM
        )
        self.assertTrue(connection.closed)
        self.assertTrue(response.closed)

    def test_fallback_addresses_share_one_deadline_including_dns_time(self) -> None:
        addresses = [PUBLIC_IP, OTHER_PUBLIC_IP, "93.184.216.36"]
        clock = FakeClock()

        def resolver(host: str, port: int, *, type: int):
            del host
            clock.advance(2)
            return [
                (socket.AF_INET, type, socket.IPPROTO_TCP, "", (address, port))
                for address in addresses
            ]

        attempts: list[tuple[str, float]] = []

        def factory(
            scheme: str,
            host: str,
            port: int,
            pinned_ip: str,
            timeout: float,
        ) -> FakeConnection:
            del scheme, host, port
            attempts.append((pinned_ip, timeout))
            clock.advance(5 if len(attempts) == 1 else 3)
            raise OSError("offline route unavailable")

        with self.assertRaisesRegex(UnsafeImageUrl, "fetch failed"):
            fetch_remote_image(
                "https://images.example/a.png",
                10,
                resolver=resolver,
                connection_factory=factory,
                monotonic=clock,
            )

        self.assertEqual([address for address, _ in attempts], addresses[:2])
        self.assertEqual([timeout for _, timeout in attempts], [8, 3])
        self.assertEqual(clock(), 110)

    def test_fallback_attempt_count_is_capped(self) -> None:
        addresses = [f"93.184.216.{value}" for value in range(34, 40)]

        def resolver(host: str, port: int, *, type: int):
            del host
            return [
                (socket.AF_INET, type, socket.IPPROTO_TCP, "", (address, port))
                for address in addresses
            ]

        attempts: list[str] = []

        def factory(
            scheme: str,
            host: str,
            port: int,
            pinned_ip: str,
            timeout: float,
        ) -> FakeConnection:
            del scheme, host, port, timeout
            attempts.append(pinned_ip)
            raise OSError("offline route unavailable")

        with self.assertRaisesRegex(UnsafeImageUrl, "fetch failed"):
            fetch_remote_image(
                "https://images.example/a.png",
                10,
                resolver=resolver,
                connection_factory=factory,
                monotonic=FakeClock(),
            )

        self.assertEqual(
            attempts,
            addresses[:MAX_REMOTE_IMAGE_ADDRESS_ATTEMPTS],
        )

    def test_mismatched_or_unobservable_pinned_peer_fails_closed(self) -> None:
        for peer_ip in ("10.0.0.9", OTHER_PUBLIC_IP, None):
            response = FakeResponse(_png(2, 2))
            connection = FakeConnection(response, peer_ip=peer_ip)
            with self.subTest(peer_ip=peer_ip), self.assertRaises(UnsafeImageUrl):
                fetch_remote_image(
                    "https://images.example/a.png",
                    10,
                    resolver=_resolver_for(PUBLIC_IP),
                    connection_factory=lambda *_args: connection,  # type: ignore[arg-type]
                )

    def test_https_pinned_transport_connects_to_ip_but_verifies_original_hostname(self) -> None:
        raw_socket = Mock()
        tls_socket = Mock()
        context = Mock()
        context.wrap_socket.return_value = tls_socket
        connection = _PinnedHTTPSConnection(
            "images.example",
            443,
            pinned_ip=PUBLIC_IP,
            timeout=12,
            context=context,
        )
        with patch("lib.image_url_safety.socket.create_connection", return_value=raw_socket) as connect:
            connection.connect()
        connect.assert_called_once_with((PUBLIC_IP, 443), 12, None)
        context.wrap_socket.assert_called_once_with(
            raw_socket, server_hostname="images.example"
        )
        self.assertIs(connection.sock, tls_socket)

    def test_https_pinned_transport_closes_raw_socket_when_tls_fails(self) -> None:
        raw_socket = Mock()
        context = Mock()
        context.wrap_socket.side_effect = ssl.SSLError("bad certificate")
        connection = _PinnedHTTPSConnection(
            "images.example",
            443,
            pinned_ip=PUBLIC_IP,
            timeout=12,
            context=context,
        )
        with patch(
            "lib.image_url_safety.socket.create_connection",
            return_value=raw_socket,
        ), self.assertRaises(ssl.SSLError):
            connection.connect()
        raw_socket.close.assert_called_once_with()

    def test_mime_length_stream_and_signature_policies_fail_closed(self) -> None:
        cases = (
            FakeResponse(_png(2, 2), content_type="text/plain"),
            FakeResponse(
                b"",
                content_length=str(MAX_REMOTE_IMAGE_BYTES + 1),
            ),
            FakeResponse(b"not an image", content_type="image/png"),
            FakeResponse(_png(2, 2), content_type="image/jpeg"),
        )
        for response in cases:
            connection = FakeConnection(response)
            with self.subTest(headers=response.headers), self.assertRaises(UnsafeImageUrl):
                fetch_remote_image(
                    "https://images.example/a.png",
                    10,
                    resolver=_resolver_for(PUBLIC_IP),
                    connection_factory=lambda *_args: connection,  # type: ignore[arg-type]
                )

        response = FakeResponse(b"12345")
        connection = FakeConnection(response)
        with patch("lib.image_url_safety.MAX_REMOTE_IMAGE_BYTES", 4), self.assertRaisesRegex(
            UnsafeImageUrl, "byte limit"
        ):
            fetch_remote_image(
                "https://images.example/a.png",
                10,
                resolver=_resolver_for(PUBLIC_IP),
                connection_factory=lambda *_args: connection,  # type: ignore[arg-type]
            )

    def test_decoder_rejects_pixel_and_decompressed_size_bombs(self) -> None:
        oversized_ihdr = struct.pack(">IIBBBBB", 20_000, 1, 8, 2, 0, 0, 0)
        oversized = bytes.fromhex("89504e470d0a1a0a") + _png_chunk(
            b"IHDR", oversized_ihdr
        )
        with self.assertRaisesRegex(ValueError, "edge exceeds"):
            inspect_image_bytes(oversized, visual_forensics=False)

        decoded_bomb_ihdr = struct.pack(">IIBBBBB", 8_000, 5_000, 16, 6, 0, 0, 0)
        decoded_bomb = b"".join(
            [
                bytes.fromhex("89504e470d0a1a0a"),
                _png_chunk(b"IHDR", decoded_bomb_ihdr),
                _png_chunk(b"IDAT", zlib.compress(b"x")),
                _png_chunk(b"IEND", b""),
            ]
        )
        with self.assertRaisesRegex(ValueError, "decoded payload exceeds"):
            inspect_image_bytes(decoded_bomb, visual_forensics=False)

        truncated = _png(8, 8)[:-5]
        with self.assertRaisesRegex(ValueError, "PNG must contain|truncated"):
            inspect_image_bytes(truncated, visual_forensics=False)

        truncated_ihdr = b"".join(
            [
                bytes.fromhex("89504e470d0a1a0a"),
                struct.pack(">I", 13),
                b"IHDR",
                struct.pack(">II", 1, 1),
                bytes([8, 2]),
            ]
        )
        with self.assertRaisesRegex(UnsafeImageUrl, "remote image decoding failed"):
            _validate_downloaded_image(truncated_ihdr, "image/png")

    def test_header_parser_exceptions_are_normalized_as_unsafe_urls(self) -> None:
        with patch(
            "lib.image_url_safety.inspect_image_bytes",
            side_effect=IndexError("offline parser failure"),
        ), self.assertRaisesRegex(UnsafeImageUrl, "IndexError"):
            _validate_downloaded_image(b"offline-fixture", "image/png")

    def test_fetch_exception_does_not_echo_signed_query_material(self) -> None:
        secret = "OFFLINE_DUMMY_SIGNED_QUERY"
        connection = FakeConnection(FakeResponse(b""))
        connection.request = Mock(
            side_effect=http.client.InvalidURL(f"invalid /x?token={secret} value")
        )
        with self.assertRaises(UnsafeImageUrl) as raised:
            fetch_remote_image(
                f"http://images.example/x?token={secret}%20value",
                10,
                resolver=_resolver_for(PUBLIC_IP),
                connection_factory=lambda *_args: connection,
            )
        self.assertNotIn(secret, str(raised.exception))
        self.assertIn("InvalidURL", str(raised.exception))

    def test_animated_images_are_rejected_before_frame_decode(self) -> None:
        animated = MagicMock()
        animated.__enter__.return_value = animated
        animated.format = "PNG"
        animated.n_frames = 2
        animated.size = (4, 3)
        animated.mode = "RGB"
        animated.getbands.return_value = ("R", "G", "B")
        image_module = SimpleNamespace(open=Mock(return_value=animated))
        pil_module = ModuleType("PIL")
        pil_module.Image = image_module  # type: ignore[attr-defined]
        info = SimpleNamespace(format="PNG", width=4, height=3)
        with patch.dict(sys.modules, {"PIL": pil_module}), patch(
            "lib.image_url_safety.inspect_image_bytes", return_value=info
        ), self.assertRaisesRegex(UnsafeImageUrl, "animated"):
            _validate_downloaded_image(b"offline-fixture", "image/png")
        animated.verify.assert_not_called()

    def test_every_allowed_format_is_verified_and_fully_loaded(self) -> None:
        for mime_type, image_format in (
            ("image/png", "PNG"),
            ("image/jpeg", "JPEG"),
            ("image/webp", "WEBP"),
        ):
            verify_image = MagicMock()
            verify_image.__enter__.return_value = verify_image
            verify_image.format = image_format
            verify_image.size = (4, 3)
            verify_image.mode = "RGB"
            verify_image.n_frames = 1
            verify_image.getbands.return_value = ("R", "G", "B")
            load_image = MagicMock()
            load_image.__enter__.return_value = load_image
            load_image.format = image_format
            load_image.size = (4, 3)
            load_image.mode = "RGB"
            load_image.n_frames = 1
            load_image.getbands.return_value = ("R", "G", "B")
            image_module = SimpleNamespace(
                open=Mock(side_effect=[verify_image, load_image])
            )
            pil_module = ModuleType("PIL")
            pil_module.Image = image_module  # type: ignore[attr-defined]
            info = SimpleNamespace(format=image_format, width=4, height=3)
            with self.subTest(mime_type=mime_type), patch.dict(
                sys.modules, {"PIL": pil_module}
            ), patch(
                "lib.image_url_safety.inspect_image_bytes",
                return_value=info,
            ):
                _validate_downloaded_image(b"offline-fixture", mime_type)
            verify_image.verify.assert_called_once_with()
            load_image.load.assert_called_once_with()
            self.assertEqual(image_module.open.call_count, 2)
