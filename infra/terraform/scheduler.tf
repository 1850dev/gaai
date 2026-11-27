variable "enable_report_worker" {
  description = "Deploy the scheduled GA4 report worker after delegated OAuth acceptance."
  type        = bool
  default     = false
  validation {
    condition     = !var.enable_report_worker || (var.backend_image != "" && var.enable_analytics_tools && var.enable_ga4_user_oauth)
    error_message = "Report worker requires a backend image, analytics tools and GA4 OAuth."
  }
}

resource "google_project_service" "scheduler" {
  count              = var.enable_report_worker ? 1 : 0
  project            = var.platform_project_id
  service            = "cloudscheduler.googleapis.com"
  disable_on_destroy = false
}

resource "google_service_account" "scheduler" {
  count        = var.enable_report_worker ? 1 : 0
  project      = var.platform_project_id
  account_id   = "analytics-report-trigger"
  display_name = "Scheduled report job invoker only"
}

resource "google_cloud_run_v2_job" "reports" {
  count               = var.enable_report_worker ? 1 : 0
  name                = "analytics-ai-reports-${var.environment}"
  project             = var.platform_project_id
  location            = var.region
  deletion_protection = var.environment == "prod"
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = google_service_account.runtime.email
      max_retries     = 0
      timeout         = "7200s"
      containers {
        image   = var.backend_image
        command = ["python", "-m", "analytics_ai.agent.worker"]
        resources {
          limits = { cpu = "2", memory = "2Gi" }
        }
        dynamic "env" {
          for_each = {
            ANALYTICS_AI_ENVIRONMENT            = var.environment
            ANALYTICS_AI_FIREBASE_PROJECT_ID    = var.platform_project_id
            ANALYTICS_AI_PLATFORM_PROJECT_ID    = var.platform_project_id
            ANALYTICS_AI_REPOSITORY_BACKEND     = "firestore"
            ANALYTICS_AI_CACHE_BACKEND          = "gcs"
            ANALYTICS_AI_CACHE_BUCKET           = google_storage_bucket.dataset_cache.name
            ANALYTICS_AI_ENABLE_ANALYTICS_TOOLS = "true"
            ANALYTICS_AI_ENABLE_BROWSER_QWEN    = tostring(var.enable_browser_qwen)
            ANALYTICS_AI_VERTEX_USER_IDS        = jsonencode(sort(tolist(var.vertex_user_ids)))
            ANALYTICS_AI_ENABLE_BIGQUERY_TOOLS  = "false"
            ANALYTICS_AI_VERTEX_MODEL           = var.vertex_model
            ANALYTICS_AI_VERTEX_LOCATION        = var.vertex_location
            ANALYTICS_AI_ENABLE_GA4_USER_OAUTH  = "true"
            ANALYTICS_AI_GA4_OAUTH_CLIENT_ID    = var.ga4_oauth_client_id
            ANALYTICS_AI_GA4_OAUTH_REDIRECT_URI = var.ga4_oauth_redirect_uri
            ANALYTICS_AI_GA4_OAUTH_RETURN_URL   = var.ga4_oauth_return_url
            ANALYTICS_AI_GA4_OAUTH_KMS_KEY_NAME = google_kms_crypto_key.ga4_oauth_refresh_tokens.id
          }
          content {
            name  = env.key
            value = env.value
          }
        }
        env {
          name = "ANALYTICS_AI_GA4_OAUTH_CLIENT_SECRET"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.ga4_oauth_client_secret.secret_id
              version = var.ga4_oauth_client_secret_version
            }
          }
        }
      }
    }
  }
  depends_on = [google_project_service.required, google_kms_crypto_key_iam_member.runtime_ga4_oauth_tokens, google_secret_manager_secret_iam_member.runtime_ga4_oauth_secret]
}

resource "google_cloud_run_v2_job_iam_member" "schedule_invoker" {
  count    = var.enable_report_worker ? 1 : 0
  project  = var.platform_project_id
  location = var.region
  name     = google_cloud_run_v2_job.reports[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler[0].email}"
}

resource "google_cloud_scheduler_job" "reports" {
  count     = var.enable_report_worker ? 1 : 0
  name      = "analytics-ai-report-dispatch-${var.environment}"
  project   = var.platform_project_id
  region    = var.region
  schedule  = "*/5 * * * *"
  time_zone = "Etc/UTC"
  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.platform_project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.reports[0].name}:run"
    body        = base64encode("{}")
    headers     = { "Content-Type" = "application/json" }
    oauth_token {
      service_account_email = google_service_account.scheduler[0].email
    }
  }
  depends_on = [google_project_service.scheduler, google_cloud_run_v2_job_iam_member.schedule_invoker]
}
