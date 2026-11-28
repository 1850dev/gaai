# BigQuery Remote MCP

Endpoint: `https://bigquery.googleapis.com/mcp`.

The standard execution tool is `execute_sql_readonly`; `execute_sql` is denied. The billing project is injected from `ANALYTICS_AI_PLATFORM_PROJECT_ID`; data resources must match the source-set allowlist.

## Execution

1. Obtain allowlisted table metadata if needed.
2. Parse and validate SQL.
3. Call `execute_sql_readonly` with `dryRun=true`.
4. Compare `totalBytesProcessed` with policy.
5. Reauthorize and execute with `dryRun=false`.
6. Enforce result-shape policy.
7. Store the small result, query ID, bytes, and Evidence linkage.

Google documents a default three-minute limit, 3,000-row maximum, and no Google Drive external tables. SQL aggregates and ranks inside BigQuery. Large text is processed by opted-in BigQuery AI and reduced before return.

## Hard cost limits

The current MCP schema has `dryRun` and byte statistics but no `maximum_bytes_billed`. Dry-run budget is not an absolute hard cap. Workloads requiring one must use a BigQuery API Safety Adapter.

