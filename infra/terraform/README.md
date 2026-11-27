# Terraform

This root provisions platform-owned infrastructure only. Standard GA4 access uses per-user OAuth and does not require a platform Service Account in customer Properties. BigQuery dataset/view `roles/bigquery.dataViewer` remains a customer-side onboarding step; this root intentionally does not grant project-wide data access. GA4 Service Account mode is an explicit exception.

Copy `terraform.tfvars.example`, use a remote state backend with locking, then run `terraform init`, `terraform plan`, and an approved `terraform apply`. Keep `enable_analytics_tools = false` until User Access Grants and every Data Source Set scope have been reviewed.

Production must use `analytics-ai-prod`. Each `data_access_profiles` entry creates a distinct keyless service account. The Cloud Run runtime can impersonate only those target accounts; end users receive neither credentials nor Token Creator.

Terraform creates a Cloud KMS key for per-user GA4 Refresh Token ciphertext and an empty Secret Manager secret container for the OAuth Web Client secret. Add the secret value out of band so it never enters Terraform state. Register the exact `ga4_oauth_redirect_uri` on the Google OAuth Client, add a Secret version, then and only then set `enable_ga4_user_oauth = true`. This gate is independent from `enable_analytics_tools`.

The Backend and Web Cloud Run services use different runtime identities. Only `analytics-ai-runtime` receives Firestore, Cache, Vertex, and per-target Token Creator roles. `analytics-ai-web` receives Logging Writer only and must never be added to a Data Access Profile, customer GA4 Property, or customer BigQuery Dataset.

If Firebase or the default Firestore database already exists, import those resources into the selected Terraform state before planning. Do not attempt to recreate or replace the existing database. Review the full plan and keep `enable_analytics_tools = false`; apply only after explicit approval.

## Existing dev Firebase／Firestore

For an existing dev Firebase project (e.g. `your-firebase-project-id`), keep `terraform.tfvars` private and import the existing resources before the first apply:

```bash
terraform import google_firebase_project.platform projects/your-firebase-project-id
terraform import google_firestore_database.platform 'projects/your-firebase-project-id/databases/(default)'
terraform plan
```

The accepted pre-apply plan must show no replacement, deletion, or in-place change for Firebase／Firestore. The Firestore `deletion_policy` is always `ABANDON`, including dev, so removing it from Terraform state cannot delete the existing database.

The local dev state and `terraform.tfvars` are Git-ignored. A shared remote backend with locking is required before multiple operators or environments manage the same infrastructure.
