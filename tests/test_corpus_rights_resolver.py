import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_adapters.types import RightsState
from corpus_rights_resolver import (
    RightsEvidence,
    RightsResolution,
    RightsResolverError,
    resolve_rights,
)


def _evidence(**overrides):
    base = dict(
        license=None,
        access_class="unknown",
        repository=None,
        is_publicly_visible=False,
        site_license_grant=False,
        source_note=None,
    )
    base.update(overrides)
    return RightsEvidence(**base)


class OpenContentTests(unittest.TestCase):
    def test_cc_by_license_resolves_to_auto_content_acquisition(self):
        resolution = resolve_rights(_evidence(license="cc-by", access_class="open_content"))
        self.assertEqual(resolution.rights_state, RightsState.PUBLIC_RIGHTS_CLEAR)
        self.assertEqual(resolution.disposition, "auto_acquire_content")
        self.assertTrue(resolution.may_acquire_content)
        self.assertTrue(resolution.may_preserve_metadata)
        self.assertIn("cc-by", resolution.basis)

    def test_public_domain_license_is_content_clear(self):
        resolution = resolve_rights(_evidence(license="public-domain", access_class="open_content"))
        self.assertEqual(resolution.rights_state, RightsState.PUBLIC_RIGHTS_CLEAR)
        self.assertTrue(resolution.may_acquire_content)

    def test_pmc_repository_identity_alone_is_not_content_clear(self):
        # Repository identity is NOT a redistribution basis: a PMC-hosted page
        # without an explicit compatible license or site grant fails closed.
        resolution = resolve_rights(_evidence(repository="pmc", access_class="open_content"))
        self.assertNotEqual(resolution.rights_state, RightsState.PUBLIC_RIGHTS_CLEAR)
        self.assertFalse(resolution.may_acquire_content)

    def test_pmc_repository_with_explicit_license_is_content_clear(self):
        # An explicit compatible license on a PMC page IS a redistribution basis.
        resolution = resolve_rights(_evidence(repository="pmc", access_class="open_content", license="cc-by"))
        self.assertEqual(resolution.rights_state, RightsState.PUBLIC_RIGHTS_CLEAR)
        self.assertTrue(resolution.may_acquire_content)

    def test_government_repository_identity_alone_is_not_content_clear(self):
        # A government host is not automatically public-domain; require an
        # explicit public-domain/compatible license or site grant.
        resolution = resolve_rights(_evidence(repository="gov", access_class="open_content"))
        self.assertNotEqual(resolution.rights_state, RightsState.PUBLIC_RIGHTS_CLEAR)
        self.assertFalse(resolution.may_acquire_content)

    def test_government_repository_with_public_domain_assertion_is_content_clear(self):
        resolution = resolve_rights(_evidence(repository="gov", access_class="open_content", license="public-domain"))
        self.assertEqual(resolution.rights_state, RightsState.PUBLIC_RIGHTS_CLEAR)
        self.assertTrue(resolution.may_acquire_content)

    def test_repository_with_site_redistribution_grant_is_content_clear(self):
        resolution = resolve_rights(_evidence(repository="pmc", access_class="open_content", site_license_grant=True))
        self.assertEqual(resolution.rights_state, RightsState.PUBLIC_RIGHTS_CLEAR)
        self.assertTrue(resolution.may_acquire_content)
        self.assertIn("site_license", resolution.basis)

    def test_explicit_site_license_is_content_clear(self):
        resolution = resolve_rights(_evidence(site_license_grant=True, access_class="open_content"))
        self.assertEqual(resolution.rights_state, RightsState.PUBLIC_RIGHTS_CLEAR)
        self.assertTrue(resolution.may_acquire_content)
        self.assertIn("site_license", resolution.basis)


class MetadataOnlyTests(unittest.TestCase):
    def test_metadata_access_preserves_metadata_but_not_content(self):
        resolution = resolve_rights(_evidence(access_class="metadata", repository="openalex", is_publicly_visible=True))
        self.assertEqual(resolution.rights_state, RightsState.PUBLIC_METADATA_ONLY)
        self.assertEqual(resolution.disposition, "metadata_only")
        self.assertFalse(resolution.may_acquire_content)
        self.assertTrue(resolution.may_preserve_metadata)

    def test_known_metadata_repository_is_metadata_only(self):
        resolution = resolve_rights(_evidence(repository="crossref", access_class="unknown", is_publicly_visible=True))
        self.assertEqual(resolution.rights_state, RightsState.PUBLIC_METADATA_ONLY)
        self.assertFalse(resolution.may_acquire_content)


class FailClosedTests(unittest.TestCase):
    def test_unknown_rights_fail_closed(self):
        resolution = resolve_rights(_evidence(access_class="unknown"))
        self.assertEqual(resolution.rights_state, RightsState.RIGHTS_UNCLEAR)
        self.assertEqual(resolution.disposition, "rights_unclear")
        self.assertFalse(resolution.may_acquire_content)
        self.assertFalse(resolution.may_preserve_metadata)

    def test_public_visibility_alone_is_not_a_rights_basis(self):
        resolution = resolve_rights(_evidence(is_publicly_visible=True, access_class="unknown", license=None))
        self.assertEqual(resolution.rights_state, RightsState.RIGHTS_UNCLEAR)
        self.assertFalse(resolution.may_acquire_content)
        self.assertFalse(resolution.may_preserve_metadata)

    def test_paid_source_is_human_gated_not_acquired(self):
        resolution = resolve_rights(_evidence(access_class="paid", is_publicly_visible=True))
        self.assertEqual(resolution.disposition, "human_gate")
        self.assertFalse(resolution.may_acquire_content)
        self.assertNotEqual(resolution.rights_state, RightsState.PUBLIC_RIGHTS_CLEAR)

    def test_login_gate_is_human_gated(self):
        resolution = resolve_rights(_evidence(access_class="login"))
        self.assertEqual(resolution.disposition, "human_gate")
        self.assertFalse(resolution.may_acquire_content)

    def test_private_source_is_human_gated_and_never_shared(self):
        resolution = resolve_rights(_evidence(access_class="private"))
        self.assertEqual(resolution.disposition, "human_gate")
        self.assertEqual(resolution.rights_state, RightsState.PRIVATE_AUTHORIZED)
        self.assertFalse(resolution.may_acquire_content)

    def test_prohibited_source_is_blocked(self):
        resolution = resolve_rights(_evidence(access_class="prohibited"))
        self.assertEqual(resolution.disposition, "blocked")
        self.assertFalse(resolution.may_acquire_content)
        self.assertFalse(resolution.may_preserve_metadata)

    def test_gate_wins_over_open_license(self):
        # A CC-BY record behind a paywall must still fail closed.
        resolution = resolve_rights(_evidence(license="cc-by", access_class="paid"))
        self.assertFalse(resolution.may_acquire_content)
        self.assertEqual(resolution.disposition, "human_gate")

    def test_all_rights_reserved_license_is_not_content_clear(self):
        resolution = resolve_rights(_evidence(license="arr", access_class="open_content", is_publicly_visible=True))
        self.assertFalse(resolution.may_acquire_content)
        self.assertEqual(resolution.rights_state, RightsState.RIGHTS_UNCLEAR)


class ConservativeLicenseTests(unittest.TestCase):
    """Only CC0/PD, CC-BY, and CC-BY-SA auto-ingest as normalized derivative text.

    NoDerivatives (ND) and NonCommercial (NC) variants must not be automatically
    normalized into the shared corpus; they route to an explicit human gate.
    """

    def test_cc_by_sa_is_auto_content_clear(self):
        resolution = resolve_rights(_evidence(license="cc-by-sa", access_class="open_content"))
        self.assertEqual(resolution.disposition, "auto_acquire_content")
        self.assertTrue(resolution.may_acquire_content)

    def test_cc0_is_auto_content_clear(self):
        resolution = resolve_rights(_evidence(license="cc0", access_class="open_content"))
        self.assertEqual(resolution.disposition, "auto_acquire_content")
        self.assertTrue(resolution.may_acquire_content)

    def test_cc_by_nd_is_not_auto_content(self):
        resolution = resolve_rights(_evidence(license="cc-by-nd", access_class="open_content"))
        self.assertFalse(resolution.may_acquire_content)
        self.assertEqual(resolution.disposition, "human_gate")
        self.assertIn("cc-by-nd", resolution.reason)

    def test_cc_by_nc_is_not_auto_content(self):
        resolution = resolve_rights(_evidence(license="cc-by-nc", access_class="open_content"))
        self.assertFalse(resolution.may_acquire_content)
        self.assertEqual(resolution.disposition, "human_gate")
        self.assertIn("cc-by-nc", resolution.reason)

    def test_cc_by_nc_nd_is_not_auto_content(self):
        resolution = resolve_rights(_evidence(license="cc-by-nc-nd", access_class="open_content"))
        self.assertFalse(resolution.may_acquire_content)
        self.assertEqual(resolution.disposition, "human_gate")

    def test_cc_by_nc_sa_is_not_auto_content(self):
        resolution = resolve_rights(_evidence(license="cc-by-nc-sa", access_class="open_content"))
        self.assertFalse(resolution.may_acquire_content)
        self.assertEqual(resolution.disposition, "human_gate")

    def test_restricted_license_still_allows_metadata(self):
        resolution = resolve_rights(_evidence(license="cc-by-nd", access_class="open_content"))
        self.assertTrue(resolution.may_preserve_metadata)


class ValidationTests(unittest.TestCase):
    def test_unknown_access_class_is_rejected(self):
        with self.assertRaises(RightsResolverError):
            _evidence(access_class="totally-made-up")

    def test_resolution_is_frozen(self):
        resolution = resolve_rights(_evidence(license="cc-by", access_class="open_content"))
        self.assertIsInstance(resolution, RightsResolution)
        with self.assertRaises(FrozenInstanceError):
            resolution.basis = "mutated"


if __name__ == "__main__":
    unittest.main()
