"""Deterministic Global → Company → Data Source Set semantic resolver."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from analytics_ai.domain.models import SemanticDefinition, SemanticScope


class SemanticResolutionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedSemantic:
    definition: SemanticDefinition
    matched_alias: str
    confidence: float


def normalize_term(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


class SemanticResolver:
    def __init__(self, definitions: list[SemanticDefinition]) -> None:
        self._definitions = tuple(item for item in definitions if item.enabled)

    def resolve(
        self,
        term: str,
        *,
        company_id: str,
        data_source_set_id: str,
        semantic_version: str,
    ) -> ResolvedSemantic:
        normalized = normalize_term(term)
        matches: list[tuple[int, SemanticDefinition, str]] = []
        for definition in self._definitions:
            if definition.version != semantic_version:
                continue
            if not self._in_scope(definition, company_id, data_source_set_id):
                continue
            for alias in (definition.canonical_name, *definition.aliases):
                if normalize_term(alias) == normalized:
                    matches.append((self._rank(definition.scope), definition, alias))
                    break
        if not matches:
            raise SemanticResolutionError("semantic_term_not_found")
        most_specific = max(rank for rank, _, _ in matches)
        finalists = [
            (definition, alias)
            for rank, definition, alias in matches
            if rank == most_specific
        ]
        canonical = {definition.canonical_name for definition, _ in finalists}
        availability = {definition.available for definition, _ in finalists}
        if len(canonical) != 1 or len(availability) != 1:
            raise SemanticResolutionError("semantic_term_ambiguous")
        definition, alias = sorted(finalists, key=lambda item: item[0].id)[0]
        if not definition.available:
            raise SemanticResolutionError(
                "semantic_term_unavailable_for_data_source_set"
            )
        return ResolvedSemantic(
            definition=definition,
            matched_alias=alias,
            confidence=1.0,
        )

    @staticmethod
    def _in_scope(
        definition: SemanticDefinition,
        company_id: str,
        data_source_set_id: str,
    ) -> bool:
        if definition.scope is SemanticScope.GLOBAL:
            return True
        if definition.scope is SemanticScope.COMPANY:
            return definition.company_id == company_id
        return (
            definition.company_id == company_id
            and definition.data_source_set_id == data_source_set_id
        )

    @staticmethod
    def _rank(scope: SemanticScope) -> int:
        return {
            SemanticScope.GLOBAL: 0,
            SemanticScope.COMPANY: 1,
            SemanticScope.DATA_SOURCE_SET: 2,
        }[scope]
