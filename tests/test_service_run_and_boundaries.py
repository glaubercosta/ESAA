from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from esaa.adapters.base import AgentAdapter
from esaa.errors import ESAAError
from esaa.projector import materialize
from esaa.service import ESAAService, build_dispatch_context
from esaa.store import load_agent_contract, load_agent_result_schema, parse_event_store
from esaa.validator import validate_agent_output


class InvalidPathAdapter(AgentAdapter):
    def __init__(self) -> None:
        self.agent_id = "agent-invalid-path"
        self._calls = 0

    def health(self) -> dict[str, str]:
        return {"status": "ok"}

    def execute(self, dispatch_context: dict[str, Any]) -> dict[str, Any]:
        task = dispatch_context["task"]
        self._calls += 1
        if self._calls == 1:
            return {
                "activity_event": {
                    "action": "claim",
                    "task_id": task["task_id"],
                    "prior_status": "todo",
                    "notes": "claim",
                }
            }
        return {
            "activity_event": {
                "action": "complete",
                "task_id": task["task_id"],
                "prior_status": "in_progress",
                "verification": {"checks": ["ok"]},
                "discovery_evidence": {
                    "unknowns": ["none"],
                    "assumptions": ["spec task still active"],
                    "critical_questions": ["is target path allowed?"],
                },
            },
            "file_updates": [{"path": "src/evil.txt", "content": "invalid for spec"}],
        }


def test_boundaries_reject_spec_write_into_src(contract_bundle: Path) -> None:
    contract = load_agent_contract(contract_bundle)
    schema = load_agent_result_schema(contract_bundle)
    task = {
        "task_id": "T-SPEC",
        "task_kind": "spec",
        "status": "in_progress",
        "outputs": {"files": ["docs/spec/T-SPEC.md"]},
    }
    output = {
        "activity_event": {
            "action": "complete",
            "task_id": "T-SPEC",
            "prior_status": "in_progress",
            "verification": {"checks": ["ok"]},
            "discovery_evidence": {
                "unknowns": ["none"],
                "assumptions": ["boundary checks enabled"],
                "critical_questions": ["does write path violate spec boundary?"],
            },
        },
        "file_updates": [{"path": "src/not-allowed.txt", "content": "x"}],
    }
    with pytest.raises(ESAAError) as exc:
        validate_agent_output(output, schema, contract, task)
    assert exc.value.code == "BOUNDARY_VIOLATION"


def test_output_rejected_has_no_side_effect_files(contract_bundle: Path) -> None:
    service = ESAAService(contract_bundle, adapter=InvalidPathAdapter())
    service.init(force=True)
    result = service.run(steps=2)
    assert result["rejected"] >= 1
    assert not (contract_bundle / "src/evil.txt").exists()

    events = parse_event_store(contract_bundle)
    assert any(event["action"] == "output.rejected" for event in events)


def test_dispatch_context_injects_active_lessons(contract_bundle: Path) -> None:
    service = ESAAService(contract_bundle)
    service.init(force=True)

    lessons_payload = {
        "meta": {"schema_version": "0.4.0"},
        "lessons": [
            {
                "lesson_id": "LES-1234",
                "status": "active",
                "title": "Discovery required",
                "mistake": "spec complete without discovery",
                "rule": "always include discovery_evidence for spec complete",
                "scope": {"task_kinds": ["spec"]},
                "enforcement": {"mode": "reject", "applies_to": "workflow_gate"},
            },
            {
                "lesson_id": "LES-9999",
                "status": "inactive",
                "title": "Ignored",
                "mistake": "n/a",
                "rule": "n/a",
                "scope": {"task_kinds": ["spec"]},
                "enforcement": {"mode": "warn", "applies_to": "workflow_gate"},
            },
        ],
        "indexes": {},
    }
    (contract_bundle / ".roadmap" / "lessons.json").write_text(
        json.dumps(lessons_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    contract = load_agent_contract(contract_bundle)
    events = parse_event_store(contract_bundle)
    roadmap, _, _ = materialize(events)
    spec_task = roadmap["tasks"][0]

    context = build_dispatch_context(roadmap=roadmap, task=spec_task, contract=contract, root=contract_bundle)
    assert "active_lessons" in context
    assert len(context["active_lessons"]) == 1
    assert context["active_lessons"][0]["lesson_id"] == "LES-1234"
