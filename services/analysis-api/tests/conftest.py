from __future__ import annotations

import pytest

from analytics_ai.domain.models import (
    BigQuerySource,
    CompanyMember,
    ConnectionStatus,
    DataAccessProfile,
    DataSourceSet,
    Ga4Platform,
    Ga4Source,
    Ga4StreamScope,
    SemanticDefinition,
    SemanticScope,
    Status,
    User,
    UserAccessGrant,
)
from analytics_ai.repositories.memory import InMemoryRepository


@pytest.fixture
def repository() -> InMemoryRepository:
    profile = DataAccessProfile(
        id="dap_company_a",
        company_id="company_a",
        service_account_email=(
            "company-a-analytics@analytics-ai-prod.iam.gserviceaccount.com"
        ),
        allowed_mcp_tools=frozenset(
            {
                "run_report",
                "run_realtime_report",
                "run_funnel_report",
                "execute_sql_readonly",
                "get_table_info",
                "list_table_ids",
                "get_dataset_info",
            }
        ),
        status=Status.ACTIVE,
        connection_status=ConnectionStatus.VERIFIED,
    )
    web = DataSourceSet(
        id="company_a_web",
        company_id="company_a",
        name="A社 Web",
        data_access_profile_id=profile.id,
        ga4=Ga4Source(
            property_id="222222222",
            display_name="A社 Web",
            stream_scopes=(
                Ga4StreamScope(stream_id="3333333333", platform=Ga4Platform.WEB),
            ),
        ),
        bigquery=BigQuerySource(
            project_id="company-a-data",
            dataset_ids=("analytics_222222222",),
            allowed_resources=(
                "company-a-data.analytics_222222222.events_*",
                "company-a-data.analytics_222222222.summary_*",
            ),
        ),
        semantic_version="a-web-v1",
        enabled=True,
    )
    store = DataSourceSet(
        id="company_a_store",
        company_id="company_a",
        name="A社 店舗",
        data_access_profile_id=profile.id,
        ga4=Ga4Source(property_id="111111111", display_name="A社 店舗"),
        bigquery=BigQuerySource(
            project_id="company-a-data",
            dataset_ids=("analytics_111111111",),
            allowed_resources=("company-a-data.analytics_111111111.events_*",),
        ),
        semantic_version="a-store-v1",
        enabled=True,
    )
    semantics = [
        SemanticDefinition(
            id="global-active-users",
            canonical_name="active_users",
            aliases=("UU", "ユーザー数"),
            kind="metric",
            scope=SemanticScope.GLOBAL,
            ga4_mapping={"metric": "activeUsers"},
            version="a-web-v1",
        ),
        SemanticDefinition(
            id="global-organic",
            canonical_name="organic_search",
            aliases=("Organic", "Organic Search"),
            kind="channel",
            scope=SemanticScope.GLOBAL,
            ga4_mapping={
                "dimension": "sessionDefaultChannelGroup",
                "value": "Organic Search",
            },
            version="a-web-v1",
        ),
        SemanticDefinition(
            id="global-mobile",
            canonical_name="mobile",
            aliases=("モバイル", "mobile"),
            kind="device",
            scope=SemanticScope.GLOBAL,
            ga4_mapping={"dimension": "deviceCategory", "value": "mobile"},
            version="a-web-v1",
        ),
        SemanticDefinition(
            id="web-conversion-unavailable",
            canonical_name="conversion",
            aliases=("CV", "コンバージョン"),
            kind="metric",
            scope=SemanticScope.DATA_SOURCE_SET,
            company_id="company_a",
            data_source_set_id="company_a_web",
            version="a-web-v1",
            available=False,
        ),
    ]
    return InMemoryRepository(
        users=[
            User(uid="user-a", email="a@example.com"),
            User(uid="user-b", email="b@example.com"),
        ],
        members=[
            CompanyMember(user_id="user-a", company_id="company_a"),
            CompanyMember(user_id="user-b", company_id="company_a"),
        ],
        profiles=[profile],
        source_sets=[web, store],
        semantic_definitions=semantics,
        grants=[
            UserAccessGrant(
                id="grant-a-web",
                user_id="user-a",
                data_source_set_id=web.id,
            ),
            UserAccessGrant(
                id="grant-b-store",
                user_id="user-b",
                data_source_set_id=store.id,
            ),
        ],
    )
