# Deployment

Google Auth Platformの外部公開に向けたホーム／ポリシー／利用規約と、審査前の確認は[ブランディング手順](google-auth-branding.md)を参照。

## Scheduled reports

`enable_report_worker=false` by default. `scheduler.tf` optionally provisions a Cloud Run Job and a Scheduler-only invoker identity. The trigger dispatches every five minutes; the internal worker claims due user schedules with Firestore CAS and rechecks current access. No public cron endpoint or fake Firebase token is used. Production uses Firestore + GCS. The `filesystem` cache is restricted to local/test and must never be deployed to Cloud Run. Cloud provisioning remains unapplied; the current local environment uses the user-approved Docker shared volume instead. See [Agent Harness](agent-harness.md).

Use separate `analytics-ai-dev`, `analytics-ai-stg`, and `analytics-ai-prod` projects. Never reuse runtime accounts, buckets, Firebase projects, or profile service accounts across environments. Production query execution is validated to `analytics-ai-prod`.

The current CI runs Python lint/format/type/tests, Svelte type/lint/unit/build/E2E checks, and Terraform format/provider validation. Before production promotion, add organization-required dependency review, secret scanning, container vulnerability scanning, signed immutable images, and real dev/staging acceptance jobs.

Terraform creates platform services, Firestore, a private cache bucket, Artifact Registry, Cloud KMS, a Secret Manager secret container, separate Backend/Web runtime identities, distinct BigQuery data-access service accounts, Backend-only target-specific impersonation, and optional Cloud Run services. Standard GA4 uses delegated user OAuth and requires no customer-side Service Account grant. Terraform intentionally does not create customer-side dataset/view Data Viewer grants.

The Firebase project and default Firestore database already created during development must be imported into the environment's Terraform state before the first plan. Never accept a plan that replaces the default database. The Web runtime must remain Logging-only; any Token Creator, Firestore, Cache, Vertex, GA4, BigQuery, or customer-resource role on it blocks deployment.

Add the GA4 OAuth Web Client secret directly as a Secret Manager version; never place its value in tfvars or Terraform state. Register the exact Cloud Run callback URI on the OAuth Client before enabling `enable_ga4_user_oauth`. Build immutable images, deploy to dev with `enable_analytics_tools = false`, run OAuth／KMS／Property-denial and BigQuery acceptance, and only then promote the same image digests. Cloud deployment and `terraform apply` require explicit operational approval.

## Firestore rules and indexes

Firestore rules and composite indexes are managed by `firebase.json` and the files under `firebase/`. Deploy only the intended target and wait for every composite index to reach `READY` before application acceptance.

```bash
firebase deploy --only firestore:rules,firestore:indexes --project YOUR_TARGET_PROJECT_ID
gcloud firestore indexes composite list --project=YOUR_TARGET_PROJECT_ID
```

For a narrowly scoped index-only change, use `--only firestore:indexes`. Never infer the production target from a developer default project.

## Rollout gates

2026-09-30 GA4 Pilot判定：**NO-GO**。ローカルS1c承認移行・R1a実装を反映したが、A1の実Identity失効／metadata復旧、R1b途中切断、Q3品質残課題、コンテナ／Backend脆弱性検査、リモートCI、固定artifactのstg昇格／rollbackは未完。ローカルimage IDはregistry manifest digestではなく、そのまま本番リリース証跡に使わない。Docker ScoutのSBOM外部送信は許可確認待ち。BigQuery・新規GCS・本番公開の保留を維持する。[最新記録](evaluation/s1c-r1-a1-release-2026-09-30.md)

- Identity and negative Data Source Set authorization pass at UI, API, Session, Gateway, and downstream-call layers.
- Concurrent GA4 User A/B OAuth isolation, same-account binding, revoke, and Property 403 pass.
- Explicit GA4 Service Account mode and BigQuery target-specific impersonation pass separately.
- BigQuery unauthorized resource, missing date, star, cross-join, DML, dry-run, and byte-budget tests pass.
- Cache blobs and Evidence remain scoped and traceable.
- Logs correlate request, session, MCP tool, query, and Evidence without tokens, prompts, PII, or result rows.
- Production dependency/container scans have no unresolved release blockers.
- Rollback image, Firestore schema compatibility, backup, and recovery are verified.
# Firebase Hosting — GA4開発Pilot

利用者の実機確認先は運営者が指定するFirebase Hosting開発サイトとする。ローカルや候補リビジョンだけで検証した場合は、実機確認用の公開サイトへの反映とは区別する。

GA4 Pilotでは、Firebase HostingからSvelteKitのWebサービスだけを配信する。分析APIとGA4 OAuth callbackはブラウザからCloud RunのAPI originへ直接接続する。Firebase HostingのCloud Run rewriteは60秒でタイムアウトするため、最大300秒のAgent SSEをHostingに中継しない。[Firebaseの制限](https://firebase.google.com/docs/hosting/cloud-run#connect-cloud-run-to-firebase-hosting)

`firebase.hosting.dev.example.json` をGit管理外の `firebase.hosting.dev.json` にコピーし、サイトID・リージョン・Webサービス名を対象環境に合わせる。既存の `firebase.json` はFirestore専用のままにし、HostingとFirestore Rules／Indexesの変更を分離する。HostingのpinTagはWeb revisionを固定するが、APIやWorkerのrollbackは別途必要。

開発版の端末内Qwenを維持する環境変数は `ANALYTICS_AI_ENABLE_BROWSER_QWEN=true` と `ANALYTICS_AI_VERTEX_USER_IDS=[]`。Terraformでは `enable_browser_qwen` と `vertex_user_ids` を対応させ、Workerを有効にする場合もAPIと同じ判定を使う。Cloud Runを直接更新した場合は、次のTerraform planで意図せず戻さないよう反映済みdigestとフラグを確認する。実際の配備内容・切り戻し先はGit管理外の運用記録に保存する。

共有エージェントを使う拡張版は別フラグ `ANALYTICS_AI_ENABLE_BROWSER_AGENT`（Terraform: `enable_browser_agent`）で管理する。既定はOFF。既存OAuth・IAM・BigQueryの有効化設定は変更しない。候補APIの直接WebSocket接続と、許可外Origin・未認証・別UIDの拒否を検証したうえで、[実機受入手順](evaluation/qwen-ga4-acceptance.md)に従って公開を判断する。共有経路を無効化した場合、従来の単一Skill経路が残る。

### 最初の配備手順

1. 承認された開発用Firebaseプロジェクト・課金（Blaze）・リージョンを確認する。Cloud Run、Artifact Registry、Secret Manager、Firestore、KMS、Vertex、Hostingを使用する。GCSの新規作成が未承認なら進めない。本アプリのクラウド構成は非公開GCSを永続キャッシュとして必須とし、Dockerのローカル共有volumeでは代替しない。
2. 既存Firebase／FirestoreをTerraform stateにimportし、previewしたplanで破壊的変更がないことを確認。BigQueryのAPI・Profileは新規設定しない。Web／APIのイメージはcommitに対応するimmutable digestを指定する。OAuth Client SecretはSecret Managerへ直接設定し、tfvars・Terraform state・ブラウザへ入れない。
3. Webの `PUBLIC_API_BASE_URL` はAPIのHTTPS origin（`public_api_base_url = ""` のときTerraformで導出）。APIの `web_origin` とOAuth return URLは公開するHosting origin、redirect URIは `https://<APIホスト>/api/integrations/ga4/callback` とする。Firebase AuthenticationのAuthorized domainsにHostingホストを追加し、GA4 OAuthクライアントへ正確なredirect URIを登録する。Firebase ID Tokenの失効確認はAPIの実行IDにのみ `firebaseauth.users.get` を付与し、Web実行IDには付与しない。
4. Firebase ConsoleまたはFirebase Management APIの対象Web AppからSDK設定を取得し、配備予定のAPIキー・App ID・認証ドメイン・Project IDが一致することを確認する。配備先の参照元でFirebase Auth設定取得が200になることも確認する。非空であることやWebページの200だけではログインが使えるとは判断しない。
5. Analytics Toolsと定期実行をOFFのままWeb・APIを立てる。APIの `/health` 成功、未認証データ要求401、Hosting originだけのCORS許可、Firebase Google Loginを確認する。旧 `/healthz` はローカル互換用で、Cloud Runの予約パスと衝突するためクラウドでは使わない。
6. 承認済みの開発サイトを確認後、`firebase deploy --only hosting --config firebase.hosting.dev.json --project YOUR_PROJECT_ID` を実行する。HostingがWeb revisionをpinしているため、Web環境変数だけを変えた場合もHosting版を再配備する。Hosting公開後の実ログイン試験・管理ユーザーと分析ユーザーの分離、GA4 Scope・OAuth失効、SSEと復旧を確認してから、明示承認された検証ユーザーに限り分析を開ける。新規会社の登録は[企業オンボーディング](onboarding-company.md)を参照。
7. WebのHosting rollback、APIの以前のrevisionへの切戻し、Analytics／Workerの停止を実環境で試験する。正式公開は[リリース準備計画](release-readiness-plan.md)の受入条件を満たしてから行う。
