locals {
  required_apis = toset([
    "analyticsadmin.googleapis.com",
    "analyticsdata.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudkms.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "firestore.googleapis.com",
    "firebase.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com"
  ])
}

resource "google_project_service" "required" {
  for_each           = local.required_apis
  project            = var.platform_project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_firebase_project" "platform" {
  provider = google-beta
  project  = var.platform_project_id

  depends_on = [google_project_service.required]
}

resource "google_firestore_database" "platform" {
  project                           = var.platform_project_id
  name                              = "(default)"
  location_id                       = var.firestore_location
  type                              = "FIRESTORE_NATIVE"
  delete_protection_state           = var.environment == "prod" ? "DELETE_PROTECTION_ENABLED" : "DELETE_PROTECTION_DISABLED"
  deletion_policy                   = "ABANDON"
  point_in_time_recovery_enablement = var.environment == "prod" ? "POINT_IN_TIME_RECOVERY_ENABLED" : "POINT_IN_TIME_RECOVERY_DISABLED"

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_firebase_project.platform]
}

resource "google_artifact_registry_repository" "containers" {
  project       = var.platform_project_id
  location      = var.region
  repository_id = "analytics-ai"
  format        = "DOCKER"

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket" "dataset_cache" {
  project                     = var.platform_project_id
  name                        = var.cache_bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_kms_key_ring" "ga4_oauth" {
  project  = var.platform_project_id
  name     = "analytics-ai-ga4-oauth-${var.environment}"
  location = var.region

  depends_on = [google_project_service.required]
}

resource "google_kms_crypto_key" "ga4_oauth_refresh_tokens" {
  name            = "ga4-oauth-refresh-tokens"
  key_ring        = google_kms_key_ring.ga4_oauth.id
  rotation_period = var.ga4_oauth_kms_rotation_period

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_secret_manager_secret" "ga4_oauth_client_secret" {
  project   = var.platform_project_id
  secret_id = "analytics-ai-ga4-oauth-client-secret-${var.environment}"

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_service_account" "runtime" {
  project      = var.platform_project_id
  account_id   = "analytics-ai-runtime"
  display_name = "Analytics AI Cloud Run runtime"
}

resource "google_service_account" "web_runtime" {
  project      = var.platform_project_id
  account_id   = "analytics-ai-web"
  display_name = "Analytics AI web runtime (no data access)"
}

resource "google_service_account" "data_access" {
  for_each     = var.data_access_profiles
  project      = var.platform_project_id
  account_id   = each.value.account_id
  display_name = each.value.display_name
}

resource "google_project_iam_member" "runtime_firestore" {
  project = var.platform_project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_project_iam_custom_role" "runtime_firebase_token_lookup" {
  project     = var.platform_project_id
  role_id     = "analyticsAiFirebaseTokenLookup"
  title       = "Analytics AI Firebase token revocation lookup"
  description = "Read the Firebase user record to check ID-token revocation."
  permissions = ["firebaseauth.users.get"]
}

resource "google_project_iam_member" "runtime_firebase_token_lookup" {
  project = var.platform_project_id
  role    = google_project_iam_custom_role.runtime_firebase_token_lookup.name
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_project_iam_member" "runtime_logging" {
  project = var.platform_project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_project_iam_member" "web_runtime_logging" {
  project = var.platform_project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.web_runtime.email}"
}

resource "google_storage_bucket_iam_member" "runtime_cache" {
  bucket = google_storage_bucket.dataset_cache.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_kms_crypto_key_iam_member" "runtime_ga4_oauth_tokens" {
  crypto_key_id = google_kms_crypto_key.ga4_oauth_refresh_tokens.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_secret_manager_secret_iam_member" "runtime_ga4_oauth_secret" {
  project   = var.platform_project_id
  secret_id = google_secret_manager_secret.ga4_oauth_client_secret.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_project_iam_member" "data_access_query_jobs" {
  for_each = var.data_access_profiles
  project  = var.platform_project_id
  role     = "roles/bigquery.jobUser"
  member   = "serviceAccount:${google_service_account.data_access[each.key].email}"
}

resource "google_project_iam_member" "data_access_mcp_tools" {
  for_each = var.data_access_profiles
  project  = var.platform_project_id
  role     = "roles/mcp.toolUser"
  member   = "serviceAccount:${google_service_account.data_access[each.key].email}"
}

resource "google_service_account_iam_member" "runtime_can_impersonate" {
  for_each           = var.data_access_profiles
  service_account_id = google_service_account.data_access[each.key].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_cloud_run_v2_service" "analysis_api" {
  count    = var.backend_image == "" ? 0 : 1
  project  = var.platform_project_id
  name     = "analytics-ai-api-${var.environment}"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  deletion_protection = var.environment == "prod"

  template {
    service_account = google_service_account.runtime.email
    timeout         = "360s"

    scaling {
      min_instance_count = var.environment == "prod" ? 1 : 0
      max_instance_count = var.environment == "dev" ? 3 : 20
    }

    containers {
      image = var.backend_image

      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
      }

      env {
        name  = "ANALYTICS_AI_ENVIRONMENT"
        value = var.environment
      }
      env {
        name  = "ANALYTICS_AI_FIREBASE_PROJECT_ID"
        value = var.platform_project_id
      }
      env {
        name  = "ANALYTICS_AI_PLATFORM_PROJECT_ID"
        value = var.platform_project_id
      }
      env {
        name  = "ANALYTICS_AI_CACHE_BUCKET"
        value = google_storage_bucket.dataset_cache.name
      }
      env {
        name  = "ANALYTICS_AI_ENABLE_ANALYTICS_TOOLS"
        value = tostring(var.enable_analytics_tools)
      }
      env {
        name  = "ANALYTICS_AI_ENABLE_BROWSER_QWEN"
        value = tostring(var.enable_browser_qwen)
      }
      env {
        name  = "ANALYTICS_AI_ENABLE_BROWSER_AGENT"
        value = tostring(var.enable_browser_agent)
      }
      env {
        name  = "ANALYTICS_AI_VERTEX_USER_IDS"
        value = jsonencode(sort(tolist(var.vertex_user_ids)))
      }
      env {
        name  = "ANALYTICS_AI_ENABLE_BIGQUERY_TOOLS"
        value = "false"
      }
      env {
        name  = "ANALYTICS_AI_VERTEX_MODEL"
        value = var.vertex_model
      }
      env {
        name  = "ANALYTICS_AI_VERTEX_LOCATION"
        value = var.vertex_location
      }
      env {
        name  = "ANALYTICS_AI_ENABLE_ADMIN_CONNECTION_CHECKS"
        value = tostring(var.enable_admin_connection_checks)
      }
      env {
        name  = "ANALYTICS_AI_ENABLE_GA4_USER_OAUTH"
        value = tostring(var.enable_ga4_user_oauth)
      }
      env {
        name  = "ANALYTICS_AI_GA4_OAUTH_CLIENT_ID"
        value = var.ga4_oauth_client_id
      }
      env {
        name  = "ANALYTICS_AI_GA4_OAUTH_REDIRECT_URI"
        value = var.ga4_oauth_redirect_uri
      }
      env {
        name  = "ANALYTICS_AI_GA4_OAUTH_RETURN_URL"
        value = var.ga4_oauth_return_url
      }
      env {
        name  = "ANALYTICS_AI_GA4_OAUTH_KMS_KEY_NAME"
        value = google_kms_crypto_key.ga4_oauth_refresh_tokens.id
      }
      dynamic "env" {
        for_each = var.enable_ga4_user_oauth ? [1] : []
        content {
          name = "ANALYTICS_AI_GA4_OAUTH_CLIENT_SECRET"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.ga4_oauth_client_secret.secret_id
              version = var.ga4_oauth_client_secret_version
            }
          }
        }
      }
      env {
        name  = "ANALYTICS_AI_ALLOW_INSECURE_DEV_AUTH"
        value = "false"
      }
      env {
        name  = "ANALYTICS_AI_CORS_ORIGINS"
        value = jsonencode([var.web_origin])
      }
    }
  }

  depends_on = [
    google_project_service.required,
    google_project_iam_member.runtime_firebase_token_lookup,
    google_kms_crypto_key_iam_member.runtime_ga4_oauth_tokens,
    google_secret_manager_secret_iam_member.runtime_ga4_oauth_secret,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "analysis_api_public" {
  count    = var.backend_image == "" ? 0 : 1
  project  = var.platform_project_id
  location = google_cloud_run_v2_service.analysis_api[0].location
  name     = google_cloud_run_v2_service.analysis_api[0].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service" "web" {
  count    = var.web_image == "" ? 0 : 1
  project  = var.platform_project_id
  name     = "analytics-ai-web-${var.environment}"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  deletion_protection = var.environment == "prod"

  lifecycle {
    # Firebase Hosting pins each release with a Cloud Run traffic tag.
    ignore_changes = [traffic]

    precondition {
      condition = (
        can(regex("^AIza[0-9A-Za-z_-]{35}$", var.firebase_web_config.api_key)) &&
        can(regex("^1:[0-9]+:web:[0-9a-f]+$", var.firebase_web_config.app_id))
      )
      error_message = "Use the Firebase Web App SDK configuration for this project, not example values."
    }
  }

  template {
    service_account = google_service_account.web_runtime.email

    scaling {
      min_instance_count = 0
      max_instance_count = var.environment == "dev" ? 3 : 20
    }

    containers {
      image = var.web_image
      env {
        name  = "ORIGIN"
        value = var.web_origin
      }

      env {
        name  = "PUBLIC_API_BASE_URL"
        value = var.public_api_base_url != "" ? var.public_api_base_url : one(google_cloud_run_v2_service.analysis_api[*].uri)
      }
      env {
        name  = "PUBLIC_FIREBASE_API_KEY"
        value = var.firebase_web_config.api_key
      }
      env {
        name  = "PUBLIC_FIREBASE_AUTH_DOMAIN"
        value = var.firebase_web_config.auth_domain
      }
      env {
        name  = "PUBLIC_FIREBASE_PROJECT_ID"
        value = var.platform_project_id
      }
      env {
        name  = "PUBLIC_FIREBASE_APP_ID"
        value = var.firebase_web_config.app_id
      }
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_service_iam_member" "web_public" {
  count    = var.web_image == "" ? 0 : 1
  project  = var.platform_project_id
  location = google_cloud_run_v2_service.web[0].location
  name     = google_cloud_run_v2_service.web[0].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
