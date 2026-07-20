import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from corpus_adapters.types import RightsState
from corpus_rights import (
    CANONICAL_RIGHTS,
    LEGACY_INTAKE_RIGHTS,
    RightsError,
    coerce_rights,
    combine_rights,
    is_shared_corpus_eligible,
    is_rights_upgrade,
    normalize_intake_rights,
)


class CanonicalVocabularyTests(unittest.TestCase):
    def test_canonical_rights_is_exactly_the_four_rights_states(self):
        self.assertEqual(CANONICAL_RIGHTS, frozenset(state.value for state in RightsState))

    def test_legacy_and_canonical_vocabularies_are_disjoint(self):
        self.assertEqual(LEGACY_INTAKE_RIGHTS & CANONICAL_RIGHTS, frozenset())


class IntakeConversionTests(unittest.TestCase):
    def test_public_maps_to_metadata_only_not_rights_clear(self):
        # Public accessibility alone is not redistribution permission.
        self.assertIs(normalize_intake_rights("public"), RightsState.PUBLIC_METADATA_ONLY)

    def test_owned_maps_to_private_authorized(self):
        self.assertIs(normalize_intake_rights("owned"), RightsState.PRIVATE_AUTHORIZED)

    def test_licensed_maps_to_private_authorized(self):
        self.assertIs(normalize_intake_rights("licensed"), RightsState.PRIVATE_AUTHORIZED)

    def test_unknown_maps_to_rights_unclear(self):
        self.assertIs(normalize_intake_rights("unknown"), RightsState.RIGHTS_UNCLEAR)

    def test_no_legacy_value_converts_to_shared_corpus_eligible(self):
        for legacy in LEGACY_INTAKE_RIGHTS:
            self.assertFalse(is_shared_corpus_eligible(normalize_intake_rights(legacy)))

    def test_unmappable_value_fails_closed(self):
        with self.assertRaises(RightsError):
            normalize_intake_rights("free-for-all")

    def test_non_string_fails_closed(self):
        with self.assertRaises(RightsError):
            normalize_intake_rights(None)


class NoUpgradeTests(unittest.TestCase):
    def test_combine_takes_most_restrictive(self):
        self.assertIs(
            combine_rights(RightsState.PUBLIC_RIGHTS_CLEAR, RightsState.RIGHTS_UNCLEAR),
            RightsState.RIGHTS_UNCLEAR,
        )
        self.assertIs(
            combine_rights(RightsState.PRIVATE_AUTHORIZED, RightsState.PUBLIC_METADATA_ONLY),
            RightsState.PUBLIC_METADATA_ONLY,
        )

    def test_combine_cannot_upgrade_to_rights_clear(self):
        self.assertIsNot(
            combine_rights(RightsState.RIGHTS_UNCLEAR, RightsState.PUBLIC_RIGHTS_CLEAR),
            RightsState.PUBLIC_RIGHTS_CLEAR,
        )

    def test_is_rights_upgrade_detects_permissive_moves(self):
        self.assertTrue(is_rights_upgrade(RightsState.RIGHTS_UNCLEAR, RightsState.PUBLIC_RIGHTS_CLEAR))
        self.assertTrue(is_rights_upgrade(RightsState.PUBLIC_METADATA_ONLY, RightsState.PRIVATE_AUTHORIZED))
        self.assertFalse(is_rights_upgrade(RightsState.PUBLIC_RIGHTS_CLEAR, RightsState.RIGHTS_UNCLEAR))
        self.assertFalse(is_rights_upgrade(RightsState.PRIVATE_AUTHORIZED, RightsState.PRIVATE_AUTHORIZED))


class CoerceTests(unittest.TestCase):
    def test_coerce_accepts_canonical_string(self):
        self.assertIs(coerce_rights("public_rights_clear"), RightsState.PUBLIC_RIGHTS_CLEAR)

    def test_coerce_accepts_enum(self):
        self.assertIs(coerce_rights(RightsState.PRIVATE_AUTHORIZED), RightsState.PRIVATE_AUTHORIZED)

    def test_coerce_rejects_legacy_value_fail_closed(self):
        # Legacy "unknown" must not silently pass through the canonical boundary.
        with self.assertRaises(RightsError):
            coerce_rights("unknown")

    def test_shared_eligibility_only_for_rights_clear(self):
        self.assertTrue(is_shared_corpus_eligible(RightsState.PUBLIC_RIGHTS_CLEAR))
        for state in RightsState:
            if state is not RightsState.PUBLIC_RIGHTS_CLEAR:
                self.assertFalse(is_shared_corpus_eligible(state))


if __name__ == "__main__":
    unittest.main()
