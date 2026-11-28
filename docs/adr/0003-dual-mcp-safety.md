# ADR 0003: Dual MCP behind a policy gateway

Status: Accepted

Use official GA4 MCP tools and BigQuery Remote MCP, never expose either to the LLM. The gateway reauthorizes, injects scope, selects credentials, checks tools/SQL/budget, and audits. BigQuery `execute_sql` is denied.

