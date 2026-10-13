import dataclasses
import unittest

from testrail_stability_analyzer.collection import (
    CaseGroup,
    CaseMetadataCache,
    FeatureGroupState,
    HistoricalCollector,
    enrich_feature_groups,
    feature_group_label,
    resolve_section_path,
)
from testrail_stability_analyzer.config import FeatureGroupSettings
from testrail_stability_analyzer.models import Section

from . import fake_testrail as fx
from . import payloads
from .fake_testrail import NOW, PASSED, FakeTestRail, days_ago

# Synthetic hierarchy: two top-level areas, one with nested feature sections.
SECTIONS = [
    payloads.section(1, "Area One"),
    payloads.section(2, "Area Two"),
    payloads.section(3, "Feature_Alpha", parent_id=2),
    payloads.section(4, "Feature Beta", parent_id=2),
    payloads.section(5, "Sub/Feature", parent_id=4, depth=2),
]


def sections(*items):
    return {item["id"]: Section.from_api(item) for item in items}


class ResolveSectionPathTests(unittest.TestCase):
    def test_paths_follow_parent_links_to_the_top_level(self):
        tree = sections(*SECTIONS)
        self.assertEqual(resolve_section_path(1, tree).names, ("Area One",))
        self.assertEqual(resolve_section_path(3, tree).names, ("Area Two", "Feature_Alpha"))
        self.assertEqual(resolve_section_path(5, tree).names, ("Area Two", "Feature Beta", "Sub/Feature"))
        self.assertIs(resolve_section_path(5, tree).state, FeatureGroupState.RESOLVED)

    def test_missing_section(self):
        tree = sections(*SECTIONS)
        for section_id in (None, 99):
            with self.subTest(section_id=section_id):
                path = resolve_section_path(section_id, tree)
                self.assertEqual((path.state, path.names), (FeatureGroupState.SECTION_NOT_FOUND, ()))

    def test_missing_parent_is_a_broken_hierarchy(self):
        tree = sections(payloads.section(7, "Orphan", parent_id=70))
        path = resolve_section_path(7, tree)
        self.assertEqual((path.state, path.names), (FeatureGroupState.BROKEN_HIERARCHY, ()))

    def test_cycles_are_detected(self):
        cycle = sections(payloads.section(8, "Loop A", parent_id=9), payloads.section(9, "Loop B", parent_id=8))
        self_parent = sections(payloads.section(10, "Self", parent_id=10))
        self.assertIs(resolve_section_path(8, cycle).state, FeatureGroupState.CYCLIC_HIERARCHY)
        self.assertIs(resolve_section_path(10, self_parent).state, FeatureGroupState.CYCLIC_HIERARCHY)


class FeatureGroupLabelTests(unittest.TestCase):
    GROUP = CaseGroup(100, FeatureGroupState.RESOLVED, 10, 5, ("Area Two", "Feature Beta", "Sub/Feature"), "Suite S")

    def test_full_path_with_default_separator(self):
        self.assertEqual(feature_group_label(self.GROUP, FeatureGroupSettings()), "Area Two/Feature Beta/Sub/Feature")

    def test_depth_separator_and_suite_name(self):
        settings = FeatureGroupSettings(separator=" > ", max_depth=2, include_suite_name=True)
        self.assertEqual(feature_group_label(self.GROUP, settings), "Suite S > Area Two > Feature Beta")

    def test_names_containing_the_separator_keep_their_structure(self):
        self.assertEqual(self.GROUP.path[-1], "Sub/Feature")

    def test_unresolved_group_has_no_label(self):
        group = CaseGroup(100, FeatureGroupState.CYCLIC_HIERARCHY, 10, 8)
        self.assertIsNone(feature_group_label(group, FeatureGroupSettings()))


def build_server(page_size=0):
    server = FakeTestRail(page_size=page_size)
    server.cases[10] = [
        payloads.case(100, section_id=1),
        payloads.case(101, section_id=5),
        payloads.case(103, section_id=99),
        payloads.case(200, section_id=3),  # not executed: never requested in a report
    ]
    server.sections[10] = list(SECTIONS)
    for run_id in (1, 2):  # two runs of the same suite
        server.add_run(
            fx.run(run_id, "App-A Plat-X", created=days_ago(5)),
            tests=[fx.test(run_id * 10 + i, case_id, run_id) for i, case_id in enumerate((100, 101, 102, 103))],
            results=[fx.result(run_id * 10, run_id * 10, PASSED, days_ago(4))],
        )
    return server


def collect(server, settings=None):
    settings = settings or fx.settings()
    client = fx.make_client(server, settings)
    cache = CaseMetadataCache(client, settings.testrail.project_id)
    report = HistoricalCollector(client, settings, fx.resolver(), clock=lambda: NOW, case_cache=cache).collect()
    return report, client, cache, settings


def enrich(server, settings=None):
    report, client, cache, settings = collect(server, settings)
    return report, enrich_feature_groups(report, client, settings, cache)


def calls(server, prefix):
    return [call for call in server.calls if call.startswith(prefix)]


class EnrichmentTests(unittest.TestCase):
    def test_groups_for_every_case_with_a_history(self):
        _, report = enrich(build_server())
        groups = report.case_groups
        self.assertEqual(sorted(groups), [100, 101, 102, 103])
        self.assertEqual(groups[100].path, ("Area One",))
        self.assertEqual((groups[101].path, groups[101].section_id), (("Area Two", "Feature Beta", "Sub/Feature"), 5))
        self.assertIs(groups[102].state, FeatureGroupState.CASE_NOT_FOUND)
        self.assertIs(groups[103].state, FeatureGroupState.SECTION_NOT_FOUND)
        self.assertIn(
            "Feature / Test Group is unknown for 2 case(s): case_not_found=1, section_not_found=1", report.limitations
        )

    def test_metadata_is_fetched_once_per_suite(self):
        server = build_server()
        enrich(server)
        self.assertEqual(calls(server, "get_cases/"), ["get_cases/1"])
        self.assertEqual(calls(server, "get_sections/"), ["get_sections/1"])
        params = [p for c, p in zip(server.calls, server.params, strict=True) if c.startswith("get_sections/")]
        self.assertEqual(params, [{"suite_id": "10"}])
        self.assertNotIn("get_suites/1", server.calls)  # suite names are not requested by default

    def test_pagination(self):
        server = build_server(page_size=2)
        _, report = enrich(server)
        self.assertEqual(len(calls(server, "get_sections/")), 3)  # 5 sections, 2 per page
        self.assertEqual(report.case_groups[101].path, ("Area Two", "Feature Beta", "Sub/Feature"))

    def test_cache_is_shared_with_applicability_lookup(self):
        server = build_server()
        settings = fx.settings(
            mappings={
                "custom_fields": {"applicability": "custom_apps"},
                "applications": [{"name": "App A", "patterns": ["(?i)app-a"], "applicability_values": ["A"]}],
            }
        )
        report, client, cache, settings = collect(server, settings)
        self.assertEqual(calls(server, "get_cases/"), ["get_cases/1"])  # the collector's applicability fallback
        report = enrich_feature_groups(report, client, settings, cache)
        self.assertEqual(calls(server, "get_cases/"), ["get_cases/1"])  # reused, not requested again
        self.assertEqual(report.case_groups[100].path, ("Area One",))

    def test_multi_suite_projects(self):
        server = build_server()
        server.cases[20] = [payloads.case(300, suite_id=20, section_id=50)]
        server.sections[20] = [payloads.section(50, "Other Area", suite_id=20)]
        server.add_run(
            fx.run(3, "App-A Plat-X other suite", created=days_ago(5), suite_id=20),
            tests=[fx.test(30, 300, 3)],
            results=[fx.result(30, 30, PASSED, days_ago(4))],
        )
        _, report = enrich(server)
        self.assertEqual((report.case_groups[300].suite_id, report.case_groups[300].path), (20, ("Other Area",)))
        self.assertEqual(report.case_groups[100].suite_id, 10)
        params = [p["suite_id"] for c, p in zip(server.calls, server.params, strict=True) if c.startswith("get_sec")]
        self.assertEqual(params, ["10", "20"])

    def add_other_suite_run(self, server, case_id, created_days_ago):
        server.add_run(
            fx.run(3, "App-A Plat-X other suite", created=days_ago(created_days_ago), suite_id=20),
            tests=[fx.test(30, case_id, 3)],
            results=[fx.result(30, 30, PASSED, days_ago(created_days_ago - 1))],
        )

    def test_case_moved_to_a_suite_of_a_newer_run_shows_its_current_group(self):
        server = build_server()
        server.cases[10] = [payloads.case(101, section_id=5), payloads.case(103, section_id=99)]  # 100 moved away
        server.cases[20] = [payloads.case(100, suite_id=20, section_id=50)]
        server.sections[20] = [payloads.section(50, "New Area", suite_id=20)]
        self.add_other_suite_run(server, 100, created_days_ago=2)  # newer than the suite 10 runs
        _, report = enrich(server)
        group = report.case_groups[100]
        self.assertEqual((group.state, group.suite_id, group.path), (FeatureGroupState.RESOLVED, 20, ("New Area",)))
        self.assertIs(report.case_groups[102].state, FeatureGroupState.CASE_NOT_FOUND)  # in no collected suite
        self.assertEqual(calls(server, "get_cases/"), ["get_cases/1", "get_cases/1"])  # once per suite
        params = [p["suite_id"] for c, p in zip(server.calls, server.params, strict=True) if c.startswith("get_sec")]
        self.assertEqual(params, ["20", "10"])  # suite 20 first: 100's newest run is there

    def test_case_moved_to_another_collected_suite_is_found_there(self):
        server = build_server()
        server.cases[10] = [payloads.case(101, section_id=5)]  # 100 is now in suite 20
        server.cases[20] = [payloads.case(100, suite_id=20, section_id=50), payloads.case(300, suite_id=20)]
        server.sections[20] = [payloads.section(50, "New Area", suite_id=20)]
        self.add_other_suite_run(server, 300, created_days_ago=8)  # 100 itself never ran in suite 20
        _, report = enrich(server)
        self.assertEqual((report.case_groups[100].suite_id, report.case_groups[100].path), (20, ("New Area",)))

    def test_suite_names_cost_one_request(self):
        server = build_server()
        server.suites = [payloads.suite(id=10, name="Suite Ten"), payloads.suite(id=20, name="Suite Twenty")]
        settings = fx.settings(report={"feature_group": {"include_suite_name": True}})
        _, report = enrich(server, settings)
        self.assertEqual(calls(server, "get_suites/"), ["get_suites/1"])
        self.assertEqual(report.case_groups[100].suite_name, "Suite Ten")
        self.assertEqual(feature_group_label(report.case_groups[100], settings.report.feature_group), "Suite Ten/Area One")

    def test_budget_exhaustion_keeps_stability_results(self):
        _, unlimited, _, settings = collect(build_server())
        # Exactly the requests collection needs: nothing is left for metadata.
        budget = dataclasses.replace(settings.http, max_requests_per_execution=unlimited.requests_made)
        tight = dataclasses.replace(settings, http=budget)
        report, client, cache, _ = collect(build_server(), tight)
        enriched = enrich_feature_groups(report, client, tight, cache)

        self.assertFalse(report.budget_exhausted)
        self.assertEqual(enriched.histories, report.histories)
        self.assertEqual(enriched.outcomes, report.outcomes)
        self.assertEqual(enriched.is_partial, report.is_partial)
        self.assertEqual(enriched.budget_exhausted, report.budget_exhausted)
        self.assertTrue(all(g.state is FeatureGroupState.NOT_FETCHED for g in enriched.case_groups.values()))
        self.assertIn(
            "case metadata of suite 10 not fetched (request budget exhausted); "
            "Feature / Test Group is unknown for its cases",
            enriched.limitations,
        )

    def test_inaccessible_sections_are_reported_without_server_text(self):
        server = build_server()
        server.errors["get_sections/1"] = (403, "Synthetic server text that must not be echoed")
        _, report = enrich(server)
        states = {case_id: group.state for case_id, group in report.case_groups.items()}
        inaccessible = FeatureGroupState.INACCESSIBLE
        # 102 is in no suite, so its sections are not needed.
        self.assertEqual(states, {100: inaccessible, 101: inaccessible, 102: FeatureGroupState.CASE_NOT_FOUND,
                                  103: inaccessible})  # fmt: skip
        note = next(note for note in report.limitations if note.startswith("sections of suite 10"))
        self.assertIn("HTTP 403", note)
        self.assertNotIn("Synthetic server text", " ".join(report.limitations))

    def test_broken_and_cyclic_hierarchies_in_a_suite(self):
        server = build_server()
        server.sections[10] = [
            *SECTIONS,
            payloads.section(6, "Orphan", parent_id=60),
            payloads.section(8, "Loop", parent_id=9),
            payloads.section(9, "Loop", parent_id=8),
        ]
        server.cases[10] = [payloads.case(100, section_id=6), payloads.case(101, section_id=8)]
        _, report = enrich(server)
        self.assertIs(report.case_groups[100].state, FeatureGroupState.BROKEN_HIERARCHY)
        self.assertIs(report.case_groups[101].state, FeatureGroupState.CYCLIC_HIERARCHY)

    def test_collection_alone_never_fetches_section_metadata(self):
        server = build_server()
        report, *_ = collect(server)
        self.assertIsNone(report.case_groups)
        for prefix in ("get_sections/", "get_cases/", "get_suites/", "get_case_types"):
            self.assertEqual(calls(server, prefix), [])


if __name__ == "__main__":
    unittest.main()
