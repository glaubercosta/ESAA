from __future__ import annotations

from pathlib import Path
from typing import Any

from .utils import sha256_hex, utc_now_iso


class LessonEngine:
    def __init__(self, root: Path) -> None:
        self.root = root

    def analyze_failures(self, events: list[dict[str, Any]], existing_lessons: list[dict[str, Any]]) -> list[dict[str, Any]]:
        suggestions: list[dict[str, Any]] = []
        seen_rules = {lesson.get("rule", "") for lesson in existing_lessons}

        for event in events:
            action = event["action"]
            payload = event.get("payload", {})

            if action == "output.rejected":
                code = str(payload.get("error_code", "UNKNOWN")).upper()
                suggestion = self._suggestion_for_rejection(code=code, payload=payload, event=event)
                if suggestion and suggestion["rule"] not in seen_rules:
                    suggestions.append(suggestion)
                    seen_rules.add(suggestion["rule"])

            if action == "verify.fail":
                rule = "Execute `esaa verify` após mutações e interrompa promoção em caso de mismatch/corrupted."
                if rule not in seen_rules:
                    suggestions.append(
                        self._make_suggestion(
                            title="Falha de Verificação de Projeção",
                            mistake="Foi detectado verify.fail indicando divergência ou corrupção de projeção.",
                            rule=rule,
                            applies_to="workflow_gate",
                            event=event,
                            mode="require_step",
                        )
                    )
                    seen_rules.add(rule)

            if action == "issue.report" and "boundary violation" in str(payload.get("title", "")).lower():
                rule = "Arquivos em áreas protegidas devem respeitar boundaries por task_kind e scope_patch."
                if rule not in seen_rules:
                    suggestions.append(
                        self._make_suggestion(
                            title="Boundary Violation Reportada",
                            mistake="Foi reportada tentativa de escrita fora dos limites autorizados.",
                            rule=rule,
                            applies_to="boundaries",
                            event=event,
                        )
                    )
                    seen_rules.add(rule)

        return suggestions

    def _suggestion_for_rejection(self, code: str, payload: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | None:
        message = str(payload.get("message", "")).strip()
        if code == "SCHEMA_INVALID":
            return self._make_suggestion(
                title="Falha de Validação de Schema",
                mistake=f"Payload rejeitado por schema inválido: {message or 'sem detalhes'}",
                rule="Sempre valide a saída contra o agent_result.schema.json antes de submeter.",
                applies_to="output_contract",
                event=event,
            )
        if code == "LOCK_VIOLATION":
            return self._make_suggestion(
                title="Violação de Lock de Tarefa",
                mistake=f"Ação enviada por ator diferente do lock owner: {message or 'sem detalhes'}",
                rule="O mesmo ator que faz claim deve completar/revisar a tarefa até liberar o lock.",
                applies_to="workflow_gate",
                event=event,
            )
        if code == "PRIOR_STATUS_MISMATCH":
            return self._make_suggestion(
                title="Divergência de Prior Status",
                mistake=f"prior_status informado não corresponde ao estado real da tarefa: {message or 'sem detalhes'}",
                rule="Antes de submeter, reprojete e use o prior_status atual do roadmap.",
                applies_to="workflow_gate",
                event=event,
            )
        if code.startswith("MISSING_"):
            return self._make_suggestion(
                title="Quebra de Sequência de Workflow",
                mistake=f"Saída rejeitada por pré-condição ausente ({code}): {message or 'sem detalhes'}",
                rule="Respeite a sequência claim -> complete -> review e os gates de verificação.",
                applies_to="workflow_gate",
                event=event,
            )
        if code == "BOUNDARY_VIOLATION":
            return self._make_suggestion(
                title="Violação de Boundary Detectada",
                mistake=f"Tentativa de escrita fora dos limites autorizados: {message or 'sem detalhes'}",
                rule="Arquivos em escopo protegido só podem ser alterados via caminhos permitidos por task_kind.",
                applies_to="boundaries",
                event=event,
            )
        return None

    def _make_suggestion(
        self,
        title: str,
        mistake: str,
        rule: str,
        applies_to: str,
        event: dict[str, Any],
        mode: str = "reject",
    ) -> dict[str, Any]:
        signature = {
            "rule": rule,
            "applies_to": applies_to,
            "mode": mode,
            "task_id": event.get("payload", {}).get("task_id", "unknown"),
        }
        lesson_id = f"LES-{sha256_hex(signature)[:12]}"
        return {
            "lesson_id": lesson_id,
            "status": "proposed",
            "created_at": utc_now_iso(),
            "title": title,
            "mistake": mistake,
            "rule": rule,
            "scope": {"task_kinds": ["spec", "impl", "qa"]},
            "enforcement": {
                "mode": mode,
                "applies_to": applies_to
            },
            "source_refs": [
                {
                    "event_id": event["event_id"],
                    "event_seq": int(event["event_seq"]),
                    "issue_id": f"ISS-SUG-{lesson_id[-6:].upper()}",
                    "task_id": event.get("payload", {}).get("task_id", "T-UNKNOWN"),
                }
            ]
        }
