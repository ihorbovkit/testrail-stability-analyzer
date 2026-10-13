"""Case applicability resolution against application mappings. Synthetic values only."""

import unittest

from testrail_stability_analyzer.collection import (
    Applicability,
    ApplicabilityValueState,
    CaseApplicability,
    matched_applications,
    normalize_values,
    resolve_applicability,
    unmapped_values,
)

from . import fake_testrail as fx

READ = ApplicabilityValueState.READ


def apps(a=("1", 3), b=(2, "3")):
    """App A lists option IDs 1 and 3, App B lists 2 and 3 (3 meaning both)."""
    mappings = {
        "applications": [
            {"name": "App A", "patterns": ["a"], "applicability_values": list(a)},
            {"name": "App B", "patterns": ["b"], "applicability_values": list(b)},
        ],
        "custom_fields": {"applicability": "custom_apps"},
    }
    return fx.settings(mappings=mappings).mappings.applications


def case(*values, state=READ):
    return CaseApplicability(state, tuple(values))


class NormalizeTests(unittest.TestCase):
    def test_scalars_lists_and_empty_values(self):
        self.assertEqual(normalize_values(None), ())
        self.assertEqual(normalize_values(""), ())
        self.assertEqual(normalize_values([]), ())
        self.assertEqual(normalize_values(3), ("3",))
        self.assertEqual(normalize_values([1, " B ", None, ""]), ("1", "B"))


class ResolveTests(unittest.TestCase):
    def test_matching_and_exclusion(self):
        mappings = apps()
        self.assertEqual(resolve_applicability(case("1"), "App A", mappings), Applicability.APPLICABLE)
        self.assertEqual(resolve_applicability(case("1"), "App B", mappings), Applicability.NOT_APPLICABLE)
        self.assertEqual(resolve_applicability(case("3"), "App B", mappings), Applicability.APPLICABLE)

    def test_integer_and_string_values_match(self):
        mappings = apps()
        self.assertEqual(resolve_applicability(case("2"), "App B", mappings), Applicability.APPLICABLE)
        from_int = case(*normalize_values([1]))
        self.assertEqual(resolve_applicability(from_int, "App A", mappings), Applicability.APPLICABLE)

    def test_unmapped_values_never_exclude(self):
        mappings = apps()
        self.assertEqual(resolve_applicability(case("9"), "App A", mappings), Applicability.UNKNOWN)
        self.assertEqual(resolve_applicability(case("1", "9"), "App B", mappings), Applicability.UNKNOWN)
        self.assertEqual(resolve_applicability(case("1", "9"), "App A", mappings), Applicability.APPLICABLE)

    def test_unresolved_values_are_unknown(self):
        mappings = apps()
        for value in (
            None,
            case(),
            case(state=ApplicabilityValueState.NOT_FETCHED),
            case(state=ApplicabilityValueState.CASE_NOT_FOUND),
        ):
            with self.subTest(value=value):
                self.assertEqual(resolve_applicability(value, "App A", mappings), Applicability.UNKNOWN)

    def test_application_without_values_or_mapping_is_unknown(self):
        mappings = apps(b=())
        self.assertEqual(resolve_applicability(case("1"), "App B", mappings), Applicability.UNKNOWN)
        self.assertEqual(resolve_applicability(case("1"), "App C", mappings), Applicability.UNKNOWN)
        self.assertEqual(resolve_applicability(case("1"), None, mappings), Applicability.UNKNOWN)

    def test_matched_and_unmapped_values(self):
        mappings = apps()
        self.assertEqual(matched_applications(case("3"), mappings), ("App A", "App B"))
        self.assertEqual(matched_applications(case("2", "9"), mappings), ("App B",))
        self.assertEqual(unmapped_values(case("2", "9"), mappings), ("9",))
        self.assertEqual(matched_applications(case(state=ApplicabilityValueState.NOT_FETCHED), mappings), ())


if __name__ == "__main__":
    unittest.main()
