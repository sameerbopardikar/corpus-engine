import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_live_fetch import (
    LiveFetchError,
    SizeCapExceeded,
    enforce_declared_length,
    read_capped,
    live_fetch,
)
from corpus_ssrf import SSRFError


class _CountingReader:
    """A reader that records exactly how many bytes were pulled from it."""

    def __init__(self, total_bytes, chunk=1024):
        self._remaining = total_bytes
        self._chunk = chunk
        self.consumed = 0

    def read(self, n):
        give = min(n, self._chunk, self._remaining)
        if give <= 0:
            return b""
        self._remaining -= give
        self.consumed += give
        return b"x" * give


class ReadCappedTests(unittest.TestCase):
    def test_returns_full_body_when_under_cap(self):
        body = read_capped(io.BytesIO(b"hello world"), max_bytes=1000)
        self.assertEqual(body, b"hello world")

    def test_raises_before_consuming_full_oversized_body(self):
        reader = _CountingReader(total_bytes=10_000_000, chunk=64 * 1024)
        with self.assertRaises(SizeCapExceeded):
            read_capped(reader, max_bytes=1_000_000)
        # The read must stop shortly after crossing the cap, never draining the
        # whole 10 MB body.
        self.assertLess(reader.consumed, 1_000_000 + 2 * 64 * 1024)

    def test_exact_cap_is_allowed(self):
        body = read_capped(io.BytesIO(b"a" * 1000), max_bytes=1000)
        self.assertEqual(len(body), 1000)


class DeclaredLengthTests(unittest.TestCase):
    def test_oversized_declared_length_rejected(self):
        with self.assertRaises(SizeCapExceeded):
            enforce_declared_length("2000000", max_bytes=1_000_000)

    def test_absent_or_bad_length_is_tolerated(self):
        enforce_declared_length(None, max_bytes=1000)
        enforce_declared_length("not-a-number", max_bytes=1000)

    def test_within_length_ok(self):
        enforce_declared_length("500", max_bytes=1000)


class _RedirectResponse:
    """A redirect response whose body must never be drained."""

    def __init__(self, status, location):
        self.status = status
        self._location = location
        self.read_called = False

    def getheader(self, name, default=None):
        if name == "Location":
            return self._location
        return default

    def read(self, *args, **kwargs):
        # If live_fetch drains the redirect body, this fires and fails the test.
        self.read_called = True
        raise AssertionError("redirect body must not be read/drained")


class _FinalResponse:
    def __init__(self, body, content_type="text/html"):
        self.status = 200
        self._body = body
        self._served = False
        self._content_type = content_type

    def getheader(self, name, default=None):
        if name == "Content-Type":
            return self._content_type
        if name == "Content-Length":
            return str(len(self._body))
        return default

    def read(self, n=-1):
        if self._served:
            return b""
        self._served = True
        return self._body


class _FakeConn:
    def __init__(self, response):
        self._response = response
        self.closed = 0

    def request(self, *args, **kwargs):
        pass

    def getresponse(self):
        return self._response

    def close(self):
        self.closed += 1


class RedirectBodyTests(unittest.TestCase):
    def test_redirect_body_is_not_consumed(self):
        redirect = _RedirectResponse(302, "https://final.example.net/page")
        final = _FinalResponse(b"<html><body>ok</body></html>")
        conns = [_FakeConn(redirect), _FakeConn(final)]

        def fake_open(scheme, host, port, timeout):
            return conns.pop(0)

        import corpus_live_fetch
        original = corpus_live_fetch._open_pinned
        corpus_live_fetch._open_pinned = fake_open
        try:
            result = live_fetch("https://start.example.org/redirect", timeout=1.0, max_bytes=1_000_000)
        finally:
            corpus_live_fetch._open_pinned = original

        # The malicious/unbounded redirect body was never read.
        self.assertFalse(redirect.read_called)
        # The connection carrying the redirect was closed (freed, not drained).
        self.assertEqual(result.final_url, "https://final.example.net/page")
        self.assertEqual(result.body, b"<html><body>ok</body></html>")
        self.assertEqual(result.redirect_chain, ("https://start.example.org/redirect",))


class LiveFetchGuardTests(unittest.TestCase):
    def test_private_target_raises_before_any_socket(self):
        with self.assertRaises(SSRFError):
            live_fetch("http://169.254.169.254/latest/meta-data/", timeout=1.0, max_bytes=1000)

    def test_localhost_target_raises(self):
        with self.assertRaises(SSRFError):
            live_fetch("http://localhost:8080/", timeout=1.0, max_bytes=1000)

    def test_non_http_scheme_raises(self):
        with self.assertRaises(LiveFetchError):
            live_fetch("file:///etc/passwd", timeout=1.0, max_bytes=1000)


if __name__ == "__main__":
    unittest.main()
