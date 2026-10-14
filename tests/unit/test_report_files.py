import os
import stat
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from testrail_stability_analyzer.export import report_files
from testrail_stability_analyzer.export.report_files import (
    ReportFileError,
    ReportTarget,
    authorize_overwrite,
    report_basename,
    report_scope,
    target_problem,
    write_reports,
)

MOMENT = datetime(2026, 10, 13, 16, 49, 0, tzinfo=UTC)
BASE = "stability-project-p105-s50343-90d-20261013-164900"


class NameTests(unittest.TestCase):
    def test_scopes(self):
        cases = [
            ("project", {}, "project"),
            ("plan", {"plan_ids": (1108517,)}, "plan-1108517"),
            ("plan", {"plan_ids": (1, 2), "run_ids": (5,)}, "plan-1_2-run-5"),
            ("run", {"run_ids": (5,), "plan_ids": (1,)}, "run-5-plan-1"),
            ("run", {"run_ids": (5, 6, 7, 8, 9)}, "run-5_6_7+2more"),
            ("plan", {"plan_ids": (3, 3)}, "plan-3"),
            ("sample", {}, "sample"),
            ("sample", {"sample_plan_id": 900}, "sample-plan-900"),
        ]
        for target, ids, expected in cases:
            with self.subTest(target=target, ids=ids):
                self.assertEqual(report_scope(target, **ids), expected)

    def test_basename_uses_utc(self):
        self.assertEqual(report_basename("project", 105, 50343, 90, MOMENT), BASE)
        local = MOMENT.astimezone(timezone(timedelta(hours=2)))  # 18:49 local time
        self.assertEqual(report_basename("project", 105, 50343, 90, local), BASE)
        self.assertEqual(
            report_basename("plan-1108517", 105, None, 60, MOMENT), "stability-plan-1108517-p105-60d-20261013-164900"
        )


class FileTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def auto(self, label="XLSX", extension=".xlsx"):
        return ReportTarget.parse(label, f"{self.dir}/", extension)

    def names(self):
        return sorted(path.name for path in self.dir.iterdir())


class TargetTests(FileTestCase):
    def test_directory_detection(self):
        self.assertTrue(ReportTarget.parse("XML", str(self.dir), ".xml").automatic)  # existing directory
        self.assertTrue(ReportTarget.parse("XML", f"{self.dir}/new/", ".xml").automatic)  # trailing separator
        self.assertFalse(ReportTarget.parse("XML", str(self.dir / "r.xml"), ".xml").automatic)

    def test_directory_decision_survives_the_directory_disappearing(self):
        directory = self.dir / "reports"
        directory.mkdir()
        target = ReportTarget.parse("XML", str(directory), ".xml")  # existing directory, no trailing separator
        self.assertIsNone(target_problem(target, overwrite=True))
        directory.rmdir()  # removed during collection

        self.assertTrue(target.automatic)
        self.assertIn("does not exist", target_problem(target, overwrite=True))
        with self.assertRaises(ReportFileError):
            write_reports([(target, b"x")], BASE)
        self.assertFalse(directory.exists())  # no report file at the directory's former path

    def test_problems(self):
        existing = self.dir / "r.xml"
        existing.write_text("old")
        cases = [
            (f"{self.dir}/missing/", False, "does not exist"),
            (str(self.dir / "missing" / "r.xml"), False, "does not exist"),
            (f"{existing}/", False, "does not exist"),  # a file named as a directory
            (str(existing), False, "already exists; pass --overwrite-report"),
            (str(existing), True, None),
            (str(self.dir), False, None),
            (str(self.dir / "new.xml"), False, None),
        ]
        for raw, overwrite, expected in cases:
            with self.subTest(raw=raw, overwrite=overwrite):
                problem = target_problem(ReportTarget.parse("XML", raw, ".xml"), overwrite=overwrite)
                if expected is None:
                    self.assertIsNone(problem)
                else:
                    self.assertIn(expected, problem)

    def test_dangling_symlink_counts_as_existing(self):
        link = self.dir / "r.xml"
        link.symlink_to(self.dir / "nowhere.xml")
        self.assertIn("already exists", target_problem(ReportTarget.parse("XML", str(link), ".xml"), overwrite=False))

    @unittest.skipIf(os.geteuid() == 0, "root can write anywhere")
    def test_unwritable_directory(self):
        locked = self.dir / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        for raw in (str(locked), str(locked / "r.xml")):
            with self.subTest(raw=raw):
                self.assertIn("not writable", target_problem(ReportTarget.parse("XML", raw, ".xml"), overwrite=False))


class WriteTests(FileTestCase):
    def test_automatic_names_are_unique_within_one_second(self):
        first = write_reports([(self.auto(), b"1")], BASE)
        second = write_reports([(self.auto(), b"2")], BASE)
        third = write_reports([(self.auto(), b"3")], BASE)  # never reuses a generated name

        self.assertEqual([path.name for _, path in first + second + third],
                         [f"{BASE}.xlsx", f"{BASE}-2.xlsx", f"{BASE}-3.xlsx"])  # fmt: skip
        self.assertEqual([path.read_bytes() for _, path in first + second + third], [b"1", b"2", b"3"])
        self.assertTrue(all(path.is_absolute() for _, path in first + second))

    def test_formats_share_one_basename_and_suffix(self):
        (self.dir / f"{BASE}.xml").write_text("someone else's report")
        written = write_reports([(self.auto("XML", ".xml"), b"x"), (self.auto(), b"y")], BASE)

        self.assertEqual([(label, path.name) for label, path in written],
                         [("XML", f"{BASE}-2.xml"), ("XLSX", f"{BASE}-2.xlsx")])  # fmt: skip
        self.assertEqual((self.dir / f"{BASE}.xml").read_text(), "someone else's report")
        self.assertNotIn(f"{BASE}.xlsx", self.names())  # the free single-format name is not used

    def test_new_files_are_private(self):
        ((_, path),) = write_reports([(self.auto(), b"x")], BASE)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_explicit_file_is_never_overwritten_without_permission(self):
        explicit = self.dir / "mine.xml"
        explicit.write_text("old")
        target = ReportTarget.parse("XML", str(explicit), ".xml")
        with self.assertRaises(ReportFileError) as ctx:
            write_reports([(target, b"new")], BASE)
        self.assertIn("already exists", str(ctx.exception))
        self.assertEqual(explicit.read_text(), "old")

        ((_, path),) = write_reports([(authorize_overwrite(target, overwrite=True), b"new")], BASE)
        self.assertEqual((path, explicit.read_text()), (explicit, "new"))
        self.assertEqual(self.names(), ["mine.xml"])  # no temporary file left

    def test_overwrite_never_replaces_a_file_absent_before_collection(self):
        explicit = self.dir / "mine.xml"
        target = authorize_overwrite(ReportTarget.parse("XML", str(explicit), ".xml"), overwrite=True)
        explicit.write_text("written by someone else")  # appears during collection
        with self.assertRaises(ReportFileError) as ctx:
            write_reports([(target, b"new")], BASE)
        self.assertIn("already exists", str(ctx.exception))
        self.assertEqual(explicit.read_text(), "written by someone else")

    def test_overwrite_keeps_a_file_changed_during_collection(self):
        explicit = self.dir / "mine.xml"
        explicit.write_text("old")
        target = authorize_overwrite(ReportTarget.parse("XML", str(explicit), ".xml"), overwrite=True)
        replacement = self.dir / "other.xml"
        replacement.write_text("written by someone else")
        replacement.replace(explicit)  # another file now has the name
        with self.assertRaises(ReportFileError) as ctx:
            write_reports([(target, b"new")], BASE)
        self.assertIn("changed while the collection ran", str(ctx.exception))
        self.assertEqual(explicit.read_text(), "written by someone else")

    def test_overwrite_creates_a_file_removed_during_collection(self):
        explicit = self.dir / "mine.xml"
        explicit.write_text("old")
        target = authorize_overwrite(ReportTarget.parse("XML", str(explicit), ".xml"), overwrite=True)
        explicit.unlink()
        ((_, path),) = write_reports([(target, b"new")], BASE)
        self.assertEqual((path, explicit.read_text()), (explicit, "new"))

    def test_overwrite_is_not_authorized_for_automatic_names_or_without_the_option(self):
        explicit = self.dir / "mine.xml"
        explicit.write_text("old")
        target = ReportTarget.parse("XML", str(explicit), ".xml")
        self.assertIsNone(authorize_overwrite(target, overwrite=False).replaces)
        self.assertIsNone(authorize_overwrite(self.auto(), overwrite=True).replaces)
        self.assertIsNotNone(authorize_overwrite(target, overwrite=True).replaces)

    def test_symlink_is_not_followed(self):
        victim = self.dir / "victim.txt"
        victim.write_text("keep")
        link = self.dir / "r.xml"
        link.symlink_to(victim)
        with self.assertRaises(ReportFileError):
            write_reports([(ReportTarget.parse("XML", str(link), ".xml"), b"x")], BASE)
        self.assertEqual(victim.read_text(), "keep")

    def test_failure_keeps_written_reports_and_removes_reserved_ones(self):
        (self.dir / "taken.xlsx").write_text("old")
        ok = ReportTarget.parse("XML", str(self.dir / "fresh.xml"), ".xml")
        taken = ReportTarget.parse("XLSX", str(self.dir / "taken.xlsx"), ".xlsx")
        automatic = ReportTarget.parse("XML", f"{self.dir}/auto/", ".xml")
        (self.dir / "auto").mkdir()
        with self.assertRaises(ReportFileError) as ctx:
            write_reports([(ok, b"1"), (taken, b"2"), (automatic, b"3")], BASE)

        self.assertEqual([label for label, _ in ctx.exception.written], ["XML"])
        self.assertIn("could not write the XLSX report", str(ctx.exception))
        self.assertEqual(sorted(p.name for p in (self.dir / "auto").iterdir()), [])  # reservation removed
        self.assertEqual((self.dir / "fresh.xml").read_bytes(), b"1")


class OwnedCleanupTests(FileTestCase):
    """Cleanup removes only files this process still owns; synthetic paths only."""

    @staticmethod
    def take_over(path):
        """Another process removes the file and puts its own report under the same name."""
        path.unlink()
        path.write_text("other report")

    def test_collision_cleanup_keeps_a_recreated_earlier_reservation(self):
        xlsx_dir, xml_dir = self.dir / "x", self.dir / "m"
        xlsx_dir.mkdir()
        xml_dir.mkdir()
        (xml_dir / f"{BASE}.xml").write_text("taken")  # the later file collides
        original = report_files._Created.create

        def create(path):
            created = original(path)
            if path.name == f"{BASE}.xlsx":
                self.take_over(path)
            return created

        targets = [ReportTarget.parse("XLSX", f"{xlsx_dir}/", ".xlsx"), ReportTarget.parse("XML", f"{xml_dir}/", ".xml")]
        with mock.patch.object(report_files._Created, "create", side_effect=create):
            written = write_reports([(target, b"r") for target in targets], BASE)

        self.assertEqual((xlsx_dir / f"{BASE}.xlsx").read_text(), "other report")
        self.assertEqual([path.name for _, path in written], [f"{BASE}-2.xlsx", f"{BASE}-2.xml"])

    def test_unwritten_reservation_is_kept_when_recreated_by_another_process(self):
        (self.dir / "taken.xml").write_text("old")
        explicit = ReportTarget.parse("XML", str(self.dir / "taken.xml"), ".xml")
        original = report_files._reserve

        def reserve(targets, basename):
            reserved = original(targets, basename)
            for created in reserved.values():
                self.take_over(created.path)
            return reserved

        with mock.patch.object(report_files, "_reserve", side_effect=reserve):
            with self.assertRaises(ReportFileError):
                write_reports([(explicit, b"x"), (self.auto(), b"y")], BASE)

        self.assertEqual((self.dir / f"{BASE}.xlsx").read_text(), "other report")

    def test_failed_write_removes_only_its_own_file(self):
        path = self.dir / "mine.xml"
        target = ReportTarget.parse("XML", str(path), ".xml")
        with mock.patch.object(report_files.os, "write", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(ReportFileError):
                write_reports([(target, b"x")], BASE)
        self.assertFalse(path.exists())

        def write(fd, data):
            self.take_over(path)
            raise OSError(28, "No space left on device")

        with mock.patch.object(report_files.os, "write", side_effect=write):
            with self.assertRaises(ReportFileError):
                write_reports([(target, b"x")], BASE)
        self.assertEqual(path.read_text(), "other report")

    def test_takeover_while_writing_is_not_reported_as_written(self):
        for target in (ReportTarget.parse("XML", str(self.dir / "mine.xml"), ".xml"), self.auto()):
            with self.subTest(automatic=target.automatic):
                original = report_files._Created.create

                def create(path):
                    created = original(path)
                    self.take_over(path)  # the other process replaces the name before the write finishes
                    return created

                with mock.patch.object(report_files._Created, "create", side_effect=create):
                    with self.assertRaises(ReportFileError) as ctx:
                        write_reports([(target, b"mine")], BASE)
                self.assertIn("replaced by another file while writing", str(ctx.exception))
                self.assertEqual(ctx.exception.written, ())
                for path in self.dir.iterdir():
                    self.assertEqual(path.read_text(), "other report")
                    path.unlink()

    def test_overwrite_rechecks_the_file_immediately_before_the_rename(self):
        explicit = self.dir / "mine.xml"
        explicit.write_text("old")
        target = authorize_overwrite(ReportTarget.parse("XML", str(explicit), ".xml"), overwrite=True)
        real = tempfile.NamedTemporaryFile

        def temporary(*args, **kwargs):
            self.take_over(explicit)  # replaced after the first check, while the report is being written
            return real(*args, **kwargs)

        with mock.patch.object(report_files.tempfile, "NamedTemporaryFile", side_effect=temporary):
            with self.assertRaises(ReportFileError) as ctx:
                write_reports([(target, b"new")], BASE)
        self.assertIn("changed while the collection ran", str(ctx.exception))
        self.assertEqual(explicit.read_text(), "other report")
        self.assertEqual(self.names(), ["mine.xml"])  # temporary file removed


if __name__ == "__main__":
    unittest.main()
