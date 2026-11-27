"""Operator-only read-only inventory; no tokens, raw GA4 data or mutation."""

import argparse
import asyncio
import json

from analytics_ai.config import Settings
from analytics_ai.repositories.firestore import FirestoreRepository


async def inventory(user_id: str) -> None:
    settings = Settings()
    repo = FirestoreRepository(settings.firebase_project_id)
    user = await repo.get_user(user_id)
    output: dict[str, object] = {
        "project": settings.firebase_project_id,
        "environment": settings.environment,
        "repository_backend": settings.resolved_repository_backend,
        "cache_backend": settings.resolved_cache_backend,
        "admin_checks": settings.enable_admin_connection_checks,
        "analytics_enabled": settings.enable_analytics_tools,
        "user_status": user.status.value if user else "unregistered",
    }
    companies = []
    for member in await repo.list_company_members(user_id):
        sources = await repo.list_company_sources(member.company_id)
        items = []
        for source in sources[:100]:
            registration = await repo.get_registration(source.id)
            grants = await repo.list_source_grants(source.id)
            items.append(
                {
                    "id": source.id,
                    "name": source.name,
                    "enabled": source.enabled,
                    "property": source.ga4.property_id if source.ga4 else None,
                    "auth_mode": source.ga4.auth_mode.value if source.ga4 else None,
                    "streams": [
                        s.model_dump(mode="json") for s in source.ga4.stream_scopes
                    ]
                    if source.ga4
                    else [],
                    "timezone": source.ga4.timezone if source.ga4 else None,
                    "currency": source.ga4.currency_code if source.ga4 else None,
                    "registration": registration.status
                    if registration
                    else "unapproved_legacy",
                    "grants": [
                        {"id": g.id, "uid": g.user_id, "enabled": g.enabled}
                        for g in grants[:100]
                    ],
                    "grants_truncated": len(grants) > 100,
                }
            )
        companies.append(
            {
                "id": member.company_id,
                "role": member.role.value,
                "status": member.status.value,
                "sets": items,
                "sets_truncated": len(sources) > 100,
            }
        )
    output["companies"] = companies
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    args = parser.parse_args()
    asyncio.run(inventory(args.user_id))
