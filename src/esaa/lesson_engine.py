from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .utils import utc_now_iso


class LessonEngine:
    def __init__(self, root: Path) -> None:
        self.root = root

    def analyze_failures(self, events: list[dict[str, Any]], existing_lessons: list[dict[str, Any]]) -> list[dict[str, Any]]:
        suggestions = []
        
        # Track what we already have lessons for to avoid duplicates
        seen_rules = {l["rule"] for l in existing_lessons}
        
        for event in events:
            action = event["action"]
            payload = event.get("payload", {})
            
            # Case 1: Schema Invalid
            if action == "output.rejected" and payload.get("error_code") == "SCHEMA_INVALID":
                rule = "Sempre valide a saída contra o agent_result.schema.json antes de submeter."
                if rule not in seen_rules:
                    suggestions.append(self._make_suggestion(
                        title="Falha de Validação de Schema",
                        mistake=f"O agente enviou um payload que quebrou o contrato: {payload.get('message')}",
                        rule=rule,
                        applies_to="output_contract",
                        event=event
                    ))
                    seen_rules.add(rule)

            # Case 2: Boundary Violation (Issue Report)
            if action == "issue.report" and "Boundary Violation" in payload.get("title", ""):
                rule = "Arquivos em .roadmap/ são protegidos e só podem ser alterados via orchestrator.view.mutate."
                if rule not in seen_rules:
                    suggestions.append(self._make_suggestion(
                        title="Violação de Boundary Detectada",
                        mistake="Tentativa de escrita direta em diretório protegido (.roadmap/).",
                        rule=rule,
                        applies_to="boundaries",
                        event=event
                    ))
                    seen_rules.add(rule)

        return suggestions

    def _make_suggestion(self, title: str, mistake: str, rule: str, applies_to: str, event: dict[str, Any]) -> dict[str, Any]:
        return {
            "lesson_id": "SUGGESTION", # Temporary
            "status": "proposed",
            "created_at": utc_now_iso(),
            "title": title,
            "mistake": mistake,
            "rule": rule,
            "scope": {"task_kinds": ["spec", "impl", "qa"]},
            "enforcement": {
                "mode": "reject",
                "applies_to": applies_to
            },
            "source_refs": [
                {"event_id": event["event_id"], "task_id": event["payload"].get("task_id")}
            ]
        }
