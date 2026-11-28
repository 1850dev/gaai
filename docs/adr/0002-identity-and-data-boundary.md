# ADR 0002: Separate human identity from data identity

Status: Superseded in part by ADR 0009

Firebase UID identifies a human. Data Source Set plus per-user grant is the application authorization boundary; membership is insufficient. BigQuery uses a profile service account. GA4 uses delegated user OAuth by default and may explicitly use a profile service account. Cloud Run impersonates only individual profile accounts with short-lived credentials; keys are prohibited.
