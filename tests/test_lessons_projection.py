from __future__ import annotations

import json
from pathlib import Path

from esaa.service import ESAAService
from esaa.utils import sha256_hex


def _issue_report_with_lesson(task_id: str, prior_status: str, issue_id: str, task_kinds: list[str]) -> dict:
    return {
        "activity_event": {
            "action": "issue.report",
            "task_id": task_id,
            "prior_status": prior_status,
            "issue_id": issue_id,
            "severity": "medium",
            "title": "Quality gate lesson",
            "category": "quality-gate",
            "subtype": "integration-gap",
            "evidence": {
                "symptom": "integration check missing",
                "repro_steps": ["run qa", "observe missing integration evidence"],
            },
            "lesson": {
                "mistake": "Approved without integration evidence.",
                "rule": "Require integration evidence before approve.",
                "scope": {"task_kinds": task_kinds},
                "enforcement": {"mode": "require_step", "applies_to": "verification_gate"},
            },
        }
    }


def _expected_deterministic_lesson_id(issue_id: str, rule: str, task_kinds: list[str], mode: str, applies_to: str) -> str:
    signature = {
        "issue_id": issue_id,
        "rule": rule,
        "scope_task_kinds": sorted(task_kinds),
        "enforcement_mode": mode,
        "enforcement_applies_to": applies_to,
    }
    return f"LES-{sha256_hex(signature)[:12]}"


def test_issue_report_lesson_is_projected(contract_bundle: Path) -> None:
    service = ESAAService(contract_bundle)
    service.init(force=True)

    payload = _issue_report_with_lesson(
        task_id="T-1000",
        prior_status="todo",
        issue_id="ISS-LSN-001",
        task_kinds=["impl", "qa"],
    )
    service.submit(payload, actor="agent-spec")

    lessons_path = contract_bundle / ".roadmap" / "lessons.json"
    lessons = json.loads(lessons_path.read_text(encoding="utf-8"))
    assert len(lessons["lessons"]) == 1

    lesson = lessons["lessons"][0]
    expected_id = _expected_deterministic_lesson_id(
        issue_id="ISS-LSN-001",
        rule="Require integration evidence before approve.",
        task_kinds=["impl", "qa"],
        mode="require_step",
        applies_to="verification_gate",
    )
    assert lesson["lesson_id"] == expected_id
    assert lesson["status"] == "active"
    assert lesson["updated_at"] >= lesson["created_at"]
    assert lesson["source_refs"][0]["issue_id"] == "ISS-LSN-001"
    assert lessons["indexes"]["by_task_kind"]["impl"] == [expected_id]
    assert lessons["indexes"]["by_enforcement_applies_to"]["verification_gate"] == [expected_id]
    assert lessons["indexes"]["by_status"]["active"] == [expected_id]


def test_issue_report_lesson_projection_is_idempotent_upsert(contract_bundle: Path) -> None:
    service = ESAAService(contract_bundle)
    service.init(force=True)

    first = _issue_report_with_lesson(
        task_id="T-1000",
        prior_status="todo",
        issue_id="ISS-LSN-002",
        task_kinds=["impl", "qa"],
    )
    second = _issue_report_with_lesson(
        task_id="T-1000",
        prior_status="todo",
        issue_id="ISS-LSN-002",
        task_kinds=["qa", "impl"],  # same signature, different order
    )

    service.submit(first, actor="agent-spec")
    service.submit(second, actor="agent-spec")

    lessons = json.loads((contract_bundle / ".roadmap" / "lessons.json").read_text(encoding="utf-8"))
    assert len(lessons["lessons"]) == 1
    assert len(lessons["lessons"][0]["source_refs"]) == 2


def test_verify_detects_lessons_projection_mismatch(contract_bundle: Path) -> None:
    service = ESAAService(contract_bundle)
    service.init(force=True)
    service.submit(
        _issue_report_with_lesson(
            task_id="T-1000",
            prior_status="todo",
            issue_id="ISS-LSN-003",
            task_kinds=["spec"],
        ),
        actor="agent-spec",
    )

    lessons_path = contract_bundle / ".roadmap" / "lessons.json"
    corrupted = json.loads(lessons_path.read_text(encoding="utf-8"))
    corrupted["lessons"] = []
    corrupted["indexes"] = {
        "by_task_kind": {},
        "by_enforcement_applies_to": {},
        "by_status": {},
    }
    lessons_path.write_text(json.dumps(corrupted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    out = service.verify()
    assert out["verify_status"] == "mismatch"
    assert "stored_lessons_hash_sha256" in out
