# GA4 Analytics AI

GA4のデータを、自然言語の会話から分析し、根拠付きの回答とグラフを残すWebアプリケーションです。

## 端末内分析（Qwen3.5 4B・任意のローカル試験）

GA4のGoogle OAuthと閲覧許可が設定済みの開発環境で、ルートの `.env` に次を設定し、API・Webを再ビルドしてください。既定は無効です。

```dotenv
ANALYTICS_AI_ENABLE_BROWSER_QWEN=true
ANALYTICS_AI_VERTEX_USER_IDS=[]
```

分析には既存の `ANALYTICS_AI_ENABLE_ANALYTICS_TOOLS=true` も必要です。 `docker compose -f compose.yaml -f compose.firestore.yaml up --build -d` の後、[ローカル画面](http://localhost:5174/)にログインします。端末内分析の対象ユーザーは、入力前にWebGPU（shader-f16）上でモデルを準備します。初回は[固定版ONNXモデル](https://huggingface.co/onnx-community/Qwen3.5-4B-ONNX-OPT/tree/bb072268d0912bdc5cf4f41c8c6f53a03fd68624)を約2.8 GB取得し、ブラウザキャッシュの消去・容量不足時には再取得が必要です。準備完了後に「2026年10月の日別PVを見せて」と入力し、計画を確認して「はい」と返信します。

Qwenは端末内で単一指標のKPI・推移・ランキング・期間比較の計画を作ります。GA4取得・権限検証・集計・グラフと根拠の保存は従来どおりサーバー側です。結果表示後にAIの解説が必要かチャットで確認されます。「はい」「OK」で、取得済み結果の短い解説を端末内で作ります。解説の履歴保存やGA4の再取得は行いません。同じ画面にいる間は読み込み済みモデルを再利用し、生成が止まった場合も自動で再読み込み・再試行しません。モデル配布元に質問・GA4結果を送る機能はありません。確認済みの質問・計画は分析実行時に本サービスのAPIに送られます。

複数指標・集計軸や段階分析については、既存のGA4エージェントをQwenでも利用する経路を検証中です。従来版とは別のフラグで管理し、結果をGeminiへ自動転送しません。[実機確認用の質問と合格条件](docs/evaluation/qwen-ga4-acceptance.md)

共有エージェントでは、GA4の指標・軸の互換性エラーや取得行数の超過に対し、失敗理由を端末内モデルへ渡して修正を1回だけ検討します。期間、比較期間、粒度、フィルタ、明示された指標・軸が変わらず、別の条件であることをサーバーで検証します。再取得は変更内容を提示し、承認を受けた場合に限ります。認可・OAuth・タイムアウトによる失敗は、このループで再送しません。

フラグ有効時は明示したFirebase UIDのみ従来のVertex経路を利用できます（課金プラン自動判定ではありません）。無料経路は非対応端末でもクラウド推論に切り替えません。無人定期実行・複数Skillの自律ループ・新しいフィルタの設定はこの初版の対象外です。APIと定期実行ワーカーは同じ環境設定で起動してください。

「2026年11月のPVランキングを上位10件で表にしてください」のように年月・指標・件数が明示されたページ別PVランキングは、登録済みの指標とページ軸を使い、モデルを起動せずに計画を作れます。この場合もGA4取得はチャットで利用者が承認したあとに実行します。

実機検証は `apps/web` で `npm run build` の後、`node qwen-smoke.mjs --download`（Playwright Chromium、または環境変数 `QWEN_BROWSER_CHANNEL=msedge`）。一時プロファイルにモデルを取得し、アプリと同じ指示と検証関数で合成質問のSkill・期間・指標・粒度を突合します。FirebaseやGA4にはアクセスしません。

単なるGA4チャットボットではなく、Semantic Layer、Analysis State、Data Source Set単位の認可、Dataset Cache、Evidence、可視化を統合したConversational Analytics Platformを目指しています。

> [!IMPORTANT]
> 開発Pilotとソースコードの一般公開は別です。運営環境のURL・設定・分析データはサンプル設定に含めません。[リポジトリ公開前の確認](docs/repository-publication.md)を参照してください。

GA4接続モーダルのProperty一覧は接続確認用です。分析はヘッダーの登録済みData Source Setを選んで「新しい分析」から開始します。Google側で閲覧できても、未登録・未GrantのPropertyはそのまま分析対象にできません。[現在のS1 → R1 → A1 → REL計画](docs/release-readiness-plan.md)

## 現在実装されているもの

- 会話の作業記憶、認可済みの過去分析検索、ユーザー確認式の長期合意管理（[使い方](docs/agent-harness.md)）
- 上位対象を確定→対象別の内訳→評価を一度の依頼で自動実行する段階分析（単一期間・1指標・2軸、最大10対象。進捗保存・安全上限あり）
- 会話をまたぐ調査エージェント（確認質問→複数分析→結果観察→追加調査→レポート／改善仮説）
- 分析回答のMarkdown保存、見出し・表のプレビュー、原文表示・コピー・.md保存（通常回答の保存はレポート作成とは別機能）
- 個人×分析対象のサイト基本情報・事業ゴール・変更可能画面・コンテンツ方針設定、Property非表示
- 根拠付きレポートの保存・Markdown出力、施策の具体化相談、週次／月次レポートWorker
- SvelteKitによるGoogle Login、Data Source Set選択、Chat、Analysis State、Evidence、KPI／Table／EChartsのUI
- Firebase ID Token検証とFirebase UIDによるユーザー識別
- GA4用Backend OAuth Authorization Code Flow、PKCE、one-use state、同一Google Account照合
- Cloud KMSによるユーザー別GA4 Refresh Token暗号化と接続解除／revoke
- ユーザー本人の権限範囲によるGA4 Property discovery
- Company所属とは分離されたData Source Set単位のUser Access Grant
- Analysis SessionへのData Source Set固定と再認可
- Service Account Impersonationを前提としたCredential Broker
- GA4 MCPのリクエスト単位Credential分離とProperty／Stream Scope強制
- Semantic Layer、Analysis State継承、Query Router、構造化Planner
- version付きGA4 Analysis Skill Registry、複数KPI／Metric、期間合計・平均・最新値、Realtime、登録済みFunnel、median/MAD異常検知
- 集客・ページ・属性／地域・技術環境・イベント・時系列の共通Capability、明示した指標・集計軸・時間粒度の完了判定
- 同一カテゴリ／複合集計軸の期間比較とコード計算済み内訳の保存・回答、複数期間の安全な件数検査
- Core reportの実行前Compatibility検査、Property quota、sampling／thresholding／(other)品質情報のEvidence／Audit化
- Admin-onlyのProperty固有custom dimension／metric候補取得（自動公開なし）
- Message APIのPlanner→Gateway→Cache→Finding／Evidence統合とFirestore transaction
- POST SSEによる型付き分析イベント、画面上の進捗反映、Frontend Stream Reader
- Message単位のat-most-once Idempotency、分析Timeout、Cancellation監査
- GA4 read-only一時障害だけに限定したbounded retry（BigQuery本実行は自動再試行しない）
- Firestore MetadataとCloud Storage BlobによるDataset Cache
- コード側での数値計算、Evidence、Visualization Specification
- 数値の書き換えを許さないEvidence-bound Vertex回答整形と決定論的fallback
- deny-by-defaultのFirestore Security Rules
- Docker、Docker Compose、Terraform、GitHub Actions
- Unit、Integration、E2E TestとArchitecture／Securityドキュメント

## 未完了・実環境未検証のもの

- 実ブラウザでの通信切断・実行中再起動、実アカウント2名のGA4 OAuth分離・失効、バックアップ復元
- 実GA4はWeb限定の複数ターン・数値突合まで検証済み。全Skillと改善提案の総合品質は未検証
- Firebase Hosting配備後の実Googleログイン／GA4受入、ステージングでの昇格・ロールバック

## 今後の機能追加予定

- **BigQuery（Pending）**：実環境への接続、Service Accountごとの権限受入、Query Guard・コスト上限・MCPの実検証。GA4 Pilotでは使用できません。既存の接続・検証コードは保持し、`ANALYTICS_AI_ENABLE_BIGQUERY_TOOLS=false` を維持します。
- CVR計算・要因分解の拡張、Custom Metadataの承認とSemantic Version管理、運用管理画面の拡張

詳細は[GA4-first TODO](docs/todo.md)、[Web限定データの実受入](docs/evaluation/q2b-completion-2026-09-25.md)、[Pilot配備手順](docs/deployment.md)、[企業オンボーディング](docs/onboarding-company.md)を参照してください。

## ローカルでの定期実行

現在のローカル永続保存・定期実行構成は次のコマンドです。Cloud Storageを新規作成せず、Docker共有volumeを利用します。PCとDockerが稼働していれば、ブラウザを閉じても定期実行できます。

```bash
docker compose -f compose.yaml -f compose.firestore.yaml -f compose.scheduler.yaml up --build -d
```

`down -v`は結果データを失うため実行しないでください。

## 最重要セキュリティ原則

```text
Application Identity = Firebase UID
GA4 Standard Identity = 同じGoogle AccountのUSER_OAUTH
BigQuery Identity = Data Access ProfileごとのService Account
GA4 Optional Identity = 明示的に選択したService Account
```

GA4とBigQueryの認証を混同しません。GA4は標準でログインユーザー本人が追加同意した`analytics.readonly` OAuthを使います。BigQueryはCloud Run RuntimeからData Access Service Accountをkeyless impersonationします。GA4 `USER_OAUTH`からService Accountへの自動fallbackは禁止です。

データアクセスは、次の条件をすべて満たした場合だけ許可します。

```text
GA4: Google UserがPropertyを閲覧可能
     AND Data Source SetにPropertyが登録済み
     AND User Access Grantが存在

BigQuery: Service AccountがDataset／Viewへアクセス可能
          AND Data Source SetにResourceが登録済み
          AND User Access Grantが存在
```

LLMにはProperty、Project、Dataset、Credential、MCP Toolを自由選択させません。認可済みScopeからBackendが強制的に注入します。

## アーキテクチャ

```text
End User
  → Firebase Authentication
  → SvelteKit Web Application
  → FastAPI Backend
  → Authorization Guard
  → Analysis Orchestrator
      ├─ Semantic Layer
      ├─ Analysis State
      ├─ Query Router
      ├─ Dataset Cache
      ├─ Evidence
      ├─ Visualization Planner
      └─ MCP Gateway
          ├─ Google Analytics MCP Adapter
          └─ BigQuery Remote MCP + Query Guard
```

## Repository構成

```text
apps/web/                    SvelteKit、Firebase Web SDK、ECharts
services/analysis-api/       FastAPI、認可、分析、MCP Gateway
packages/contracts/          Frontend／Backend共通JSON Schema
infra/terraform/             Google Cloud Infrastructure as Code
firebase/                    Firestore Security RulesとIndex
docs/                        Architecture、Security、運用ドキュメント
tests/                       Acceptance Test用領域
```

## ローカル起動

Docker Composeでの起動を推奨します。DockerイメージはPython 3.12.16とNode.js 24.22.3 LTSを使用します。GA4分析の既定モデルは `gemini-3.8-flash` です。既存のローカル `.env` にモデル指定があれば、そちらが優先されます。

### 1. 環境ファイルを作成

Repositoryルートで実行します。

```bash
cp -n .env.example .env
```

Firebase未設定のままでも、安全なConfiguration Required画面とBackendの起動確認はできます。

### 2. Dockerを起動

```bash
docker compose up --build
```

バックグラウンドで起動する場合は次を使用します。

```bash
docker compose up --build -d
docker compose logs -f
```

確認先は以下です。

- Web（Docker）: <http://localhost:5174>
- Backend Health: <http://localhost:8080/healthz>
- FastAPI API Docs: <http://localhost:8080/docs>

停止する場合は次を実行します。

```bash
docker compose down
```

### 3. Firebase Google Loginを有効化

開発用FirebaseプロジェクトでWeb Appを登録し、Google Loginを有効にします。Repositoryルートの`.env`へFirebase Web Appの公開設定を入力してください。

```dotenv
ANALYTICS_AI_ENVIRONMENT=local
ANALYTICS_AI_FIREBASE_PROJECT_ID=実際のFirebaseプロジェクトID
ANALYTICS_AI_PLATFORM_PROJECT_ID=実際の開発用GCPプロジェクトID

PUBLIC_API_BASE_URL=http://localhost:8080
PUBLIC_FIREBASE_API_KEY=...
PUBLIC_FIREBASE_AUTH_DOMAIN=...
PUBLIC_FIREBASE_PROJECT_ID=実際のFirebaseプロジェクトID
PUBLIC_FIREBASE_APP_ID=...
```

設定後、Frontendへ公開設定を反映するため再ビルドします。

```bash
docker compose up --build -d
```

Firebase Web App設定は公開識別子です。Service Account KeyとAccess Tokenは保存しないでください。ローカルGA4 OAuth検証に限りOAuth Web Client SecretをGit-ignoredの`.env`へ設定できますが、dev／stg／prodではSecret Managerを使用します。

Backend検証済みFirebase UIDの診断、dev Firestore用Compose override、監査付きcreate-only bootstrapを実装済みです。初期登録は[ローカル開発](docs/local-development.md)の「dev Firestoreへ安全に接続する」と「最初のUser／CompanyをBootstrapする」を順に実施してください。Data Access ProfileとData Source Setは、実リソースの接続検証が完了するまで無効状態を維持します。

## Dockerを使わない開発

サポートする標準開発環境はPython 3.12とNode.js 24です。現在のhash lockにはPython 3.11以上を要求する依存関係が含まれるため、Python 3.10は使用しないでください。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r services/analysis-api/requirements-dev.lock
python -m pip install --no-deps -e services/analysis-api
npm --prefix apps/web ci
```

Backendを起動します。

```bash
uvicorn analytics_ai.api.app:app \
  --app-dir services/analysis-api/src \
  --reload \
  --port 8080
```

別ターミナルでFrontendを起動します。Viteで起動する場合、Frontend用の公開設定は`apps/web/.env.local`にも配置してください。

```bash
cp -n .env.example apps/web/.env.local
npm --prefix apps/web run dev
```

## Analytics ToolのFeature Gate

Analytics実行はデフォルトで無効です。

```dotenv
ANALYTICS_AI_ENABLE_ANALYTICS_TOOLS=false
ANALYTICS_AI_ENABLE_ADMIN_CONNECTION_CHECKS=false
ANALYTICS_AI_ENABLE_GA4_USER_OAUTH=false
ANALYTICS_AI_ALLOW_INSECURE_DEV_AUTH=false
```

Firebase検証、Firestore登録、GA4 User OAuth／BigQuery Data Access Profile、IAM、接続検証、Data Source Set分離のAcceptance Testが完了するまで`ANALYTICS_AI_ENABLE_ANALYTICS_TOOLS=true`にしないでください。

`true`にすると、Message endpointは実際のVertex Planner、MCP Gateway、Credential Broker、Dataset Cacheを組み立てます。Mockテストが通っていることだけを理由に有効化せず、実Firebase／Firestore、GA4 User OAuth、Admin登録済みProperty／Stream／Grant、BigQuery再開後はProfile単位IAMの接続検証が完了するまで`false`を維持してください。

`ANALYTICS_AI_CACHE_BACKEND=auto`はlocal／testではin-memory cache、dev／stg／prodではFirestore metadata＋Cloud Storage blobを選択します。非local環境でmemory cacheを指定すると起動を拒否します。

Chat入力は通常のEnterで改行し、`Ctrl + Enter`（macOSでは`⌘ + Enter`）で送信できます。Session一覧の削除操作は物理削除ではなく、所有者確認・楽観的排他・Audit Eventを伴う復元可能なarchiveです。

ユーザー別GA4接続は`ANALYTICS_AI_ENABLE_GA4_USER_OAUTH`で独立制御します。有効化にはOAuth Web Client、exact callback URI、Secret、Cloud KMS keyが必要です。このgateを有効にしてもMessage分析は有効になりません。登録済みPropertyの検証は別の`ANALYTICS_AI_ENABLE_ADMIN_CONNECTION_CHECKS`で制御します。

## 品質チェック

```bash
python -m ruff check services/analysis-api
python -m ruff format --check services/analysis-api
python -m mypy --config-file services/analysis-api/pyproject.toml services/analysis-api/src
python -m pytest -s services/analysis-api/tests

npm --prefix apps/web run check
npm --prefix apps/web run lint
npm --prefix apps/web run test
npm --prefix apps/web run build
npm --prefix apps/web run test:e2e

terraform -chdir=infra/terraform fmt -check -recursive
terraform -chdir=infra/terraform validate
```

## 関連ドキュメント

- [最新のDual-MCP製品要件 v1.2](docs/GA4_BigQuery_Dual_MCP_Analytics_AI_Requirements_v1.2.md)
- [ローカル分析品質評価（2026-09-16）](docs/evaluation/local-analysis-quality-2026-09-16.md)

- [最新の機能要件・システム構成書 v1.2](docs/GA4_BigQuery_Analytics_AI_Functional_Requirements_Architecture_v1.2.md)
- [GA4-first TODO](docs/todo.md)
- [管理者によるSet登録・承認移行](docs/admin-registration.md)
- [S1c／R1／A1の検証・リリース判定（2026-09-30）](docs/evaluation/s1c-r1-a1-release-2026-09-30.md)
- [GA4分析Skill設計調査](docs/ga4-analysis-skills.md)
- [実装計画](docs/implementation-plan.md)
- [Architecture](docs/architecture.md)
- [Security](docs/security.md)
- [Data Access](docs/data-access.md)
- [GA4 MCP](docs/ga4-mcp.md)
- [BigQuery MCP](docs/bigquery-mcp.md)
- [Semantic Layer](docs/semantic-layer.md)
- [Analysis State](docs/analysis-state.md)
- [Analysis Streaming API](docs/api-streaming.md)
- [企業オンボーディング](docs/onboarding-company.md)
- [ローカル開発](docs/local-development.md)
- [デプロイ](docs/deployment.md)
