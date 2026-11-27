from __future__ import annotations

import json
from pathlib import Path

from analytics_ai.domain.models import AnalysisState
from analytics_ai.visualization.planner import VisualizationSpec


def contract(name: str) -> dict[str, object]:
    repository_root = Path(__file__).parents[3]
    return json.loads(
        (repository_root / "packages" / "contracts" / name).read_text(encoding="utf-8")
    )


def test_analysis_state_contract_matches_backend_top_level_fields() -> None:
    schema = contract("analysis-state.schema.json")
    assert set(schema["required"]) == set(AnalysisState.model_fields)
    assert set(schema["properties"]) == set(AnalysisState.model_fields)


def test_visualization_contract_matches_backend_top_level_fields() -> None:
    schema = contract("visualization-spec.schema.json")
    assert set(schema["required"]) == set(VisualizationSpec.model_fields)
    assert set(schema["properties"]) == set(VisualizationSpec.model_fields)
