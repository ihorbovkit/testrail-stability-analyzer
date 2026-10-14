import io
import threading
import unittest
from datetime import timedelta

from testrail_stability_analyzer.api import TestRailClient, TestRailRequestBudgetExceeded, TransportTimeout
from testrail_stability_analyzer.collection import HistoricalCollector, RunDiscovery
from testrail_stability_analyzer.console_progress import ConsoleProgress, NullProgress, Outcome
from testrail_stability_analyzer.progress import (
    BudgetExhausted,
    CollectionProgress,
    DiscoveryProgress,
    RequestFinished,
    RequestStarted,
    RetryScheduled,
    Stage,
    StageStarted,
)

from . import fake_testrail as fx
from .fake_testrail import NOW, PASSED, FakeTestRail, days_ago
from .fakes import FAKE_KEY, FAKE_USER, FakeCredentialProvider, make_client, ok
from .test_retry import rate_limited


def of_type(events, kind):
    return [event for event in events if isinstance(event, kind)]


def build_server():
    server = FakeTestRail()
    server.add_plan(900, "App-A Regression", created=days_ago(4))
    for run_id, config in ((501, "Plat-X"), (502, "Plat-Y")):
        server.add_plan_run(
            900,
            fx.run(run_id, "Nightly", created=days_ago(4), config=config),
            tests=[fx.test(run_id * 10, 100, run_id)],
            results=[fx.result(run_id * 10, run_id * 10, PASSED, days_ago(3))],
        )
    server.add_run(
        fx.run(601, "App-A smoke Plat-X", created=days_ago(1)),
        tests=[fx.test(6010, 100, 601)],
        results=[fx.result(6010, 6010, PASSED, days_ago(1))],
    )
    return server


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def reporter(phases=("Setup", "Discovery", "Collection"), *, tty=False, interval=15, **kwargs):
    stream, clock = io.StringIO(), FakeClock()
    progress = ConsoleProgress(stream, phases, tty=tty, interval=interval, clock=clock, width=lambda: 200, **kwargs)
    return progress, stream, clock


class ClientEventTests(unittest.TestCase):
    def test_request_events_match_the_request_count(self):
        events = []
        server = build_server()
        client = fx.make_client(server, fx.settings(), progress=events.append)
        list(client.get_results_for_run(501, created_after=123))
        client.get_plan(900)

        started = of_type(events, RequestStarted)
        self.assertEqual(len(started), client.requests_made)
        self.assertEqual(len(of_type(events, RequestFinished)), client.requests_made)
        self.assertEqual([event.requests_made for event in started], [1, 2])
        # Endpoint path only: no query string (filters) and nothing from the credentials.
        self.assertEqual([event.endpoint for event in started], ["get_results_for_run/501", "get_plan/900"])
        self.assertEqual(started[0].budget, 500)
        self.assertNotIn(FAKE_KEY, repr(events))
        self.assertNotIn(FAKE_USER, repr(events))

    def test_retry_events_are_emitted_before_waiting(self):
        events, sleeps = [], []
        http = {"max_retries": 3, "retry_backoff_seconds": 1.0, "max_retry_delay_seconds": 10.0}

        def sleep(seconds):
            sleeps.append(seconds)
            # The event is already delivered when the client starts waiting.
            self.assertEqual(len(of_type(events, RetryScheduled)), len(sleeps))

        responses = (rate_limited("2"), TransportTimeout("timed out"), ok({}, status=503), ok({"id": 1}))
        client, transport, _ = make_client(*responses, http=http, sleep=sleep, progress=events.append)
        client.get("get_case/1")

        retries = of_type(events, RetryScheduled)
        self.assertEqual([r.reason for r in retries], ["rate limited (HTTP 429)", "timeout", "HTTP 503"])
        self.assertEqual([r.delay_seconds for r in retries], [2.0, 2.0, 4.0])
        self.assertEqual([r.attempt for r in retries], [2, 3, 4])
        self.assertEqual({r.max_attempts for r in retries}, {4})
        self.assertEqual([e.status for e in of_type(events, RequestFinished)], [429, None, 503, 200])
        self.assertEqual(len(of_type(events, RequestStarted)), 4)
        self.assertEqual(client.requests_made, len(transport.calls))

    def test_budget_exhaustion_is_reported(self):
        events = []
        client, _, _ = make_client(ok({}), http={"max_requests_per_execution": 1}, progress=events.append)
        client.get("get_case/1")
        with self.assertRaises(TestRailRequestBudgetExceeded):
            client.get("get_case/2")

        self.assertEqual(of_type(events, BudgetExhausted), [BudgetExhausted(1, 1)])
        self.assertEqual(len(of_type(events, RequestStarted)), 1)


class DiscoveryAndCollectionEventTests(unittest.TestCase):
    def collect(self, settings=None, server=None):
        events = []
        settings = settings or fx.settings()
        client = fx.make_client(server or build_server(), settings, progress=events.append)
        collector = HistoricalCollector(client, settings, fx.resolver(), clock=lambda: NOW, progress=events.append)
        return collector.collect(), events, client

    def test_project_wide_stages_and_counts(self):
        report, events, client = self.collect()

        stages = of_type(events, StageStarted)
        self.assertEqual(stages, [StageStarted(Stage.DISCOVERY), StageStarted(Stage.COLLECTION)])
        discovery = of_type(events, DiscoveryProgress)
        self.assertEqual(discovery[-1], DiscoveryProgress(plans_fetched=1, runs_found=3, runs_included=3))
        self.assertIsNone(discovery[-1].targets_total)  # unknown total: no X/Y shown
        # Discovery events precede the collection stage.
        self.assertLess(events.index(discovery[-1]), events.index(stages[1]))

        collection = of_type(events, CollectionProgress)
        last = collection[-1]
        self.assertEqual(last.step, "tests of runs with results")
        self.assertEqual(last.processed, last.total)
        self.assertEqual(last.runs_with_results, 3)
        self.assertEqual(last.runs_skipped, 0)
        self.assertEqual(last.records, report.result_records_fetched)
        for event in collection:
            self.assertLessEqual(event.processed, event.total)
        self.assertEqual(len(of_type(events, RequestStarted)), client.requests_made)

    def test_runs_without_results_are_not_counted_as_runs_with_results(self):
        server = build_server()
        server.add_run(fx.run(602, "App-A smoke Plat-Y", created=days_ago(1)), tests=[fx.test(6020, 100, 602)])
        report, events, _ = self.collect(server=server)

        self.assertIn(602, [run.run_id for run in report.discovery.included])  # scanned, but without results
        last = of_type(events, CollectionProgress)[-1]
        self.assertEqual(last.runs_with_results, 3)  # 501, 502, 601; the empty run 602 is not counted

    def test_skipped_runs_are_counted_when_the_budget_runs_out(self):
        # statuses are not fetched here; discovery uses 5 requests, leaving 2 for collection.
        report, events, client = self.collect(fx.settings(http={"max_requests_per_execution": 7}))

        self.assertTrue(report.budget_exhausted)
        last = of_type(events, CollectionProgress)[-1]
        self.assertGreater(last.runs_skipped, 0)
        self.assertEqual(len(of_type(events, BudgetExhausted)), 1)
        self.assertEqual(len(of_type(events, RequestStarted)), client.requests_made)

    def test_discovery_reports_runs_found_before_the_budget_ran_out(self):
        # The first standalone-run listing succeeds; the second (open runs before the cutoff) exceeds the budget.
        settings = fx.settings(http={"max_requests_per_execution": 1})
        events = []
        client = fx.make_client(build_server(), settings)
        result = RunDiscovery(client, settings, progress=events.append).discover(NOW - timedelta(days=30))

        self.assertTrue(result.budget_exhausted)
        self.assertGreater(len(result.runs), 0)
        self.assertEqual(of_type(events, DiscoveryProgress)[-1].runs_found, len(result.runs))

    def test_targeted_discovery_reports_selection_totals(self):
        events = []
        client = fx.make_client(build_server(), fx.settings())
        RunDiscovery(client, fx.settings(), progress=events.append).discover_selected((900,), (601,))

        discovery = of_type(events, DiscoveryProgress)
        self.assertEqual([(e.targets_done, e.targets_total) for e in discovery], [(0, 2), (1, 2), (2, 2)])
        self.assertEqual(discovery[-1].runs_found, 3)

    def test_sample_selection_reports_candidates(self):
        events = []
        client = fx.make_client(build_server(), fx.settings())
        RunDiscovery(client, fx.settings(), progress=events.append).find_sample_plan(None, max_candidates=3)

        discovery = of_type(events, DiscoveryProgress)
        # The eligible first candidate is still reported as fetched before the selection returns.
        self.assertEqual(
            [(e.plans_listed, e.plans_fetched, e.targets_done, e.targets_total) for e in discovery],
            [(1, 0, 0, 1), (1, 1, 1, 1)],
        )
        self.assertEqual({e.target_kind for e in discovery}, {"candidate plans"})

    def test_sample_selection_counts_invalid_and_ineligible_candidates(self):
        server = build_server()
        server.add_plan(901, "App-A Smoke", created=days_ago(2))
        server.errors["get_plan/901"] = (403, "no access")
        settings = fx.settings(naming={"run_name_patterns": ["^Nothing$"]})
        events = []
        client = fx.make_client(server, settings)
        selection = RunDiscovery(client, settings, progress=events.append).find_sample_plan(None, max_candidates=3)

        self.assertIsNone(selection.plan)
        discovery = of_type(events, DiscoveryProgress)
        self.assertEqual(
            [(e.plans_fetched, e.targets_done, e.targets_total) for e in discovery], [(0, 0, 2), (0, 1, 2), (1, 2, 2)]
        )


class ConsoleProgressTests(unittest.TestCase):
    def test_phases_are_numbered_in_order_with_remaining_counts(self):
        progress, stream, clock = reporter()
        for index in (1, 2, 3):
            progress.phase(index)
            clock.advance(2)
        progress.finish(Outcome.COMPLETED)
        lines = stream.getvalue().splitlines()

        self.assertEqual(
            [line.split("] ", 1)[1] for line in lines],
            [
                "Phase 1/3: Setup (0 completed, 2 remaining after this)",
                "Phase 1/3 ended after 2s",
                "Phase 2/3: Discovery (1 completed, 1 remaining after this)",
                "Phase 2/3 ended after 2s",
                "Phase 3/3: Collection (2 completed, 0 remaining after this)",
                "Phase 3/3 ended after 2s",
                "Completed all 3 phases in 6s",
            ],
        )

    def test_stage_events_enter_mapped_phases_once(self):
        progress, stream, _ = reporter(stage_phases={Stage.DISCOVERY: 2, Stage.COLLECTION: 3})
        progress.phase(1)
        progress.phase(2)  # e.g. sample selection starts discovery before the collector does
        progress(StageStarted(Stage.DISCOVERY))
        progress(StageStarted(Stage.COLLECTION))

        self.assertEqual(stream.getvalue().count("Phase 2/3: Discovery"), 1)
        self.assertIn("Phase 3/3: Collection", stream.getvalue())

    def test_pending_request_is_reported_before_it_completes(self):
        progress, stream, clock = reporter(interval=10)
        progress.phase(1)
        progress(RequestStarted("get_plan/900", 1, 7, 100))
        clock.advance(10)
        progress.tick()
        self.assertIn("waiting 10s: get_plan/900", stream.getvalue())
        self.assertIn("API 7/100", stream.getvalue())

        clock.advance(5)
        progress.tick()
        self.assertIn("Slow response: still waiting for TestRail (get_plan/900, 15s)", stream.getvalue())
        clock.advance(29)
        progress.tick()
        self.assertEqual(stream.getvalue().count("Slow response"), 1)
        clock.advance(1)
        progress.tick()
        self.assertIn("(get_plan/900, 45s)", stream.getvalue())

        progress(RequestFinished("get_plan/900", 200))
        clock.advance(20)
        progress.tick()
        self.assertNotIn("waiting", stream.getvalue().splitlines()[-1])

    def test_retry_wait_is_reported_with_a_countdown(self):
        progress, stream, clock = reporter(interval=5)
        progress.phase(1)
        progress(RetryScheduled("get_runs/1", "rate limited (HTTP 429)", 12.0, 2, 4))
        self.assertIn("Rate limited (HTTP 429): retrying get_runs/1 in 12s (attempt 2 of 4)", stream.getvalue())
        clock.advance(5)
        progress.tick()
        self.assertIn("rate limited (HTTP 429), retry in 7s: get_runs/1", stream.getvalue().splitlines()[-1])

    def test_plain_status_lines_are_throttled(self):
        progress, stream, clock = reporter(interval=15)
        progress.phase(2)
        for _ in range(29):
            progress(DiscoveryProgress(plans_fetched=1, runs_found=2, runs_included=1))
            clock.advance(1)
            progress.tick()
        lines = stream.getvalue().splitlines()

        self.assertEqual(len(lines), 2)  # phase line, then one status line after 15 s
        self.assertIn("plans fetched 1, runs found 2 (included 1)", lines[1])
        self.assertNotIn("/", lines[1].split("|")[2])  # no invented total for discovery
        self.assertNotIn("\r", stream.getvalue())

    def test_tty_redraws_one_status_line(self):
        progress, stream, clock = reporter(tty=True)
        progress.phase(3)
        progress(CollectionProgress("results of the other runs", 4, 10, 4, 1, 120))
        for _ in range(3):
            clock.advance(1)
            progress.tick()
        output = stream.getvalue()

        self.assertEqual(output.count("\r\x1b[K"), 3)
        self.assertIn("results of the other runs: 4/10 runs; runs with results 4, skipped 1", output)
        progress(RetryScheduled("get_tests/5", "HTTP 503", 1.0, 2, 3))  # a permanent line clears the status first
        self.assertTrue(stream.getvalue().endswith("(attempt 2 of 3)\n"))
        self.assertIn("\r\x1b[K[3s] HTTP 503: retrying", stream.getvalue())

    def test_tty_status_line_is_truncated_to_the_terminal_width(self):
        stream = io.StringIO()
        progress = ConsoleProgress(stream, ["Collection"], tty=True, clock=FakeClock(), width=lambda: 60)
        progress.phase(1)
        progress(CollectionProgress("x" * 200, 1, 2, 1, 0, 5))
        stream.truncate(0)
        stream.seek(0)
        progress.tick()
        line = stream.getvalue().removeprefix("\r\x1b[K")
        self.assertEqual(len(line), 59)
        self.assertTrue(line.endswith("..."))

    def test_budget_warnings(self):
        progress, stream, _ = reporter()
        progress.phase(3)
        for made in range(1, 21):
            progress(RequestStarted("get_tests/1", 1, made, 20))
        progress(BudgetExhausted(20, 20))
        progress(BudgetExhausted(20, 20))
        output = stream.getvalue()

        self.assertIn("Request budget 80% used (16/20)", output)
        self.assertIn("Request budget 95% used (19/20)", output)
        self.assertEqual(output.count("Request budget exhausted (20/20)"), 1)

    def test_partial_and_failed_runs_never_report_completion(self):
        cases = [
            (Outcome.PARTIAL, None, "Finished with PARTIAL coverage", "all 3 phases ran; request budget exhausted"),
            (Outcome.FAILED, "TestRail API error X", "Failed during phase 3/3 (Collection)", "TestRail API error X"),
            (Outcome.INTERRUPTED, None, "Interrupted during phase 3/3", ""),
        ]
        for outcome, detail, expected, extra in cases:
            with self.subTest(outcome=outcome):
                progress, stream, _ = reporter()
                progress.phase(3)
                progress(BudgetExhausted(20, 20))
                progress.finish(outcome, detail)
                final = stream.getvalue().splitlines()[-1]
                self.assertIn(expected, final)
                self.assertIn(extra, final)
                self.assertNotIn("Completed", stream.getvalue())
                self.assertNotIn("100%", stream.getvalue())

    def test_early_stop_names_the_last_phase(self):
        progress, stream, _ = reporter(phases=("A", "B", "C", "D"))
        progress.phase(2)
        progress.finish(Outcome.NOTHING_TO_COLLECT)
        self.assertIn("Stopped after phase 2/4 (B)", stream.getvalue())
        self.assertIn("nothing to collect", stream.getvalue())

    def test_events_after_finish_are_ignored(self):
        progress, stream, _ = reporter()
        progress.phase(1)
        progress.finish(Outcome.COMPLETED)
        before = stream.getvalue()
        progress(RetryScheduled("get_case/1", "timeout", 1.0, 2, 2))
        progress.tick()
        progress.finish(Outcome.FAILED)
        self.assertEqual(stream.getvalue(), before)

    def test_null_progress_prints_nothing(self):
        progress = NullProgress()
        progress.start()
        progress.phase(1)
        progress(RequestStarted("get_case/1", 1, 1, 1))
        progress.tick()
        progress.finish(Outcome.FAILED, "x")  # nothing to assert beyond not raising


class HeartbeatTests(unittest.TestCase):
    def test_output_continues_while_a_request_is_blocked(self):
        seen = threading.Event()

        class Stream(io.StringIO):
            def write(self, text):
                if "waiting" in text and "get_case/1" in text:
                    seen.set()
                return super().write(text)

        stream = Stream()
        progress = ConsoleProgress(stream, ["Setup"], tty=True, tick_seconds=0.01, width=lambda: 200)

        class BlockingTransport:
            def get(self, url, headers, *, connect_timeout, read_timeout):
                # The response is released only after the heartbeat has reported the pending request.
                if not seen.wait(timeout=5):
                    raise AssertionError("no progress while the request was pending")
                return ok({"id": 1})

        settings = fx.settings()
        client = TestRailClient(settings, FakeCredentialProvider(), BlockingTransport(), progress=progress)
        progress.phase(1)
        progress.start()
        try:
            self.assertEqual(client.get("get_case/1"), {"id": 1})
        finally:
            progress.finish(Outcome.COMPLETED)
        self.assertTrue(seen.is_set())
        self.assertIn("Completed all 1 phases", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
