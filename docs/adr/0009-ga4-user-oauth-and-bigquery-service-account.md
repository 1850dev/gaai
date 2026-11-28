# ADR 0009: GA4 user OAuth and BigQuery service-account separation

Status: Accepted

Date: 2026-09-01

## Context

Requiring every customer to add a platform Service Account to every GA4 Property is not an acceptable standard onboarding model. Firebase Authentication proves application identity, but its ID token is not a delegated Google Analytics credential. BigQuery dataset access remains a workload-identity concern and must not be conflated with GA4.

## Decision

- `Ga4Source.auth_mode` is explicit per Data Source Set: `USER_OAUTH` or `SERVICE_ACCOUNT`.
- `USER_OAUTH` is the standard and default. The signed-in user completes a backend Authorization Code Flow with PKCE, one-use state, offline access, and `analytics.readonly`.
- MVP requires the Firebase Google subject/email and the OAuth Google subject/email to identify the same account.
- Refresh tokens are encrypted with Cloud KMS before Firestore persistence. Plaintext tokens never reach the browser, Firestore, logs, or application audit.
- GA4 credentials are selected per request and per user. The official pinned MCP tools run inside a `ContextVar` credential scope. No global tenant credential cache is allowed.
- Property discovery uses that same user's OAuth credential. Discovery does not create a Data Source Set or User Access Grant.
- Effective GA4 access requires Google-side Property access, registration of the exact Property in the Data Source Set, and an application User Access Grant.
- The backend overwrites caller/LLM Property IDs from the registered Data Source Set and records user-by-Property access status.
- `SERVICE_ACCOUNT` remains supported only when explicitly selected. There is no automatic fallback from `USER_OAUTH` to a Service Account.
- BigQuery continues to use a Data Access Profile Service Account through keyless impersonation and dataset/view-scoped IAM.

## Consequences

GA4 and BigQuery may coexist in one Data Source Set while using different identities. A missing or disabled BigQuery profile cannot be used as a GA4 credential. Conversely, a user's GA4 OAuth grant never authorizes BigQuery. Audit records include auth mode and non-secret identity metadata from the exact credential context used by the request.

The OAuth feature and Analytics execution feature remain independently fail-closed. Enabling `ANALYTICS_AI_ENABLE_GA4_USER_OAUTH` does not enable `ANALYTICS_AI_ENABLE_ANALYTICS_TOOLS`.

## Migration

Legacy GA4 records without `auth_mode` deserialize as `USER_OAUTH`. Existing Data Access Profile references may remain for BigQuery, but are ignored by GA4 in `USER_OAUTH` mode. Any tenant that intentionally uses GA4 Service Account access must be migrated to explicit `SERVICE_ACCOUNT` and pass separate acceptance tests.

## References

- [OAuth 2.0 for web server applications](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Google Analytics MCP](https://developers.google.com/analytics/devguides/MCP)
- [Cloud KMS encrypt and decrypt](https://cloud.google.com/kms/docs/encrypt-decrypt)
