# ADR 0008: Profile-scoped GA4 property discovery

Status: Superseded by ADR 0009

This decision described the pre-correction implementation and must not be used for new development. Per-user GA4 Property discovery is now required.

## Context

`get_account_summaries` can enumerate the Analytics accounts and Property summaries visible to its caller. In this product, the Firebase user identity is deliberately different from the GA4／BigQuery data-access identity. Using a signed-in user's personal Google Analytics permissions for discovery would require an additional Google OAuth consent flow and would blur that boundary.

Discovery is also an onboarding operation: a Data Access Profile is not yet verified when its service account access must first be inspected. The normal Credential Broker correctly rejects an unverified Profile, so using the analysis execution path would create a verification cycle.

## Decision

- GA4 discovery uses the selected Data Access Profile Service Account, never the Firebase user's personal Analytics authorization.
- Only an active Company Admin or Platform Admin who is an active member of the Profile's Company may call discovery.
- Discovery has a separate onboarding credential method and a fixed one-tool allowlist: `get_account_summaries`.
- The normal analysis MCP Gateway continues to reject account discovery and continues to require an active, verified Profile.
- Account and Property summaries are normalized to bounded candidates containing Account ID/name, Property ID/name, Property type, parent, and `can_edit`.
- Ordinary Properties, Analytics 360 subproperties, roll-up Properties, and multiple Analytics accounts are preserved in the candidate list.
- A discovery result never creates or enables a Data Source Set and never creates a User Access Grant. An administrator must select the exact Property, register its stream scope, verify the connection, and grant users explicitly.
- Normal users only list enabled Data Source Sets for which they have an explicit User Access Grant.
- Discovery calls are audited by Profile and caller without recording access tokens or credentials.
- A separate `ANALYTICS_AI_ENABLE_ADMIN_CONNECTION_CHECKS` gate controls onboarding checks and defaults to `false`. It does not enable Message analysis tools; `ANALYTICS_AI_ENABLE_ANALYTICS_TOOLS=false` remains unchanged until Analytics acceptance passes.

## Deferred alternative

If the product later needs to show Properties visible to each human's personal Analytics account, it requires a separate, explicitly consented Analytics Discovery OAuth flow using `analytics.readonly`. Its token must be short-lived, Backend-only, and unusable for analytics queries. It must not auto-create a Data Source Set or grant. This is a new security-boundary decision and requires a separate ADR before implementation.

## Consequences

Company onboarding can discover every Property intentionally shared with that Company's service account without distributing a key. Accidental over-grants to the service account can be seen by that Company's administrator, but do not become usable data sources automatically. Firebase login alone does not enumerate the human user's Analytics access.

## References

- [Google Analytics Admin API: accountSummaries.list](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/accountSummaries/list)
- [Google Analytics Admin API: Properties resource](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties)
- [Firebase Authentication: Google sign-in and additional OAuth scopes](https://firebase.google.com/docs/auth/web/google-signin)
- [Google Analytics MCP guide](https://developers.google.com/analytics/devguides/MCP)
