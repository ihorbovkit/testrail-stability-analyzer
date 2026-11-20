"""Overview Pass Rate notes show the execution history of the window, oldest to newest.

Verified on the generated XLSX workbook. All data is synthetic.
"""

import io
import re
import unittest
import zipfile
from datetime import timedelta

from testrail_stability_analyzer.analysis import Rate, analyze
from testrail_stability_analyzer.export import render_xlsx

from . import fake_testrail as fx
from .analysis_fixtures import APP_A, APP_B, PLAT_X, PLAT_Y, histories, outcome, rec, report, runs
from .fake_testrail import (
    AUTOMATION_ISSUE,
    BLOCKED,
    FAILED,
    KNOWN_ISSUE,
    MYSTERY,
    NOT_APPLICABLE,
    PASSED,
    RETEST,
    UNTESTED,
)
from .report_fixtures import xlsx_rows_by_case

try:
    import openpyxl
except ImportError:  # pragma: no cover - exercised only without the optional dependency
    openpyxl = None

HEADING = "Recent executions (oldest → newest):"
COMBINATIONS = ((APP_A, PLAT_X), (APP_A, PLAT_Y), (APP_B, PLAT_X), (APP_B, PLAT_Y))


def xlsx(outcomes, *, max_runs=10):
    hist = histories(outcomes, max_runs=max_runs, platforms=(PLAT_X, PLAT_Y))
    collected = report(outcomes, hist=hist)
    settings = fx.settings()
    analysis = analyze(collected, settings)
    return render_xlsx(collected, analysis, settings), analysis


def workbook(outcomes, **kwargs):
    data, analysis = xlsx(outcomes, **kwargs)
    return openpyxl.load_workbook(io.BytesIO(data)), analysis


def note_sizes(data):
    """(width, height) in px of every note box, read from the workbook's VML drawing."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        vml = "".join(archive.read(name).decode() for name in archive.namelist() if name.endswith(".vml"))
    return [(int(w), int(h)) for w, h in re.findall(r"width:(\d+)px;height:(\d+)px", vml)]


def rate_cell(outcomes, app="App A", platform="Platform X", *, case_id=100, **kwargs):
    wb, analysis = workbook(outcomes, **kwargs)
    _, rows = xlsx_rows_by_case(wb["Stability Overview"])
    return rows[case_id][f"{app} {platform}: Pass Rate"], analysis


def note(outcomes, *args, **kwargs):
    cell, _ = rate_cell(outcomes, *args, **kwargs)
    return cell.comment.text.splitlines()


@unittest.skipUnless(openpyxl, "openpyxl is not installed")
class ExecutionSequenceTests(unittest.TestCase):
    def test_mixed_history_oldest_to_newest(self):
        # runs() takes statuses newest first; the note shows them oldest first.
        newest_first = [KNOWN_ISSUE, AUTOMATION_ISSUE, AUTOMATION_ISSUE, FAILED, FAILED, PASSED, PASSED, PASSED]
        self.assertEqual(
            note(runs(newest_first)),
            [HEADING, "P P P F F A A K", "8 execution(s); latest: Known Issue"],
        )

    def test_latest_execution_is_rightmost(self):
        lines = note(runs([PASSED] + [FAILED] * 5))
        self.assertEqual(lines[1], "F F F F F P")
        self.assertEqual(lines[-1], "6 execution(s); latest: Passed")

    def test_order_follows_result_timestamps_not_run_ids(self):
        # The higher run ID holds the older result.
        outcomes = [outcome(600, 100, rec(1, FAILED, 9)), outcome(500, 100, rec(2, KNOWN_ISSUE, 1))]
        outcomes += runs([PASSED] * 3, first_run=700, case_id=100)  # 1-3 days ago
        self.assertEqual(note(outcomes)[1], "F P P K P")  # K (1 day) and P (1 day) tie: higher run ID last

    def test_other_statuses_have_distinct_codes(self):
        lines = note(runs([BLOCKED, RETEST, UNTESTED, NOT_APPLICABLE, PASSED]))
        self.assertEqual(lines[1], "P X N R B")
        self.assertEqual(lines[2], "5 execution(s); latest: Blocked")  # no legend lines

    def test_unknown_status_is_a_question_mark(self):
        lines = note(runs([MYSTERY, PASSED]))
        self.assertEqual(lines[1], "P ?")
        self.assertEqual(lines[-2], "2 execution(s); latest: Unknown")

    def test_retries_within_a_run_count_once_with_their_final_status(self):
        retried = outcome(500, 100, rec(1, FAILED, 2), rec(2, PASSED, 1.9))
        self.assertEqual(note([retried, *runs([KNOWN_ISSUE], first_run=600)])[1], "P K")

    def test_note_does_not_repeat_other_metrics(self):
        text = "\n".join(note(runs([PASSED] * 8 + [FAILED])))
        for repeated in ("Pass rate", "Diagnosis", "Confidence", "Coverage"):
            self.assertNotIn(repeated, text)


@unittest.skipUnless(openpyxl, "openpyxl is not installed")
class EmptyHistoryTests(unittest.TestCase):
    def test_no_execution_in_the_window_names_the_latest_known_result(self):
        old = [outcome(500, 100, rec(1, KNOWN_ISSUE, 70))]  # 70 days ago: outside the 60-day window
        date = f"{fx.NOW - timedelta(days=70):%Y-%m-%d}"
        self.assertEqual(
            note(old),
            ["No executions in the history window.", f"Latest known result: Known Issue on {date}, before the window."],
        )

    def test_combination_without_history(self):
        lines = note(runs([PASSED] * 5), "App B", "Platform Y")
        no_run = "No collected run of this application/platform included the case."
        self.assertEqual(lines, ["No executions in the history window.", no_run])


@unittest.skipUnless(openpyxl, "openpyxl is not installed")
class WindowSizeTests(unittest.TestCase):
    def test_executions_before_the_window_start_are_not_shown(self):
        outcomes = [*runs([PASSED] * 3), outcome(900, 100, rec(9, FAILED, 70))]
        self.assertEqual(note(outcomes)[1], "P P P")

    def test_capped_window_shows_the_analyzed_executions(self):
        lines = note(runs([KNOWN_ISSUE] + [PASSED] * 11))  # 12 runs, max_runs 10
        self.assertEqual(lines[1], "P P P P P P P P P K")
        self.assertEqual(lines[-1], "latest 10 of 12 executions (max runs); latest: Known Issue")

    def test_max_runs_is_respected(self):
        self.assertEqual(note(runs([PASSED, FAILED, PASSED, FAILED]), max_runs=3)[1], "P F P")

    def test_long_history_wraps_without_truncation(self):
        statuses = [KNOWN_ISSUE] + [PASSED, FAILED] * 57  # 115 runs, three a day, all within the 60-day window
        outcomes = [outcome(500 + i, 100, rec(5000 + i, status, (i + 1) / 3)) for i, status in enumerate(statuses)]
        data, _ = xlsx(outcomes, max_runs=115)
        _, rows = xlsx_rows_by_case(openpyxl.load_workbook(io.BytesIO(data))["Stability Overview"])
        lines = rows[100]["App A Platform X: Pass Rate"].comment.text.splitlines()
        self.assertEqual([len(line.split()) for line in lines[1:7]], [20, 20, 20, 20, 20, 15])
        codes = " ".join(lines[1:7]).split()
        self.assertEqual((len(codes), codes[0], codes[-1]), (115, "F", "K"))
        self.assertEqual(lines[-1], "115 execution(s); latest: Known Issue")
        tall = 16 * len(lines) + 16
        self.assertGreater(tall, 130)
        self.assertIn((260, tall), note_sizes(data))  # the note box grows with the lines

    def test_short_notes_keep_the_default_size(self):
        data, _ = xlsx(runs([PASSED] * 5))
        sizes = note_sizes(data)
        self.assertTrue(sizes)
        self.assertEqual(set(sizes), {(260, 130)})


@unittest.skipUnless(openpyxl, "openpyxl is not installed")
class CombinationTests(unittest.TestCase):
    def test_every_application_platform_has_its_own_history(self):
        sequences = {
            (APP_A, PLAT_X): [PASSED, FAILED],
            (APP_A, PLAT_Y): [KNOWN_ISSUE, PASSED, PASSED],
            (APP_B, PLAT_X): [AUTOMATION_ISSUE],
            (APP_B, PLAT_Y): [BLOCKED, KNOWN_ISSUE, FAILED, PASSED],
        }
        outcomes = []
        for index, ((app, platform), statuses) in enumerate(sequences.items()):
            outcomes += runs(statuses, first_run=500 + 100 * index, app=app, platform=platform)
        wb, _ = workbook(outcomes)
        _, rows = xlsx_rows_by_case(wb["Stability Overview"])
        expected = {
            (APP_A, PLAT_X): "F P",
            (APP_A, PLAT_Y): "P P K",
            (APP_B, PLAT_X): "A",
            (APP_B, PLAT_Y): "P F K B",
        }
        self.assertEqual(set(expected), set(COMBINATIONS))
        for (app, platform), sequence in expected.items():
            with self.subTest(app=app.name, platform=platform.name):
                cell = rows[100][f"{app.name} {platform.name}: Pass Rate"]
                self.assertEqual(cell.comment.text.splitlines()[:2], [HEADING, sequence])

    def test_latest_status_notes_are_unchanged(self):
        wb, _ = workbook(runs([PASSED] * 5))
        _, rows = xlsx_rows_by_case(wb["Stability Overview"])
        status = rows[100]["App A Platform X: Latest Status"].comment.text.splitlines()
        self.assertEqual(
            [line.split(":", 1)[0] for line in status],
            ["Diagnosis", "Confidence", "Executions", "Pass rate", "Coverage", "Latest result"],
        )


@unittest.skipUnless(openpyxl, "openpyxl is not installed")
class NoRegressionTests(unittest.TestCase):
    def test_pass_rate_value_colour_and_diagnosis_are_unchanged(self):
        cell, analysis = rate_cell(runs([KNOWN_ISSUE] * 2 + [PASSED] * 8))
        (key,) = [a for a in analysis.analyses if a.key.platform == PLAT_X]
        self.assertEqual(key.metrics.pass_rate, Rate(8, 10))
        self.assertEqual(cell.value, 0.8)
        self.assertEqual(cell.fill.fgColor.rgb[-6:], "FFEB9C")  # yellow band
        self.assertEqual(key.diagnosis.value, "inconsistent")

    def test_grey_explanation_is_kept(self):
        lines = note(runs([PASSED, FAILED]))
        self.assertEqual(lines[-1], "Grey: fewer than 5 conclusive executions")


if __name__ == "__main__":
    unittest.main()
