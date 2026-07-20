import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_ssrf import (
    SSRFError,
    _unsafe_ip_reason,
    assert_safe_resolved_ip,
    is_safe_locator,
    locator_reason,
)


class LocatorSchemeTests(unittest.TestCase):
    def test_https_public_host_is_safe(self):
        self.assertTrue(is_safe_locator("https://api.openalex.org/works"))
        self.assertTrue(is_safe_locator("http://example.org/vbt"))

    def test_non_http_scheme_rejected(self):
        self.assertFalse(is_safe_locator("ftp://example.org/x"))
        self.assertFalse(is_safe_locator("file:///etc/passwd"))
        self.assertFalse(is_safe_locator("gopher://example.org/"))

    def test_credentials_rejected(self):
        self.assertFalse(is_safe_locator("https://user:pass@example.org/x"))
        self.assertFalse(is_safe_locator("https://user@example.org/x"))

    def test_missing_host_rejected(self):
        self.assertFalse(is_safe_locator("http:///no-host"))
        self.assertFalse(is_safe_locator(""))
        self.assertFalse(is_safe_locator(None))


class LiteralIpTests(unittest.TestCase):
    def test_loopback_v4_rejected(self):
        self.assertFalse(is_safe_locator("http://127.0.0.1/"))
        self.assertFalse(is_safe_locator("http://127.1/"))

    def test_loopback_v6_rejected(self):
        self.assertFalse(is_safe_locator("http://[::1]/"))

    def test_private_ranges_rejected(self):
        for host in ("10.0.0.5", "192.168.1.1", "172.16.0.1"):
            self.assertFalse(is_safe_locator(f"http://{host}/"), host)

    def test_shared_cgnat_range_rejected(self):
        for host in ("100.64.0.1", "100.127.255.254"):
            self.assertFalse(is_safe_locator(f"http://{host}/"), host)

    def test_link_local_and_metadata_rejected(self):
        self.assertFalse(is_safe_locator("http://169.254.169.254/latest/meta-data/"))
        self.assertFalse(is_safe_locator("http://[fe80::1]/"))

    def test_unique_local_v6_rejected(self):
        self.assertFalse(is_safe_locator("http://[fd00::1]/"))

    def test_unspecified_and_reserved_rejected(self):
        self.assertFalse(is_safe_locator("http://0.0.0.0/"))
        self.assertFalse(is_safe_locator("http://[::]/"))

    def test_encoded_alternate_ipv4_forms_rejected(self):
        # Decimal, hex, and octal encodings of 127.0.0.1 that socket accepts.
        for host in ("2130706433", "0x7f000001", "0177.0.0.1", "0x7f.0.0.1"):
            self.assertFalse(is_safe_locator(f"http://{host}/"), host)

    def test_ipv4_mapped_v6_loopback_rejected(self):
        self.assertFalse(is_safe_locator("http://[::ffff:127.0.0.1]/"))

    def test_public_ip_is_safe(self):
        # 93.184.216.34 (example.com) is public global unicast.
        self.assertTrue(is_safe_locator("http://93.184.216.34/"))


class _FakeAddress:
    """An address whose named category flags are all clear but which is NOT
    global unicast — exactly the shape a denylist of named flags misses.

    This models a special-use range the stdlib does not (yet) flag as
    private/reserved/etc. A fail-closed guard must reject it on the basis that it
    is not global unicast, without depending on any named category matching.
    """

    is_loopback = False
    is_private = False
    is_link_local = False
    is_multicast = False
    is_reserved = False
    is_unspecified = False
    is_site_local = False
    is_global = False

    def __str__(self) -> str:  # pragma: no cover - trivial
        return "203.0.113.7(simulated-non-global)"


class FailClosedByConstructionTests(unittest.TestCase):
    def test_non_global_address_rejected_even_when_no_named_flag_matches(self):
        # Red without the is_global backstop: the denylist would return None
        # (allow) because every named category flag is clear. A fail-closed guard
        # must still reject it because it is not global unicast.
        reason = _unsafe_ip_reason(_FakeAddress())
        self.assertIsInstance(reason, str)
        self.assertTrue(reason)

    def test_curated_non_global_ranges_all_rejected(self):
        # Regression lock over representative non-global literals across v4/v6,
        # including CGNAT, benchmarking, documentation, and 6to4/mapped wrappers.
        non_global = (
            "0.0.0.0", "0.1.2.3", "10.0.0.1", "127.0.0.1", "169.254.169.254",
            "172.16.0.1", "192.168.1.1", "100.64.0.1", "100.127.255.254",
            "192.0.2.1", "198.51.100.1", "203.0.113.1", "198.18.0.1",
            "240.0.0.1", "255.255.255.255", "[::]", "[::1]", "[fe80::1]",
            "[fc00::1]", "[2001:db8::1]", "[::ffff:127.0.0.1]",
            "[::ffff:100.64.0.1]", "[2002:6440:0001::1]",
        )
        for host in non_global:
            with self.subTest(host=host):
                self.assertFalse(is_safe_locator(f"http://{host}/"), host)

    def test_global_unicast_still_allowed(self):
        for host in ("93.184.216.34", "8.8.8.8", "[2001:4860:4860::8888]"):
            with self.subTest(host=host):
                self.assertTrue(is_safe_locator(f"http://{host}/"), host)


class DangerousNameTests(unittest.TestCase):
    def test_localhost_names_rejected(self):
        self.assertFalse(is_safe_locator("http://localhost/"))
        self.assertFalse(is_safe_locator("http://LOCALHOST:8080/"))
        self.assertFalse(is_safe_locator("http://foo.localhost/"))

    def test_dot_local_mdns_rejected(self):
        self.assertFalse(is_safe_locator("http://printer.local/"))

    def test_cloud_metadata_names_rejected(self):
        self.assertFalse(is_safe_locator("http://metadata.google.internal/"))
        self.assertFalse(is_safe_locator("http://metadata/"))
        self.assertFalse(is_safe_locator("http://instance-data/"))

    def test_reason_is_explicit(self):
        reason = locator_reason("http://169.254.169.254/")
        self.assertIsInstance(reason, str)
        self.assertTrue(reason)
        self.assertIsNone(locator_reason("https://example.org/ok"))


class ResolvedIpTests(unittest.TestCase):
    def test_public_ip_passes(self):
        assert_safe_resolved_ip("93.184.216.34")  # no raise

    def test_private_ip_raises(self):
        with self.assertRaises(SSRFError):
            assert_safe_resolved_ip("10.1.2.3")

    def test_metadata_ip_raises(self):
        with self.assertRaises(SSRFError):
            assert_safe_resolved_ip("169.254.169.254")

    def test_loopback_v6_raises(self):
        with self.assertRaises(SSRFError):
            assert_safe_resolved_ip("::1")

    def test_shared_cgnat_range_raises(self):
        for host in ("100.64.0.1", "100.127.255.254"):
            with self.subTest(host=host), self.assertRaises(SSRFError):
                assert_safe_resolved_ip(host)


if __name__ == "__main__":
    unittest.main()
