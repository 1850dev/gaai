variable "platform_project_id" {
  description = "Platform-owned Google Cloud project. Production must be analytics-ai-prod."
  type        = string

  validation {
    condition     = var.environment != "prod" || var.platform_project_id == "analytics-ai-prod"
    error_message = "Production query execution must use analytics-ai-prod."
  }
}

variable "environment" {
  description = "Deployment environment."
  type        = string

  validation {
    condition     = contains(["dev", "stg", "prod"], var.environment)
    error_message = "environment must be dev, stg, or prod."
  }
}

variable "region" {
  description = "Cloud Run and Artifact Registry region."
  type        = string
  default     = "asia-northeast1"
}

variable "firestore_location" {
  description = "Firestore database location."
  type        = string
  default     = "asia-northeast1"
}

variable "cache_bucket_name" {
  description = "Globally unique private dataset result bucket."
  type        = string
}

variable "backend_image" {
  description = "Immutable analysis-api container image reference. Empty skips Cloud Run creation."
  type        = string
  default     = ""
}

variable "web_image" {
  description = "Immutable web container image reference. Empty skips Cloud Run creation."
  type        = string
  default     = ""
}

variable "web_origin" {
  description = "Allowed browser origin for backend CORS."
  type        = string
  default     = "http://localhost:5173"
}

variable "public_api_base_url" {
  description = "Browser-visible analysis API origin. Empty derives it from the Cloud Run API service in this configuration."
  type        = string
  default     = "http://localhost:8080"
}

variable "firebase_web_config" {
  description = "Firebase public web configuration; these values are identifiers, not credentials."
  type = object({
    api_key     = string
    auth_domain = string
    app_id      = string
  })
  sensitive = true
}

variable "data_access_profiles" {
  description = "One distinct impersonated service account per company/data access profile."
  type = map(object({
    account_id   = string
    display_name = string
  }))
  default = {}
}

variable "enable_analytics_tools" {
  description = "Fail-closed feature gate. Enable only after access grants and scope configuration are reviewed."
  type        = bool
  default     = false
}

variable "enable_browser_qwen" {
  description = "Use browser-local Qwen for analysis unless the Firebase UID is explicitly allowed to use Vertex."
  type        = bool
  default     = false

  validation {
    condition     = !var.enable_browser_qwen || (var.enable_analytics_tools && var.enable_ga4_user_oauth)
    error_message = "Browser Qwen requires analytics tools and GA4 user OAuth."
  }
}

variable "vertex_user_ids" {
  description = "Explicit Firebase UID allowlist for Vertex when browser Qwen is enabled. An empty set disables server inference for all users."
  type        = set(string)
  default     = []
}

variable "enable_browser_agent" {
  description = "Enable the browser-driven shared GA4 agent after authenticated WebSocket acceptance."
  type        = bool
  default     = false

  validation {
    condition     = !var.enable_browser_agent || var.enable_browser_qwen
    error_message = "The browser agent requires browser Qwen."
  }
}

variable "vertex_model" {
  description = "Stable Gemini model used by both the API and scheduled report worker."
  type        = string
  default     = "gemini-3.8-flash"
}

variable "vertex_location" {
  description = "Vertex publisher model endpoint location; confirm residency requirements before changing it."
  type        = string
  default     = "global"
}

variable "enable_admin_connection_checks" {
  description = "Fail-closed admin onboarding gate. It never enables message analysis tools."
  type        = bool
  default     = false
}

variable "enable_ga4_user_oauth" {
  description = "Fail-closed gate for per-user GA4 delegated OAuth. It does not enable analytics execution."
  type        = bool
  default     = false
}

variable "ga4_oauth_client_id" {
  description = "Google OAuth Web application client ID. The client secret is stored only in Secret Manager."
  type        = string
  default     = ""

  validation {
    condition     = !var.enable_ga4_user_oauth || var.ga4_oauth_client_id != ""
    error_message = "ga4_oauth_client_id is required when GA4 user OAuth is enabled."
  }
}

variable "ga4_oauth_kms_rotation_period" {
  description = "KMS key rotation period. Pass null when adopting an existing key with no rotation schedule."
  type        = string
  default     = "7776000s"
}

variable "ga4_oauth_client_secret_version" {
  description = "Secret Manager version of the GA4 OAuth Web client secret; use a numeric version for a reproducible rollout."
  type        = string
  default     = "latest"

  validation {
    condition     = var.ga4_oauth_client_secret_version == "latest" || can(regex("^[1-9][0-9]*$", var.ga4_oauth_client_secret_version))
    error_message = "Use latest or a positive numeric Secret Manager version."
  }
}

variable "ga4_oauth_redirect_uri" {
  description = "Exact backend OAuth callback URI registered on the Google OAuth Web client."
  type        = string
  default     = "http://localhost:8080/api/integrations/ga4/callback"

  validation {
    condition     = can(regex("^https://", var.ga4_oauth_redirect_uri)) || var.environment == "dev"
    error_message = "stg/prod GA4 OAuth redirect URI must use HTTPS."
  }
}

variable "ga4_oauth_return_url" {
  description = "Frontend URL used after the backend completes the OAuth callback."
  type        = string
  default     = "http://localhost:5173"

  validation {
    condition     = can(regex("^https://", var.ga4_oauth_return_url)) || var.environment == "dev"
    error_message = "stg/prod GA4 OAuth return URL must use HTTPS."
  }
}
