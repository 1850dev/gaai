from __future__ import annotations

from analytics_ai.ga4.metadata import Ga4MetadataCatalogService


def test_custom_metadata_is_normalized_as_inactive_review_candidates() -> None:
    catalog = Ga4MetadataCatalogService._normalize(
        {
            "custom_dimensions": [
                {
                    "api_name": "customEvent:membership_level",
                    "ui_name": "Membership level",
                    "description": "Registered event-scoped dimension",
                    "category": "Custom",
                    "custom_definition": True,
                }
            ],
            "custom_metrics": [
                {
                    "apiName": "customEvent:content_score",
                    "uiName": "Content score",
                    "description": "Content quality score",
                }
            ],
        }
    )

    assert catalog.custom_dimensions[0].api_name == ("customEvent:membership_level")
    assert catalog.custom_dimensions[0].definition_type == "dimension"
    assert catalog.custom_metrics[0].definition_type == "metric"
    assert not hasattr(catalog.custom_metrics[0], "enabled")
