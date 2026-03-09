from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .utils import ensure_parent, utc_now_iso

MEMORY_PATH = ".roadmap/memory/semantic_index.json"


class SemanticMemory:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.index_path = root / MEMORY_PATH
        self.data: dict[str, Any] = {
            "meta": {
                "last_event_seq": 0,
                "updated_at": None,
            },
            "entries": []
        }
        self._load()

    def _load(self) -> None:
        if self.index_path.exists():
            try:
                self.data = json.loads(self.index_path.read_text(encoding="utf-8"))
            except Exception:
                pass

    def save(self) -> None:
        ensure_parent(self.index_path)
        self.data["meta"]["updated_at"] = utc_now_iso()
        self.index_path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def sync(self, events: list[dict[str, Any]]) -> int:
        last_synced = self.data["meta"].get("last_event_seq", 0)
        new_events = [ev for ev in events if ev["event_seq"] > last_synced]
        
        count = 0
        for event in new_events:
            text_blocks = self._extract_text(event)
            if not text_blocks:
                continue
            
            for block in text_blocks:
                self.data["entries"].append({
                    "event_id": event["event_id"],
                    "event_seq": event["event_seq"],
                    "actor": event["actor"],
                    "action": event["action"],
                    "task_id": event["payload"].get("task_id"),
                    "text": block,
                    "ts": event["ts"]
                })
            count += 1
            self.data["meta"]["last_event_seq"] = event["event_seq"]
        
        if count > 0:
            self.save()
        return count

    def _extract_text(self, event: dict[str, Any]) -> list[str]:
        payload = event.get("payload", {})
        blocks = []
        
        # 1. Notes
        if payload.get("notes"):
            blocks.append(payload["notes"])
            
        # 2. Discovery Evidence
        discovery = payload.get("discovery_evidence")
        if discovery:
            for key in ["unknowns", "assumptions", "critical_questions"]:
                items = discovery.get(key, [])
                if items:
                    blocks.append(f"{key.capitalize()}: " + " | ".join(items))
        
        # 3. Task details (on create)
        if event["action"] == "task.create":
            blocks.append(f"Task {payload.get('task_id')}: {payload.get('title')} - {payload.get('description')}")

        # 4. Mutations (Mutate)
        if event["action"] == "orchestrator.view.mutate":
            blocks.append(f"Mutation: {payload.get('summary')}")

        return [b for b in blocks if b]

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        if not query or not self.data["entries"]:
            return []
        
        # Simple keyword matching as fallback/MVP
        # Future: Sentence-transformers integration here
        query_terms = set(re.findall(r"\w+", query.lower()))
        
        scored_entries = []
        for entry in self.data["entries"]:
            entry_text = entry["text"].lower()
            score = 0
            for term in query_terms:
                if term in entry_text:
                    score += 1
            if score > 0:
                scored_entries.append((score, entry))
        
        # Sort by score (desc) then by event_seq (desc - more recent first if score tie)
        scored_entries.sort(key=lambda x: (x[0], x[1]["event_seq"]), reverse=True)
        
        return [item[1] for item in scored_entries[:top_k]]
