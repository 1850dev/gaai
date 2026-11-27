resource "google_project_service" "vertex_ai" {
  project            = var.platform_project_id
  service            = "aiplatform.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_iam_member" "runtime_vertex_ai" {
  project = var.platform_project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.runtime.email}"

  depends_on = [google_project_service.vertex_ai]
}
