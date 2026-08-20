"""Committed synthetic scenario catalog."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


CATALOG_PATH = Path(__file__).resolve().parents[1] / "scenarios" / "catalog.json"


def scenario_checksum(scenario: dict[str, Any]) -> str:
    content = {"request": scenario["request"], "response": scenario.get("response")}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


class ScenarioCatalog:
    def __init__(self, path: Path = CATALOG_PATH) -> None:
        document = json.loads(path.read_text(encoding="utf-8"))
        scenarios = document.get("scenarios", [])
        self._scenarios = {scenario["id"]: scenario for scenario in scenarios}
        if len(self._scenarios) != len(scenarios):
            raise ValueError("scenario IDs must be unique")
        for scenario in scenarios:
            if scenario.get("fictional_data_attestation") is not True:
                raise ValueError(f"{scenario['id']}: fictional-data attestation required")
            if scenario.get("checksum") != scenario_checksum(scenario):
                raise ValueError(f"{scenario['id']}: content checksum mismatch")

    def get(self, scenario_id: str) -> dict[str, Any] | None:
        return self._scenarios.get(scenario_id)

    def public_scenarios(self) -> list[dict[str, Any]]:
        """Return display metadata without exposing committed prompt or response text."""
        result = []
        for scenario in self._scenarios.values():
            request = scenario["request"]
            response_format = request.get("response_format") or {"type": "text"}
            result.append(
                {
                    "id": scenario["id"],
                    "purpose": scenario["purpose"],
                    "stream": bool(request.get("stream")),
                    "tools": bool(request.get("tools")),
                    "response_format": response_format["type"],
                    "fault": scenario["id"].startswith("fault."),
                }
            )
        return result
