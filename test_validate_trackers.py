import struct
import unittest

from validate_trackers import (
    BencodeError,
    _normalized_url,
    _parse_udp_header,
    bdecode,
    validate_http_payload,
)


class BencodeTests(unittest.TestCase):
    def test_decodes_valid_announce_response(self):
        payload = b"d8:intervali1800e5:peers0:e"
        self.assertEqual(
            bdecode(payload),
            {b"interval": 1800, b"peers": b""},
        )
        self.assertEqual(
            validate_http_payload(payload),
            (True, "valid HTTP announce response"),
        )

    def test_rejects_failure_reason_that_trackerping_accepts(self):
        payload = b"d14:failure reason25:torrent is not registerede"
        success, reason = validate_http_payload(payload)
        self.assertFalse(success)
        self.assertIn("torrent is not registered", reason)

    def test_rejects_dictionary_without_announce_fields(self):
        success, reason = validate_http_payload(b"d4:spam4:eggse")
        self.assertFalse(success)
        self.assertIn("interval", reason)

    def test_rejects_trailing_garbage(self):
        with self.assertRaises(BencodeError):
            bdecode(b"d8:intervali1e5:peers0:ehtml")


class UrlTests(unittest.TestCase):
    def test_rejects_websocket_tracker_for_standard_qbittorrent(self):
        url, reason = _normalized_url("wss://tracker.example/announce")
        self.assertIsNone(url)
        self.assertIn("unsupported", reason)

    def test_rejects_private_literal_address(self):
        url, reason = _normalized_url("http://127.0.0.1:8080/announce")
        self.assertIsNone(url)
        self.assertIn("not a public", reason)

    def test_accepts_udp_url_with_port(self):
        url, reason = _normalized_url("udp://tracker.example:6969/announce")
        self.assertEqual(url, "udp://tracker.example:6969/announce")
        self.assertIsNone(reason)


class UdpTests(unittest.TestCase):
    def test_accepts_announce_header_with_matching_transaction(self):
        transaction = 1234
        payload = struct.pack("!IIIII", 1, transaction, 1800, 3, 5)
        action, error = _parse_udp_header(payload, transaction)
        self.assertEqual(action, 1)
        self.assertIsNone(error)

    def test_rejects_udp_error_response(self):
        transaction = 1234
        payload = struct.pack("!II", 3, transaction) + b"unregistered torrent"
        action, error = _parse_udp_header(payload, transaction)
        self.assertIsNone(action)
        self.assertIn("unregistered torrent", error)


if __name__ == "__main__":
    unittest.main()

