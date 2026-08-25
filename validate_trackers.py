#!/usr/bin/env python3
"""Strict BitTorrent tracker validation for qBittorrent tracker lists.

Unlike a basic connectivity check, this module validates the tracker protocol
response. HTTP(S) replies are fully bdecoded and UDP trackers must complete an
announce exchange after the initial connection handshake.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass
import hashlib
import ipaddress
from pathlib import Path
import random
import socket
import struct
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import aiohttp


DEFAULT_TIMEOUT = 10.0
DEFAULT_CONCURRENCY = 32
DEFAULT_PASSES = 2
MAX_HTTP_RESPONSE = 1_048_576
UDP_PROTOCOL_ID = 0x41727101980
INFO_HASH = hashlib.sha1(b"qbittorrent-tracker-health-check").digest()
PEER_ID = b"-qB5000-" + random.randbytes(12)


@dataclass(frozen=True)
class ValidationResult:
    url: str
    success: bool
    reason: str


class BencodeError(ValueError):
    pass


def bdecode(payload: bytes) -> Any:
    """Decode one complete bencoded value and reject trailing garbage."""

    position = 0

    def parse() -> Any:
        nonlocal position
        if position >= len(payload):
            raise BencodeError("unexpected end of response")

        marker = payload[position]

        if marker == ord("i"):
            position += 1
            end = payload.find(b"e", position)
            if end == -1:
                raise BencodeError("unterminated integer")
            raw = payload[position:end]
            if not raw or raw == b"-0" or (raw.startswith(b"0") and raw != b"0"):
                raise BencodeError("invalid integer")
            try:
                value = int(raw)
            except ValueError as exc:
                raise BencodeError("invalid integer") from exc
            position = end + 1
            return value

        if marker == ord("l"):
            position += 1
            values = []
            while position < len(payload) and payload[position] != ord("e"):
                values.append(parse())
            if position >= len(payload):
                raise BencodeError("unterminated list")
            position += 1
            return values

        if marker == ord("d"):
            position += 1
            values: dict[bytes, Any] = {}
            while position < len(payload) and payload[position] != ord("e"):
                key = parse()
                if not isinstance(key, bytes):
                    raise BencodeError("dictionary key is not a byte string")
                values[key] = parse()
            if position >= len(payload):
                raise BencodeError("unterminated dictionary")
            position += 1
            return values

        if ord("0") <= marker <= ord("9"):
            colon = payload.find(b":", position)
            if colon == -1:
                raise BencodeError("missing byte-string separator")
            try:
                length = int(payload[position:colon])
            except ValueError as exc:
                raise BencodeError("invalid byte-string length") from exc
            if length < 0:
                raise BencodeError("negative byte-string length")
            position = colon + 1
            end = position + length
            if end > len(payload):
                raise BencodeError("truncated byte string")
            value = payload[position:end]
            position = end
            return value

        raise BencodeError(f"unexpected marker 0x{marker:02x}")

    value = parse()
    if position != len(payload):
        raise BencodeError("trailing data after bencoded response")
    return value


def _display_bytes(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def validate_http_payload(payload: bytes) -> tuple[bool, str]:
    """Validate the body of a BEP 3 HTTP tracker announce response."""

    try:
        response = bdecode(payload)
    except BencodeError as exc:
        return False, f"invalid bencoded response: {exc}"

    if not isinstance(response, dict):
        return False, "announce response is not a dictionary"

    if b"failure reason" in response:
        return False, f"tracker rejected announce: {_display_bytes(response[b'failure reason'])}"

    interval = response.get(b"interval")
    if not isinstance(interval, int) or interval <= 0:
        return False, "announce response has no valid interval"

    if b"peers" not in response and b"peers6" not in response:
        return False, "announce response has no peers or peers6 field"

    return True, "valid HTTP announce response"


def _normalized_url(raw_url: str) -> tuple[str | None, str | None]:
    try:
        parts = urlsplit(raw_url.strip())
        _ = parts.port
    except ValueError as exc:
        return None, f"invalid URL: {exc}"

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https", "udp"}:
        return None, f"unsupported by standard qBittorrent tracker mode: {scheme or 'missing scheme'}"
    if not parts.hostname:
        return None, "URL has no hostname"
    if scheme == "udp" and parts.port is None:
        return None, "UDP tracker URL has no port"

    try:
        literal_ip = ipaddress.ip_address(parts.hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not literal_ip.is_global:
        return None, "tracker address is not a public Internet address"

    normalized = urlunsplit((scheme, parts.netloc, parts.path or "/announce", parts.query, ""))
    return normalized, None


def _http_announce_url(url: str) -> str:
    params = urlencode(
        {
            "info_hash": INFO_HASH,
            "peer_id": PEER_ID,
            "port": random.randint(10000, 60000),
            "uploaded": 0,
            "downloaded": 0,
            "left": 1,
            "compact": 1,
            "no_peer_id": 1,
            "event": "stopped",
            "numwant": 1,
            "key": f"{random.getrandbits(32):08x}",
        }
    )
    return f"{url}{'&' if '?' in url else '?'}{params}"


async def validate_http(
    session: aiohttp.ClientSession, url: str, timeout: float
) -> tuple[bool, str]:
    try:
        async with session.get(
            _http_announce_url(url),
            allow_redirects=True,
            max_redirects=3,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            if not 200 <= response.status < 300:
                return False, f"HTTP {response.status}"
            payload = await response.content.read(MAX_HTTP_RESPONSE + 1)
    except asyncio.TimeoutError:
        return False, "HTTP announce timed out"
    except aiohttp.TooManyRedirects:
        return False, "too many HTTP redirects"
    except aiohttp.ClientError as exc:
        return False, f"HTTP connection failed: {exc}"

    if len(payload) > MAX_HTTP_RESPONSE:
        return False, "HTTP announce response is unexpectedly large"
    return validate_http_payload(payload)


def _parse_udp_header(payload: bytes, expected_transaction: int) -> tuple[int | None, str | None]:
    if len(payload) < 8:
        return None, "truncated UDP response"
    action, transaction_id = struct.unpack_from("!II", payload)
    if transaction_id != expected_transaction:
        return None, "UDP transaction ID mismatch"
    if action == 3:
        message = payload[8:].decode("utf-8", errors="replace") or "unspecified error"
        return None, f"tracker rejected announce: {message}"
    return action, None


def _validate_udp_blocking(url: str, timeout: float) -> tuple[bool, str]:
    parts = urlsplit(url)
    assert parts.hostname and parts.port
    addresses = socket.getaddrinfo(parts.hostname, parts.port, type=socket.SOCK_DGRAM)
    last_error = "no usable UDP address"

    for family, socktype, protocol, _, address in addresses:
        try:
            with socket.socket(family, socktype, protocol) as sock:
                sock.settimeout(timeout)
                sock.connect(address)

                connect_transaction = random.getrandbits(32)
                sock.send(struct.pack("!QII", UDP_PROTOCOL_ID, 0, connect_transaction))
                connect_response = sock.recv(2048)
                action, error = _parse_udp_header(connect_response, connect_transaction)
                if error:
                    last_error = error
                    continue
                if action != 0 or len(connect_response) < 16:
                    last_error = "invalid UDP connect response"
                    continue
                connection_id = struct.unpack_from("!Q", connect_response, 8)[0]

                announce_transaction = random.getrandbits(32)
                announce_request = struct.pack(
                    "!QII20s20sQQQIIIiH",
                    connection_id,
                    1,
                    announce_transaction,
                    INFO_HASH,
                    PEER_ID,
                    0,
                    1,
                    0,
                    3,
                    0,
                    random.getrandbits(32),
                    1,
                    random.randint(10000, 60000),
                )
                sock.send(announce_request)
                announce_response = sock.recv(65536)
                action, error = _parse_udp_header(announce_response, announce_transaction)
                if error:
                    last_error = error
                    continue
                if action != 1 or len(announce_response) < 20:
                    last_error = "invalid UDP announce response"
                    continue
                return True, "valid UDP announce response"
        except (OSError, socket.timeout) as exc:
            last_error = f"UDP announce failed: {exc}"

    return False, last_error


async def validate_udp(url: str, timeout: float) -> tuple[bool, str]:
    try:
        return await asyncio.to_thread(_validate_udp_blocking, url, timeout)
    except socket.gaierror as exc:
        return False, f"DNS lookup failed: {exc}"
    except OSError as exc:
        return False, f"UDP connection failed: {exc}"


async def validate_one(
    raw_url: str,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    timeout: float,
    passes: int,
) -> ValidationResult:
    url, error = _normalized_url(raw_url)
    if error or url is None:
        return ValidationResult(raw_url, False, error or "invalid URL")

    async with semaphore:
        for pass_number in range(1, passes + 1):
            if urlsplit(url).scheme in {"http", "https"}:
                success, reason = await validate_http(session, url, timeout)
            else:
                success, reason = await validate_udp(url, timeout)
            if not success:
                return ValidationResult(raw_url, False, f"check {pass_number}/{passes}: {reason}")

    return ValidationResult(raw_url, True, f"passed {passes}/{passes} protocol checks")


def _read_trackers(path: Path) -> list[str]:
    seen: set[str] = set()
    trackers = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#") and value not in seen:
            trackers.append(value)
            seen.add(value)
    return trackers


def _write_trackers(path: Path, trackers: list[str]) -> None:
    path.write_text("\n\n".join(trackers) + ("\n" if trackers else ""), encoding="utf-8")


async def run(args: argparse.Namespace) -> int:
    trackers = _read_trackers(args.input)
    print(f"Validating {len(trackers)} unique tracker URLs")
    print(
        f"Policy: {args.passes} required protocol checks, "
        f"{args.timeout:g}s timeout, {args.concurrency} concurrent trackers"
    )

    headers = {
        "User-Agent": "qBittorrent/5.0.0",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    connector = aiohttp.TCPConnector(limit=args.concurrency, ttl_dns_cache=300)
    semaphore = asyncio.Semaphore(args.concurrency)
    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        results = await asyncio.gather(
            *(
                validate_one(
                    tracker,
                    session,
                    semaphore,
                    args.timeout,
                    args.passes,
                )
                for tracker in trackers
            )
        )

    accepted = [result.url for result in results if result.success]
    rejected = [result for result in results if not result.success]
    _write_trackers(args.output, accepted)
    args.rejected.write_text(
        "\n".join(f"{result.url}\t{result.reason}" for result in rejected)
        + ("\n" if rejected else ""),
        encoding="utf-8",
    )

    for result in rejected:
        print(f"REJECTED {result.url}\n  {result.reason}")

    schemes = Counter(urlsplit(url).scheme.lower() for url in accepted)
    print("\n--- strict validation statistics ---")
    print(f"{len(trackers)} total, {len(accepted)} accepted, {len(rejected)} rejected")
    print("Accepted by protocol: " + ", ".join(f"{key}={value}" for key, value in sorted(schemes.items())))
    print(f"Rejection details written to {args.rejected}")

    if not accepted:
        print("ERROR: strict validation rejected every tracker")
        return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Strictly validate public BitTorrent trackers")
    parser.add_argument("input", type=Path, help="input tracker list")
    parser.add_argument("output", type=Path, help="output list containing only accepted trackers")
    parser.add_argument(
        "--rejected",
        type=Path,
        default=Path("rejected_trackers.txt"),
        help="diagnostic output containing rejected trackers and reasons",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--passes", type=int, default=DEFAULT_PASSES)
    args = parser.parse_args()
    if args.timeout <= 0 or args.concurrency <= 0 or args.passes <= 0:
        parser.error("timeout, concurrency, and passes must all be positive")
    return args


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

