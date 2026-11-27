output "runtime_service_account" {
  description = "Cloud Run runtime identity. This is the only principal allowed to impersonate data access profiles."
  value       = google_service_account.runtime.email
}

output "web_runtime_service_account" {
  description = "Web Cloud Run identity. It has no Firestore, Cache, Vertex, BigQuery, GA4, or impersonation role."
  value       = google_service_account.web_runtime.email
}

output "data_access_service_accounts" {
  description = "Give each email to the customer for dataset/view-scoped BigQuery Data Viewer. Use for GA4 only when the Data Source Set explicitly selects SERVICE_ACCOUNT mode."
  value       = { for key, account in google_service_account.data_access : key => account.email }
}

output "ga4_oauth_kms_key" {
  description = "KMS key used by the backend to encrypt per-user GA4 OAuth refresh tokens."
  value       = google_kms_crypto_key.ga4_oauth_refresh_tokens.id
}

output "ga4_oauth_client_secret_id" {
  description = "Secret Manager secret ID. Add a version out of band; Terraform never stores the OAuth client secret value."
  value       = google_secret_manager_secret.ga4_oauth_client_secret.secret_id
}

output "dataset_cache_bucket" {
  value = google_storage_bucket.dataset_cache.name
}

output "analysis_api_uri" {
  value = try(google_cloud_run_v2_service.analysis_api[0].uri, null)
}

output "web_uri" {
  value = try(google_cloud_run_v2_service.web[0].uri, null)
}
