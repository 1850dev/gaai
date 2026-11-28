# GA4 × BigQuery Analytics AI — 実装マスタープロンプト

> **GA4認証訂正:** 本文中の「GA4標準＝Data Access Service Account」は2026-09-01に失効した。GA4認証は[機能要件 v1.2](GA4_BigQuery_Analytics_AI_Functional_Requirements_Architecture_v1.2.md)と[ADR 0009](adr/0009-ga4-user-oauth-and-bigquery-service-account.md)を必ず優先する。BigQueryのService Account要件は継続する。

あなたは、シニアレベルのStaff Software Engineer / Cloud Architect / AI Engineer / Product Engineerとして、このプロジェクトを設計・実装してください。

単にデモを作るのではなく、**実際に複数の事業会社で安全に利用できるProduction-readyなAnalytics AI Web Application**を構築することが目的です。

---

# 0. 最重要指示

このRepositoryに以下の仕様書が存在する場合、最初に全文を読み、**Source of Truth**として扱ってください。

`GA4_BigQuery_Analytics_AI_Functional_Requirements_Architecture_v1.1.md`

仕様書とこのプロンプトに差異がある場合は、以下の優先順位で判断してください。

1. このプロンプトの「絶対条件」
2. 最新の機能要件・システム構成書
3. Repository内のArchitecture Decision Record
4. 既存コード

不明点があっても、実装可能な範囲まで作業を止めないでください。

小さな不明点について質問を繰り返すのではなく、合理的な仮定を置き、その仮定をドキュメントへ記録してください。

ただし、以下に関わる変更を勝手に行ってはいけません。

- Security boundary
- Authentication方式
- Data Source Setによるデータ分離
- Service Accountによるデータアクセス
- read-only原則
- GA4 MCP / BigQuery MCP構成
- Company間のCredential分離

---

# 1. プロダクトの目的

ユーザーがGA4およびBigQueryのデータについて自然言語で質問し、会話を続けながら分析粒度を深められるWebアプリケーションを構築してください。

典型的なUXは以下です。

ユーザー:

「今月のCVはどう？」

AI:

- 前月比
- 主要KPI
- チャネル別寄与
- 必要に応じてグラフ

ユーザー:

「Organicを深掘りして」

AI:

- Analysis Stateを引き継ぐ
- Organic Searchに絞る
- LP / Device / Eventなどを追加分析する

ユーザー:

「モバイルだけ見て」

AI:

- Propertyや期間を再確認せず、現在のAnalysis Stateを引き継ぐ
- Deviceをmobileに限定して追加分析する

ユーザー:

「今の分析をグラフにして」

AI:

- GA4 / BigQueryを再取得する必要がなければCacheを利用する
- 適切なVisualization Specificationを生成する
- FrontendでChartを描画する

---

# 2. 優先技術スタック

可能な限り以下を優先してください。

## Frontend

- SvelteKit
- TypeScript
- Node.js
- Firebase Web SDK
- Apache ECharts

## Backend

- Python
- FastAPI
- Pydantic
- Google Cloud Python SDK
- MCP Client

## Google Cloud

- Google Cloud
- Cloud Run
- Firebase Authentication
- Cloud Firestore
- Cloud Storage
- Secret Manager
- IAM
- Service Account Impersonation
- Cloud Logging
- Cloud Audit Logs
- Vertex AI Gemini
- BigQuery

## Analytics

- Google公式 Google Analytics MCP
- Google公式 BigQuery Remote MCP

## Infrastructure

- Terraformを優先
- Docker
- Artifact Registry
- Cloud Build または GitHub Actions

他の技術を採用する場合は、明確な理由をArchitecture Decisionとして残してください。

---

# 3. システムの最重要アーキテクチャ

以下の構造を崩さないでください。

```text
End User
   ↓
Firebase Authentication
   ↓
SvelteKit Web Application
   ↓
Python FastAPI Backend
   ↓
Authorization Guard
   ↓
Analysis Orchestrator
   ├─ Semantic Layer
   ├─ Analysis State
   ├─ Query Router
   ├─ Dataset Cache
   ├─ Evidence
   ├─ Visualization Planner
   └─ MCP Gateway
          ├─ Google Analytics MCP
          └─ BigQuery Remote MCP
```

---

# 4. ユーザー認証とデータアクセスを分離する

これは絶対条件です。

## 人の認証

エンドユーザー:

```text
Google Login
→ Firebase Authentication
→ Firebase UID
```

Firebase UIDをアプリケーション上のUser Identityとして使用してください。

## データアクセス

GA4 / BigQueryへのアクセスには、エンドユーザー本人のGoogle Cloud権限を使用しません。

代わりに、

```text
Company / Data Access Profile
→ Data Access Service Account
```

を使用してください。

つまり、

```text
End User Identity
≠
GA4 / BigQuery Data Access Identity
```

です。

Service Account Key JSONをエンドユーザーへ配布してはいけません。

可能な限りService Account Key自体を作成しないでください。

Cloud Run BackendのRuntime Service AccountからService Account Impersonationを利用して短期Credentialを取得してください。

---

# 5. Google Cloud Projectの責任範囲

本サービス運営側で以下のGoogle Cloud Projectを管理する想定です。

```text
analytics-ai-dev
analytics-ai-stg
analytics-ai-prod
```

Production Project側で以下を有効化します。

- Google Analytics Admin API
- Google Analytics Data API
- BigQuery API
- IAM Service Account Credentials API
- Firebase Authentication
- Firestore
- Cloud Run
- Secret Manager
- Cloud Logging
- BigQuery MCP利用に必要な設定

BigQuery Query Jobの実行Projectは原則、

`analytics-ai-prod`

に固定してください。

---

# 6. 利用企業側で設定するもの

当社側で会社ごとのData Access Service Accountを作成します。

例:

`company-a-analytics@analytics-ai-prod.iam.gserviceaccount.com`

利用企業にはこのメールアドレスを渡し、以下を設定してもらいます。

## GA4

対象PropertyにService Accountを追加。

権限:

`Viewer`

## BigQuery

対象DatasetまたはViewにService Accountを追加。

権限:

`roles/bigquery.dataViewer`

Project全体へのData Viewer付与を標準にしてはいけません。

必要なDataset / Viewだけを許可してください。

---

# 7. Data Source Set

**Companyをデータアクセス境界にしてはいけません。**

Data Source SetをデータアクセスとAnalysis Sessionの基本単位にしてください。

例:

```text
A社

├─ A社 Web
│   ├─ GA4 Property: 222222222
│   └─ BigQuery Dataset: analytics_222222222
│
└─ A社 店舗
    ├─ GA4 Property: 111111111
    └─ BigQuery Dataset: analytics_111111111
```

User A:

```text
A社 Web = Allow
A社 店舗 = Deny
```

User B:

```text
A社 Web = Deny
A社 店舗 = Allow
```

同じCompany所属であることを理由に、Company内のすべてのデータを閲覧可能にしてはいけません。

最終的なアクセス許可は、

```text
Service Accountが対象データへアクセス可能
AND
Data Source Setに対象リソースが登録されている
AND
User Access Grantが存在する
```

のすべてを満たした場合のみ許可してください。

---

# 8. Analysis Session

Chat SessionとAnalysis Stateを分離してください。

Chat historyだけをAnalysis Contextとして使用してはいけません。

Analysis Stateには最低限以下を保持してください。

```json
{
  "data_source_set_id": "...",
  "period": {},
  "comparison_period": {},
  "focus": {
    "metric": null,
    "dimension": null,
    "channel": null,
    "device": null
  },
  "dataset_ids": [],
  "findings": []
}
```

以下の自然な会話を成立させてください。

- 「Organicを深掘り」
- 「その中でモバイルだけ」
- 「前月と比較して」
- 「さっきのLPだけ」
- 「今の結果をグラフにして」

---

# 9. Semantic Layer

Semantic Layerは本プロダクトの中核機能です。

日本語のAnalytics用語を直接LLM任せに解釈してはいけません。

以下の階層を持たせてください。

```text
Global
↓
Company
↓
Data Source Set
```

下位定義が上位定義をoverride可能としてください。

例:

```text
UU
ユーザー
ユーザー数
訪問ユーザー

→ activeUsers
```

会社固有のCV定義にも対応してください。

例:

```text
A社 Web
CV = generate_lead

A社 EC
CV = purchase
```

Semantic Layerでは最低限以下を管理してください。

- KPI
- Metric
- Dimension
- Alias
- Event
- Key Event
- Conversion Definition
- Revenue Definition
- Channel Definition
- Business Terminology
- Comparison Rule
- BigQuery logical field
- SQL macro
- Unit
- Visualization metadata

---

# 10. GA4 MCP

Google公式Repositoryを前提とします。

`https://github.com/googleanalytics/google-analytics-mcp`

主に以下を使用します。

- get_account_summaries
- get_property_details
- get_custom_dimensions_and_metrics
- run_report
- run_realtime_report
- run_funnel_report
- その他read-only Tool

GA4 MCPはread-onlyとしてください。

## 重要

Google公式GA4 MCPはローカル利用を前提とし、ADC Credentialを使用します。

マルチCompany Web Applicationで、異なるCompany Credentialを同じglobal credential cacheで共有してはいけません。

必要であれば、

`GA4 MCP Adapter`

を実装し、

- Data Access Profile単位でCredentialを注入
- Credential cacheをProfile単位に分離
- 公式Tool interfaceを可能な限り維持
- 公式Repositoryの実装を可能な限り再利用

してください。

Company AのCredentialでCompany BのPropertyへアクセス可能になる設計は重大なSecurity Bugとして扱ってください。

---

# 11. BigQuery MCP

Google公式 BigQuery Remote MCPを使用してください。

Endpoint:

`https://bigquery.googleapis.com/mcp`

主に使用するTool:

- list_dataset_ids
- get_dataset_info
- list_table_ids
- get_table_info
- execute_sql_readonly

通常のInteractive Analysisでは `execute_sql_readonly` をデフォルトとしてください。

BigQuery MCPの現行制約として、Interactive queryについて、

- 最大約3分
- 結果最大3,000行

を前提に設計してください。

大量データをLLMへ返してはいけません。

以下の処理をBigQuery内部で行ってください。

```text
filter
aggregate
join
classification
AI processing
ranking
```

そして小さな結果だけをMCPから返してください。

---

# 12. BigQuery Query Guard

LLMが生成したSQLをそのまま実行してはいけません。

実行前に必ず検証してください。

最低限:

- read-only
- Allowlist外Project禁止
- Allowlist外Dataset禁止
- Allowlist外View/Table禁止
- `SELECT *` 原則禁止
- GA4 events_*では日付範囲を要求
- 不要なCROSS JOIN禁止
- Query ProjectをPlatform Projectへ固定
- 結果件数を必要最小化
- Query Cost / Bytes Processedを記録
- BigQuery MCP dry runを可能な限り利用

MCP Tool Schemaだけで絶対的なコスト上限を保証できない場合は、MCP Gatewayの前段にSafety Adapterを設けてください。

---

# 13. BigQuery AI

将来的にBigQuery内の生成AI機能を利用できる設計にしてください。

用途:

- 問い合わせ分類
- Review分類
- Sentiment
- Text Summarization
- Structured Extraction
- 非構造化データ分析

原則:

```text
大量テキスト
↓
BigQuery内部でAI処理
↓
BigQuery内部で集計
↓
小さな結果
↓
MCP
↓
外側のAnalysis AI
```

大量のraw textを外側LLMへ送ってはいけません。

BigQuery AIはCompany / Data Source SetごとにOpt-in可能としてください。

---

# 14. Query Router

質問内容によってGA4 MCPまたはBigQuery MCPを自動選択してください。

## GA4優先

- Standard KPI
- Users
- Sessions
- Channel
- Landing Page
- Event Aggregate
- Realtime
- Funnel
- Property Metadata

## BigQuery優先

- Raw Event Analysis
- User Journey
- Complex JOIN
- Long-term Detailed Analysis
- GA4 Export
- CRM / Salesとの結合
- Text Analysis
- BigQuery AI
- Advanced SQL

ユーザーに「GA4かBigQueryか」を意識させる必要はありません。

---

# 15. Dataset Cache

取得済みの分析Datasetを再利用してください。

Cache Keyは自然言語ではなく、正規化されたQueryから生成してください。

例:

```json
{
  "data_source_set": "company_a_web",
  "source": "ga4",
  "date_range": [],
  "dimensions": [],
  "metrics": [],
  "filters": {}
}
```

Canonical JSON → Hashとしてください。

Metadata:

Firestore

Result Blob:

Cloud Storage

を基本としてください。

大きなDatasetをFirestoreへ直接保存しないでください。

---

# 16. Evidence

AIの分析結果にはEvidenceを紐付けてください。

例:

```text
Organic SearchのCVが前月比28%減少
Evidence: dataset_002
```

数値計算をLLMに任せてはいけません。

以下はコード側で計算してください。

- MoM
- YoY
- CVR
- Difference
- Ratio
- Ranking
- Contribution

---

# 17. Visualization

LLMにChart描画コードを書かせないでください。

LLMまたはVisualization PlannerはVisualization Specificationのみ生成します。

例:

```json
{
  "type": "line",
  "title": "CV推移",
  "x": "date",
  "series": [
    {
      "field": "keyEvents",
      "label": "CV"
    }
  ]
}
```

Frontend側でEChartsを使って描画してください。

原則:

- Single KPI → KPI Card
- Date × Metric → Line
- Category × Metric → Bar
- Composition → Pie / Donut
- Detail → Table

LLM判断よりルールベースを優先してください。

---

# 18. UI / UX

SvelteKitで以下を実装してください。

## Main

- Google Login
- Data Source Set Selector
- Chat UI
- Streaming response
- KPI Cards
- Tables
- Charts
- Evidence
- Session history
- Analysis State summary

## Admin

- Company
- User
- Data Access Profile
- Data Source Set
- User Access Grant
- Semantic Layer
- Audit Log

デザインは、

- clean
- professional
- analytics-focused
- information dense but readable

を目指してください。

過剰な装飾より、分析結果の読みやすさを優先してください。

---

# 19. Firestore

最低限以下の論理構造を実装してください。

```text
users
companies
companyMembers
dataAccessProfiles
dataSourceSets
userAccessGrants
semanticDefinitions
analysisSessions
messages
findings
datasetCache
auditEvents
```

Security RulesもRepository管理してください。

FrontendからSecurity-sensitiveなcollectionを自由に書き換えられないようにしてください。

---

# 20. MCP Gateway

LLMからMCP Serverへ直接アクセスさせてはいけません。

必ず以下を通してください。

```text
LLM
↓
MCP Gateway
↓
Authorization
↓
Scope Injection
↓
Credential Selection
↓
Tool Policy
↓
Query Guard
↓
MCP
```

MCP Gatewayでは最低限以下を検証します。

- user_id
- session_id
- data_source_set_id
- data_access_profile_id
- property_id
- project_id
- dataset_id
- table/view
- Tool allowlist
- read-only
- audit context

---

# 21. Observability

以下を記録してください。

- user_id
- company_id
- data_source_set_id
- session_id
- data_access_profile
- LLM model
- MCP server
- MCP tool
- GA4 property
- BigQuery project
- BigQuery dataset
- query hash
- job id
- bytes processed
- cache hit/miss
- latency
- error
- Evidence IDs

Cloud Logging / Audit LogsとアプリAuditを関連付けられるようにしてください。

---

# 22. Security

以下はProduction Blockerです。

- Service Account Keyの配布
- CredentialのFrontend保存
- UserにToken Creator権限を付与
- Company所属だけで全Data Sourceへアクセス
- LLMによるProperty自由選択
- LLMによるDataset自由選択
- MCP Toolの無制限公開
- Company間Credential共有
- raw大量データのLLM送信
- read/write BigQueryを無制限に許可
- 数値計算のLLM依存
- Auditなし

---

# 23. Repository構成

原則以下を推奨します。

```text
analytics-ai/
├─ apps/
│   └─ web/
│       └─ SvelteKit
│
├─ services/
│   └─ analysis-api/
│       ├─ api/
│       ├─ auth/
│       ├─ authorization/
│       ├─ orchestrator/
│       ├─ semantic/
│       ├─ analysis_state/
│       ├─ query_router/
│       ├─ mcp_gateway/
│       ├─ ga4/
│       ├─ bigquery/
│       ├─ cache/
│       ├─ evidence/
│       └─ visualization/
│
├─ packages/
│   ├─ contracts/
│   └─ shared-schema/
│
├─ infra/
│   └─ terraform/
│
├─ tests/
└─ docs/
```

---

# 24. 開発の進め方

一気に巨大な実装を行わず、Vertical Sliceで進めてください。

## Phase 0 — Foundation

まず以下を作成してください。

- Architecture review
- ADR
- Repository structure
- Docker
- Local development
- Terraform skeleton
- Firebase project config
- Cloud Run config
- CI

## Phase 1 — Identity / Authorization

- Firebase Google Login
- User
- Company
- Data Source Set
- User Access Grant
- Authorization Guard

ここを完成させる前にAnalytics Toolを公開しないでください。

## Phase 2 — GA4

- GA4 MCP Adapter
- Credential isolation
- Property scope enforcement
- Basic run_report
- Cache
- Evidence

## Phase 3 — Chat / Analysis

- Chat UI
- Streaming
- Semantic Layer
- Analysis State
- Query planning
- Conversation continuation

## Phase 4 — Visualization

- KPI
- Table
- Line
- Bar
- Visualization Spec

## Phase 5 — BigQuery

- BigQuery Remote MCP
- Dataset scope enforcement
- SQL generation
- Query Guard
- dry run
- BigQuery result cache

## Phase 6 — Advanced

- BigQuery AI
- Text classification
- Advanced analysis
- Admin UI
- Audit UI

各Phase終了時にAcceptance Testを実行してください。

---

# 25. 最低限のAcceptance Tests

必ずAutomated TestまたはE2E Testで確認してください。

## Security

User A:

```text
A社 Web = Allow
A社 店舗 = Deny
```

の場合、

- UIに店舗を表示しない
- Backend APIで店舗指定を拒否
- LLMが店舗を指定しても拒否
- GA4 MCPへ店舗Propertyを送らない
- BigQuery MCPへ店舗Datasetを送らない

## Conversation

以下が成立すること。

```text
「今月のCV」
↓
「Organicだけ」
↓
「モバイルだけ」
↓
「前月比較」
↓
「グラフにして」
```

## Cache

取得済みDatasetを再利用し、同一Queryを不必要に再実行しない。

## Semantic

「UU」をProperty / Company定義に従い正しいMetricへ解決する。

## Evidence

すべての主要数値Findingから元Datasetを追跡できる。

---

# 26. Coding Quality

必須:

- strict typing
- lint
- formatting
- unit test
- integration test
- E2E test
- structured logging
- error handling
- retry policy
- timeout
- dependency pinning
- no hardcoded credential
- no secret commit
- README
- Architecture docs

Python:

- type hints
- Pydantic
- pytest
- ruff

TypeScript:

- strict mode
- ESLint
- Prettier
- Vitest
- Playwright

---

# 27. ドキュメント

実装と同時に以下を更新してください。

```text
README.md
docs/architecture.md
docs/security.md
docs/data-access.md
docs/ga4-mcp.md
docs/bigquery-mcp.md
docs/semantic-layer.md
docs/analysis-state.md
docs/onboarding-company.md
docs/local-development.md
docs/deployment.md
```

特に `onboarding-company.md` には、

1. Service Accountの受け渡し
2. GA4 Viewer付与
3. BigQuery Dataset Data Viewer付与
4. Property ID
5. Project ID
6. Dataset ID

の設定手順を明記してください。

---

# 28. 進行時のルール

コードを書く前に、まずRepositoryと仕様を読み、

1. Current State
2. Target Architecture
3. Implementation Plan
4. Security considerations
5. Unknowns / assumptions

を簡潔に整理してください。

その後、作業を開始してください。

途中で発見した重要なArchitecture Issueはすぐに報告してください。

ただし、小さな判断のたびに実装を止めないでください。

---

# 29. 完成条件

「画面が動く」だけでは完成ではありません。

最低限、

- Google Login
- User authorization
- Data Source Set isolation
- Company Service Account
- GA4 MCP
- BigQuery MCP
- Semantic Layer
- Analysis State
- Dataset Cache
- Evidence
- Query Router
- Query Guard
- Chart rendering
- Session continuation
- Audit
- Infrastructure as Code
- Automated tests

が一貫して動作して初めてMVP完成とします。

---

# 30. 最終目標

これは単なる「GA4 Chatbot」ではありません。

最終的に構築するものは、

**Conversational Analytics Platform**

です。

ユーザーには、

```text
どのAPIを使うか
どのSQLを書くか
GA4かBigQueryか
どのDimension名か
どのMetric名か
```

を意識させないでください。

ユーザーが考えるべきなのは、

> 「事業について何を知りたいか」

だけです。

システム側で、

```text
Natural Language
↓
Semantic Understanding
↓
Analysis State
↓
Query Planning
↓
GA4 / BigQuery Routing
↓
Data Retrieval
↓
Analysis
↓
Evidence
↓
Visualization
↓
Next Question
```

を成立させてください。

最優先するものは、

**Security / Correctness / Explainability / Maintainability / User Experience**

です。

派手なAIデモより、正しく、安全に、繰り返し使える分析システムを作ってください。
