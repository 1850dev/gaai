# ADR 0004: Request-scoped GA4 credentials

Status: Accepted with pinned upstream contract

Official analytics-mcp v0.7.0 has a module-global credential cache. Install a ContextVar provider and invoke pinned official tools within a credential scope. Upgrade requires contract/concurrency tests; otherwise maintain a minimal fork with an explicit provider.

