import unittest

from testrail_stability_analyzer.config import StatusCategory, settings_from_mapping
from testrail_stability_analyzer.models import ResolutionSource, Status, StatusResolver

from . import payloads

STATUSES = [Status.from_api(item) for item in payloads.STATUSES]


class StatusResolverTests(unittest.TestCase):
    def test_system_statuses_resolve_by_system_name(self):
        resolver = StatusResolver(STATUSES)
        expected = {
            1: StatusCategory.PASSED,
            2: StatusCategory.BLOCKED,
            3: StatusCategory.NOT_RUN,
            4: StatusCategory.RETEST,
            5: StatusCategory.FAILED,
        }
        for status_id, category in expected.items():
            with self.subTest(status_id=status_id):
                resolved = resolver.resolve(status_id)
                self.assertEqual(resolved.category, category)
                self.assertEqual(resolved.source, ResolutionSource.SYSTEM_DEFAULT)

    def test_unmapped_custom_statuses_are_unknown_not_guessed(self):
        # "Automation Problem" / "Not Applicable" labels must not be interpreted.
        resolver = StatusResolver(STATUSES)
        for status_id in (6, 7):
            resolved = resolver.resolve(status_id)
            self.assertEqual(resolved.category, StatusCategory.UNKNOWN)
            self.assertEqual(resolved.source, ResolutionSource.UNRESOLVED)
            self.assertTrue(resolved.is_known)

    def test_configured_mappings_by_name_and_id(self):
        resolver = StatusResolver(
            STATUSES,
            {StatusCategory.AUTOMATION_ISSUE: ("custom_status1",), StatusCategory.NOT_APPLICABLE: (7,)},
        )
        automation = resolver.resolve(6)
        self.assertEqual(
            (automation.status_id, automation.name, automation.label, automation.category, automation.source),
            (6, "custom_status1", "Automation Problem", StatusCategory.AUTOMATION_ISSUE, ResolutionSource.CONFIGURED),
        )
        self.assertEqual(resolver.resolve(7).category, StatusCategory.NOT_APPLICABLE)
        self.assertEqual(resolver.unmatched_mappings, ())

    def test_configuration_overrides_system_default(self):
        resolver = StatusResolver(STATUSES, {StatusCategory.FAILED: ("retest",)})
        self.assertEqual(resolver.resolve(4).category, StatusCategory.FAILED)

    def test_works_with_settings_mappings(self):
        settings = settings_from_mapping(
            {
                "testrail": {"base_url": "https://example.testrail.io", "project_id": 1},
                "mappings": {"statuses": {"automation_issue": ["custom_status1"]}},
            }
        )
        resolver = StatusResolver(STATUSES, settings.mappings.statuses)
        self.assertEqual(resolver.resolve(6).category, StatusCategory.AUTOMATION_ISSUE)

    def test_missing_optional_custom_status_is_reported_not_fatal(self):
        resolver = StatusResolver(STATUSES[:5], {StatusCategory.AUTOMATION_ISSUE: ("au_issue", 12)})
        self.assertEqual(
            resolver.unmatched_mappings,
            ((StatusCategory.AUTOMATION_ISSUE, "au_issue"), (StatusCategory.AUTOMATION_ISSUE, 12)),
        )

    def test_unknown_status_id(self):
        resolved = StatusResolver(STATUSES).resolve(99)
        self.assertEqual((resolved.status_id, resolved.category), (99, StatusCategory.UNKNOWN))
        self.assertFalse(resolved.is_known)
        self.assertIsNone(resolved.label)

    def test_custom_untested_flag_maps_to_not_run(self):
        custom = Status.from_api(payloads.status(8, "custom_status3", "Queued", is_system=False, is_untested=True))
        resolved = StatusResolver([custom]).resolve(8)
        self.assertEqual((resolved.category, resolved.source), (StatusCategory.NOT_RUN, ResolutionSource.UNTESTED_FLAG))

    def test_custom_status_with_system_like_name_is_not_defaulted(self):
        custom = Status.from_api(payloads.status(9, "passed", "Passed?", is_system=False))
        self.assertEqual(StatusResolver([custom]).resolve(9).category, StatusCategory.UNKNOWN)

    def test_resolved_covers_all_instance_statuses(self):
        self.assertEqual(sorted(StatusResolver(STATUSES).resolved), [1, 2, 3, 4, 5, 6, 7])

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            StatusResolver(STATUSES + [STATUSES[0]])
        with self.assertRaises(ValueError):
            StatusResolver(STATUSES, {StatusCategory.UNKNOWN: (1,)})
        with self.assertRaises(ValueError):
            StatusResolver(STATUSES, {StatusCategory.FAILED: (5,), StatusCategory.RETEST: ("failed",)})


if __name__ == "__main__":
    unittest.main()
