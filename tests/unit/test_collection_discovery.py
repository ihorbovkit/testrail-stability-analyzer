import unittest
from datetime import timedelta

from testrail_stability_analyzer.collection import DimensionState, RunDecision, RunDiscovery

from . import fake_testrail as fx
from .fake_testrail import NOW, FakeTestRail, days_ago

LOOKBACK = NOW - timedelta(days=365)


def discover(server, settings=None, lookback=LOOKBACK):
    settings = settings or fx.settings()
    return RunDiscovery(fx.make_client(server, settings), settings).discover(lookback)


class DiscoveryStructureTests(unittest.TestCase):
    def test_plan_based_runs_keep_plan_context(self):
        server = FakeTestRail()
        server.add_plan(900, "App-A Regression")
        server.add_plan_run(900, fx.run(501, "Regression", config="Plat-X"))
        server.add_plan_run(900, fx.run(502, "Regression", config="Plat-Y"))
        result = discover(server)
        self.assertTrue(result.complete)
        self.assertEqual([(r.run_id, r.plan_id, r.plan_name) for r in result.runs], [
            (501, 900, "App-A Regression"),
            (502, 900, "App-A Regression"),
        ])
        self.assertEqual([r.classification.platform.name for r in result.runs], ["Platform X", "Platform Y"])
        self.assertTrue(all(r.classification.application.name == "App A" for r in result.runs))

    def test_standalone_runs_without_plans(self):
        server = FakeTestRail()
        server.add_run(fx.run(600, "App-B Plat-Y nightly"))
        result = discover(server)
        (run,) = result.runs
        self.assertIsNone(run.plan_id)
        self.assertIsNone(run.plan_name)
        self.assertEqual((run.classification.application.name, run.classification.platform.name), ("App B", "Platform Y"))
        self.assertTrue(run.run.created_on and run.run.is_completed is False)

    def test_mixed_structures_and_plan_with_single_platform_and_empty_plan(self):
        server = FakeTestRail()
        server.add_run(fx.run(600, "App-A Plat-X smoke"))
        server.add_plan(900, "App-A Release")
        server.add_plan_run(900, fx.run(501, "Release", config="Plat-Y"))
        server.add_plan(901, "Empty plan")
        result = discover(server)
        self.assertEqual([r.run_id for r in result.runs], [501, 600])
        self.assertIn("get_plan/901", server.calls)

    def test_runs_are_deduplicated_and_plan_context_is_preferred(self):
        server = FakeTestRail()
        plan_run = fx.run(501, "App-A Plat-X", plan_id=900)
        server.add_plan(900, "App-A Plan")
        server.add_plan_run(900, plan_run)
        # An open run created before the lookback also appears in a second listing.
        server.add_run({**plan_run, "plan_id": None})
        server.add_run(fx.run(600, "App-A Plat-X", created=days_ago(400)))
        result = discover(server)
        self.assertEqual([r.run_id for r in result.runs], [501, 600])
        self.assertEqual(result.runs[0].plan_id, 900)

    def test_unknown_names_are_preserved_as_unresolved(self):
        server = FakeTestRail()
        server.add_run(fx.run(600, "Weekly something"))
        server.add_plan(900, "Q2 sign-off")
        server.add_plan_run(900, fx.run(501, "Config 3"))
        result = discover(server)
        self.assertEqual(len(result.runs), 2)
        self.assertTrue(all(r.included for r in result.runs))
        self.assertTrue(all(r.classification.application.state is DimensionState.UNRESOLVED for r in result.runs))

    def test_empty_project(self):
        result = discover(FakeTestRail())
        self.assertEqual(result.runs, ())
        self.assertTrue(result.complete)


class DiscoveryLookbackTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeTestRail()
        self.server.add_run(fx.run(1, "App-A Plat-X recent"))
        self.server.add_run(fx.run(2, "App-A Plat-X old open", created=days_ago(500)))
        self.server.add_run(fx.run(3, "App-A Plat-X old closed", created=days_ago(500), completed=days_ago(499)))
        self.server.add_plan(900, "App-A old open plan", created=days_ago(500))
        self.server.add_plan_run(900, fx.run(10, "Plat-X old closed", created=days_ago(500), completed=days_ago(498)))
        self.server.add_plan_run(900, fx.run(11, "Plat-X added recently", created=days_ago(3)))
        self.server.add_plan(901, "App-A old closed plan", created=days_ago(500), completed=days_ago(490))
        self.server.add_plan_run(901, fx.run(20, "Plat-X", created=days_ago(500), completed=days_ago(490)))

    def test_lookback_includes_recent_and_open_runs_and_records_boundary_limitation(self):
        result = discover(self.server)
        decisions = {r.run_id: r.decision for r in result.runs}
        self.assertEqual(decisions, {
            1: RunDecision.INCLUDED,
            2: RunDecision.INCLUDED,
            10: RunDecision.OUTSIDE_LOOKBACK,
            11: RunDecision.INCLUDED,
        })
        self.assertNotIn("get_plan/901", self.server.calls)
        self.assertTrue(any("before the lookback start" in note for note in result.limitations))

    def test_unlimited_lookback_lists_everything(self):
        result = discover(self.server, lookback=None)
        self.assertEqual(sorted(r.run_id for r in result.runs), [1, 2, 3, 10, 11, 20])
        self.assertTrue(all(r.included for r in result.runs))
        self.assertEqual(result.limitations, ())


class DiscoveryFilterTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeTestRail()
        self.server.add_run(fx.run(1, "App-A Plat-X automated"))
        self.server.add_run(fx.run(2, "App-B Plat-X automated"))
        self.server.add_run(fx.run(3, "Unknown Plat-X automated"))
        self.server.add_run(fx.run(4, "App-A Plat-Y manual"))
        self.server.add_run(fx.run(5, "App-A App-B Plat-X automated"))

    def settings(self, **filters):
        return fx.settings(
            mappings={
                "execution_types": [
                    {"name": "Automated", "patterns": ["automated"]},
                    {"name": "Manual", "patterns": ["manual"]},
                ]
            },
            filters=filters,
        )

    def test_application_platform_and_execution_type_filters(self):
        result = discover(self.server, self.settings(applications=["App A"], platforms=["Platform X"], execution_types=["Automated"]))
        decisions = {r.run_id: r.decision for r in result.runs}
        self.assertEqual(decisions, {
            1: RunDecision.INCLUDED,
            2: RunDecision.EXCLUDED_BY_FILTER,
            3: RunDecision.UNCLASSIFIED,
            4: RunDecision.EXCLUDED_BY_FILTER,
            5: RunDecision.UNCLASSIFIED,
        })
        self.assertEqual([r.run_id for r in result.unclassified], [3, 5])
        self.assertIn("ambiguous", result.runs[4].reason)

    def test_without_filters_everything_is_included(self):
        result = discover(self.server, self.settings())
        self.assertTrue(all(r.included for r in result.runs))

    def test_name_patterns_and_suite_are_recorded_exclusions(self):
        self.server.add_run(fx.run(6, "App-A Plat-X other suite", suite_id=99))
        settings = fx.settings(
            testrail={"base_url": "https://example.testrail.io", "project_id": 1, "suite_id": 10},
            naming={"run_name_patterns": ["automated|suite"]},
        )
        result = discover(self.server, settings)
        decisions = {r.run_id: r.decision for r in result.runs}
        self.assertEqual(decisions[4], RunDecision.EXCLUDED_BY_NAME)
        # The fake applies suite_id server-side for standalone runs, like TestRail.
        self.assertNotIn(6, decisions)
        self.assertEqual(decisions[1], RunDecision.INCLUDED)

    def test_plan_run_from_other_suite_is_excluded_with_reason(self):
        self.server.add_plan(900, "App-A")
        self.server.add_plan_run(900, fx.run(7, "Plat-X", suite_id=99))
        settings = fx.settings(testrail={"base_url": "https://example.testrail.io", "project_id": 1, "suite_id": 10})
        run = next(r for r in discover(self.server, settings).runs if r.run_id == 7)
        self.assertEqual(run.decision, RunDecision.EXCLUDED_BY_FILTER)
        self.assertIn("suite", run.reason)


class DiscoveryBudgetTests(unittest.TestCase):
    def test_budget_exhaustion_marks_discovery_incomplete(self):
        server = FakeTestRail()
        server.add_run(fx.run(1, "App-A Plat-X"))
        for plan_id in (900, 901, 902):
            server.add_plan(plan_id, "App-A")
            server.add_plan_run(plan_id, fx.run(plan_id, "Plat-X"))
        result = discover(server, fx.settings(http={"max_requests_per_execution": 5}))
        self.assertFalse(result.complete)
        self.assertTrue(any("request budget" in note for note in result.limitations))
        self.assertIn(1, [r.run_id for r in result.runs])
