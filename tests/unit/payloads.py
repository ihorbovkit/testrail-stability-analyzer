"""Synthetic TestRail API payloads shaped like real responses. No real customer data."""

T0 = 1_700_000_000  # 2023-11-14T22:13:20Z


def project(**overrides):
    return {
        "id": 1,
        "name": "Example Project",
        "announcement": None,
        "show_announcement": False,
        "is_completed": False,
        "completed_on": None,
        "suite_mode": 3,
        "url": "https://example.testrail.io/index.php?/projects/overview/1",
        **overrides,
    }


def suite(**overrides):
    return {
        "id": 10,
        "name": "Regression",
        "description": None,
        "project_id": 1,
        "is_master": False,
        "is_baseline": False,
        "is_completed": False,
        "completed_on": None,
        "url": "https://example.testrail.io/index.php?/suites/view/10",
        **overrides,
    }


def status(id, name, label, *, is_system=True, is_untested=False, is_final=True):
    return {
        "id": id,
        "name": name,
        "label": label,
        "color_dark": 1,
        "color_medium": 2,
        "color_bright": 3,
        "is_system": is_system,
        "is_untested": is_untested,
        "is_final": is_final,
    }


# TestRail's built-in system statuses plus two instance-specific custom statuses.
STATUSES = [
    status(1, "passed", "Passed"),
    status(2, "blocked", "Blocked"),
    status(3, "untested", "Untested", is_untested=True, is_final=False),
    status(4, "retest", "Retest", is_final=False),
    status(5, "failed", "Failed"),
    status(6, "custom_status1", "Automation Problem", is_system=False),
    status(7, "custom_status2", "Not Applicable", is_system=False),
]


def case_field(**overrides):
    return {
        "id": 20,
        "system_name": "custom_component",
        "name": "component",
        "label": "Component",
        "type_id": 6,
        "description": None,
        "display_order": 1,
        "is_active": True,
        "include_all": False,
        "template_ids": [1, 2],
        "configs": [
            {
                "id": "abc-123",
                "context": {"is_global": False, "project_ids": [1, 3]},
                "options": {"is_required": False, "default_value": "1", "items": "1, Login\n2, Checkout\n"},
            }
        ],
        "entity_id": 1,
        **overrides,
    }


def case(id=100, **overrides):
    return {
        "id": id,
        "title": f"Case {id}",
        "section_id": 30,
        "suite_id": 10,
        "template_id": 1,
        "type_id": 3,
        "priority_id": 2,
        "milestone_id": None,
        "refs": "REQ-1",
        "estimate": None,
        "created_on": T0,
        "created_by": 5,
        "updated_on": T0 + 60,
        "updated_by": 5,
        "display_order": 1,
        "is_deleted": 0,
        "custom_component": 2,
        "custom_automation_id": "suite.module.test_login",
        **overrides,
    }


def run(id=500, **overrides):
    return {
        "id": id,
        "name": f"Run {id}",
        "description": None,
        "suite_id": 10,
        "project_id": 1,
        "plan_id": None,
        "milestone_id": None,
        "assignedto_id": None,
        "include_all": True,
        "is_completed": False,
        "completed_on": None,
        "config": None,
        "config_ids": [],
        "entry_id": None,
        "entry_index": None,
        "created_on": T0,
        "created_by": 5,
        "updated_on": T0 + 30,
        "refs": None,
        "url": f"https://example.testrail.io/index.php?/runs/view/{id}",
        "passed_count": 3,
        "blocked_count": 0,
        "untested_count": 1,
        "retest_count": 0,
        "failed_count": 1,
        "custom_status1_count": 2,
        "custom_status2_count": 0,
        **overrides,
    }


def plan(id=900, *, entries=None, **overrides):
    payload = {
        "id": id,
        "name": f"Plan {id}",
        "description": None,
        "milestone_id": None,
        "assignedto_id": None,
        "is_completed": False,
        "completed_on": None,
        "project_id": 1,
        "created_on": T0,
        "created_by": 5,
        "refs": None,
        "url": f"https://example.testrail.io/index.php?/plans/view/{id}",
        "passed_count": 6,
        "blocked_count": 0,
        "untested_count": 0,
        "retest_count": 0,
        "failed_count": 2,
        "custom_status1_count": 1,
        **overrides,
    }
    if entries is not None:
        payload["entries"] = entries
    return payload


def plan_entry(entry_id, plan_id, runs, **overrides):
    return {
        "id": entry_id,
        "suite_id": 10,
        "name": "Regression",
        "description": None,
        "include_all": True,
        "refs": None,
        "runs": [
            run(run_id, plan_id=plan_id, entry_id=entry_id, entry_index=1, config=config, config_ids=[config_id])
            for run_id, config, config_id in runs
        ],
        **overrides,
    }


def test(id=7000, **overrides):
    return {
        "id": id,
        "case_id": 100,
        "run_id": 500,
        "status_id": 5,
        "assignedto_id": None,
        "title": "Case 100",
        "template_id": 1,
        "type_id": 3,
        "priority_id": 2,
        "estimate": None,
        "estimate_forecast": None,
        "refs": None,
        "milestone_id": None,
        "custom_automation_id": "suite.module.test_login",
        **overrides,
    }


def result(id=80000, **overrides):
    return {
        "id": id,
        "test_id": 7000,
        "status_id": 5,
        "created_on": T0 + 120,
        "created_by": 5,
        "assignedto_id": None,
        "comment": "Synthetic failure message",
        "version": "1.2.3",
        "elapsed": "1m 5s",
        "defects": None,
        "attachment_ids": [],
        "custom_environment": "staging",
        **overrides,
    }
