import unittest
from datetime import UTC, datetime

from testrail_stability_analyzer.models import (
    Case,
    CaseType,
    CustomField,
    ModelError,
    Plan,
    Project,
    Result,
    Run,
    Status,
    Suite,
    Test,
)

from . import payloads

T0_UTC = datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


class MetadataModelTests(unittest.TestCase):
    def test_project(self):
        project = Project.from_api(payloads.project())
        self.assertEqual((project.id, project.name, project.suite_mode), (1, "Example Project", 3))
        self.assertIs(project.is_completed, False)
        self.assertIsNone(project.completed_on)
        self.assertEqual(dict(project.extra), {"show_announcement": False})

    def test_suite(self):
        suite = Suite.from_api(payloads.suite(is_completed=True, completed_on=payloads.T0))
        self.assertEqual((suite.id, suite.project_id, suite.name), (10, 1, "Regression"))
        self.assertEqual(suite.completed_on, T0_UTC)

    def test_case_type(self):
        case_type = CaseType.from_api({"id": 3, "name": "Automated", "is_default": False})
        self.assertEqual((case_type.id, case_type.name, case_type.is_default), (3, "Automated", False))

    def test_status(self):
        status = Status.from_api(payloads.status(6, "custom_status1", "Automation Problem", is_system=False))
        self.assertEqual((status.id, status.name, status.label), (6, "custom_status1", "Automation Problem"))
        self.assertIs(status.is_system, False)
        self.assertEqual((status.color_dark, status.color_medium, status.color_bright), (1, 2, 3))

    def test_custom_case_field(self):
        field = CustomField.from_api(payloads.case_field())
        self.assertEqual((field.id, field.system_name, field.label, field.type_id), (20, "custom_component", "Component", 6))
        self.assertEqual(field.template_ids, (1, 2))
        config = field.configs[0]
        self.assertEqual((config.id, config.is_global, config.project_ids), ("abc-123", False, (1, 3)))
        self.assertEqual((config.is_required, config.default_value), (False, "1"))
        self.assertEqual(config.items(), {1: "Login", 2: "Checkout"})
        self.assertTrue(field.applies_to_project(3))
        self.assertFalse(field.applies_to_project(2))
        self.assertEqual(dict(field.extra), {"entity_id": 1})
        self.assertEqual(dict(config.context), {"is_global": False, "project_ids": [1, 3]})
        self.assertEqual(dict(config.extra), {})

    def test_custom_field_config_preserves_unknown_keys(self):
        config = {"id": "c", "context": {"is_global": True, "scope": "new"}, "options": {}, "future": 1}
        parsed = CustomField.from_api(payloads.case_field(configs=[config])).configs[0]
        self.assertEqual(dict(parsed.extra), {"future": 1})
        self.assertEqual(parsed.context["scope"], "new")

    def test_custom_result_field_global_without_items(self):
        field = CustomField.from_api(
            payloads.case_field(
                system_name="custom_environment",
                type_id=1,
                configs=[{"id": "x", "context": {"is_global": True, "project_ids": None}, "options": {}}],
            )
        )
        self.assertTrue(field.applies_to_project(42))
        self.assertEqual(field.configs[0].items(), {})

    def test_malformed_dropdown_items(self):
        field = CustomField.from_api(
            payloads.case_field(configs=[{"context": {}, "options": {"items": "1, Ok\nnot-an-item"}}])
        )
        with self.assertRaises(ModelError):
            field.configs[0].items()

    def test_field_without_configs(self):
        field = CustomField.from_api({"id": 1, "system_name": "custom_x"})
        self.assertEqual(field.configs, ())
        self.assertFalse(field.applies_to_project(1))


class CaseModelTests(unittest.TestCase):
    def test_case_with_custom_and_unknown_fields(self):
        case = Case.from_api(payloads.case(future_field={"nested": True}))
        self.assertEqual((case.id, case.title, case.suite_id, case.section_id), (100, "Case 100", 10, 30))
        self.assertEqual((case.created_on, case.updated_on.timestamp()), (T0_UTC, payloads.T0 + 60))
        self.assertEqual(
            dict(case.custom_fields), {"custom_component": 2, "custom_automation_id": "suite.module.test_login"}
        )
        self.assertEqual(dict(case.extra), {"display_order": 1, "is_deleted": 0, "future_field": {"nested": True}})

    def test_minimal_case(self):
        case = Case.from_api({"id": 1, "title": "Only required fields"})
        self.assertIsNone(case.suite_id)
        self.assertIsNone(case.created_on)
        self.assertEqual(dict(case.custom_fields), {})
        self.assertEqual(dict(case.extra), {})

    def test_mappings_are_read_only(self):
        case = Case.from_api(payloads.case())
        with self.assertRaises(TypeError):
            case.custom_fields["custom_component"] = 1  # type: ignore[index]


class RunAndPlanModelTests(unittest.TestCase):
    def test_standalone_run(self):
        run = Run.from_api(payloads.run())
        self.assertTrue(run.is_standalone)
        self.assertIsNone(run.plan_id)
        self.assertEqual(run.created_on, T0_UTC)
        self.assertEqual(
            dict(run.status_counts),
            {"passed": 3, "blocked": 0, "untested": 1, "retest": 0, "failed": 1, "custom_status1": 2, "custom_status2": 0},
        )
        self.assertEqual(dict(run.extra), {})

    def test_plan_summary_without_entries(self):
        plan = Plan.from_api(payloads.plan())
        self.assertEqual(plan.entries, ())
        self.assertEqual(plan.runs, ())
        self.assertEqual(plan.status_counts["custom_status1"], 1)

    def test_plan_with_multiple_configuration_runs(self):
        # Two runs of one entry, one per configuration; configuration names are opaque.
        entry = payloads.plan_entry("entry-a", 900, [(501, "Config One", 11), (502, "Config Two", 12)])
        single = payloads.plan_entry("entry-b", 900, [(503, None, 13)])
        plan = Plan.from_api(payloads.plan(entries=[entry, single]))

        self.assertEqual([e.id for e in plan.entries], ["entry-a", "entry-b"])
        self.assertEqual([run.id for run in plan.runs], [501, 502, 503])
        first = plan.runs[0]
        self.assertEqual((first.plan_id, first.entry_id, first.config, first.config_ids), (900, "entry-a", "Config One", (11,)))
        self.assertFalse(first.is_standalone)

    def test_plan_entry_runs_inherit_missing_relationship_ids(self):
        entry = {"id": "entry-x", "runs": [{"id": 1, "name": "Nested run"}]}
        plan = Plan.from_api(payloads.plan(entries=[entry]))
        self.assertEqual((plan.runs[0].plan_id, plan.runs[0].entry_id), (900, "entry-x"))


class TestAndResultModelTests(unittest.TestCase):
    def test_test_instance(self):
        test = Test.from_api(payloads.test())
        self.assertEqual((test.id, test.case_id, test.run_id, test.status_id), (7000, 100, 500, 5))
        self.assertEqual(dict(test.custom_fields), {"custom_automation_id": "suite.module.test_login"})
        self.assertEqual(dict(test.extra), {"estimate_forecast": None})

    def test_result(self):
        result = Result.from_api(payloads.result(), run_id=500)
        self.assertEqual((result.id, result.test_id, result.status_id, result.run_id), (80000, 7000, 5, 500))
        self.assertEqual(result.created_on, datetime(2023, 11, 14, 22, 15, 20, tzinfo=UTC))
        self.assertEqual((result.version, result.elapsed), ("1.2.3", "1m 5s"))
        self.assertEqual(result.comment, "Synthetic failure message")
        self.assertEqual(dict(result.custom_fields), {"custom_environment": "staging"})
        self.assertIsNone(result.case_id)

    def test_result_repr_excludes_free_text(self):
        text = repr(Result.from_api(payloads.result()))
        self.assertNotIn("Synthetic failure message", text)
        self.assertNotIn("staging", text)
        self.assertIn("test_id=7000", text)

    def test_comment_only_result_has_no_status(self):
        result = Result.from_api(payloads.result(status_id=None, comment="triage note"))
        self.assertIsNone(result.status_id)

    def test_api_relationship_ids_take_precedence_over_context(self):
        result = Result.from_api(payloads.result(case_id=101), run_id=500, case_id=999)
        self.assertEqual(result.case_id, 101)


class MalformedPayloadTests(unittest.TestCase):
    def test_rejects_malformed_objects(self):
        cases = [
            (Project, "not an object"),
            (Project, {"name": "missing id"}),
            (Project, {"id": "1", "name": "string id"}),
            (Project, {"id": True, "name": "bool id"}),
            (Project, {"id": 0, "name": "zero id"}),
            (Project, {"id": -1, "name": "negative id"}),
            (Test, {"id": 1, "case_id": 0, "run_id": 2}),
            (Test, {"id": 1, "case_id": 1, "run_id": -2}),
            (Result, {"id": 1, "test_id": 0}),
            (Suite, {"id": 1}),
            (Status, {"id": 1, "name": "passed"}),
            (Case, {"id": 1, "title": "x", "created_on": "yesterday"}),
            (Case, {"id": 1, "title": "x", "created_on": 10**20}),
            (Run, {"id": 1, "name": "x", "config_ids": "1,2"}),
            (Run, {"id": 1, "name": "x", "is_completed": "no"}),
            (Plan, {"id": 1, "name": "x", "entries": {}}),
            (Plan, {"id": 1, "name": "x", "entries": [{"runs": []}]}),
            (Test, {"id": 1, "run_id": 2}),
            (Result, {"id": 1}),
            (Result, {"id": 1, "test_id": 2, "comment": 5}),
            (CustomField, {"id": 1, "system_name": "custom_x", "configs": [{"context": []}]}),
        ]
        for model, payload in cases:
            with self.subTest(model=model.__name__, payload=payload), self.assertRaises(ModelError):
                model.from_api(payload)

    def test_integer_booleans_accepted(self):
        self.assertIs(Run.from_api({"id": 1, "name": "x", "is_completed": 1}).is_completed, True)


if __name__ == "__main__":
    unittest.main()
