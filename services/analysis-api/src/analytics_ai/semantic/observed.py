"""Exact drill-down filters derived from authorized, observed dimension values."""

import hashlib
import json

from analytics_ai.domain.models import (
    AnalysisFilter,
    AnalysisSession,
    SemanticDefinition,
    SemanticScope,
)


def observed_name(field: str, value: str) -> str:
    digest = hashlib.sha256(json.dumps([field, value]).encode()).hexdigest()
    return "observed_" + digest[:32]


def observed_definition(
    session: AnalysisSession, value: AnalysisFilter
) -> SemanticDefinition:
    if value.semantic_name != observed_name(value.field_name, value.value):
        raise ValueError("observed_filter_invalid")
    return SemanticDefinition(
        id=value.semantic_name,
        canonical_name=value.semantic_name,
        aliases=(value.semantic_name,),
        kind="filter",
        scope=SemanticScope.DATA_SOURCE_SET,
        company_id=session.company_id,
        data_source_set_id=session.data_source_set_id,
        version=session.state.semantic_version,
        ga4_mapping={"dimension": value.field_name, "value": value.value},
    )
