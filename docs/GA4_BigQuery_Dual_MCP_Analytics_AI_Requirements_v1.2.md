**GA4 × BigQuery  
Dual-MCP Conversational Analytics AI**

Webアプリケーション 要件定義書

Version 1.2 / GA4-first Local Quality & Agent Baseline  
2026年9月16日

> **設計コンセプト：会話をAnalysis Stateとして継続し、GA4 MCPで高速な集計分析、BigQuery MCPでイベントレベル・高度分析・BigQuery AIを実行。結果をEvidence付きの文章・KPI・表・グラフとして返す。**

## 本書の想定読者

本書は、社内向けAnalytics AIの企画・設計・実装・運用に関わる以下の関係者が、共通の前提で意思決定するための要件定義書である。

- プロダクトオーナー / 事業責任者
- Webアプリ / AI / データエンジニア
- Google Cloud / IAM / セキュリティ管理者
- GA4 / BigQuery / データ基盤の管理者

本書は製品要件の現行版である。認証・認可の詳細は[機能要件・システム構成書 v1.2](GA4_BigQuery_Analytics_AI_Functional_Requirements_Architecture_v1.2.md)と[ADR 0009](adr/0009-ga4-user-oauth-and-bigquery-service-account.md)に従う。実装済みであることと、受入検証済みであることを区別する。

## 文書の前提

外部APIの参考情報は旧版の2026年9月1日時点の記録を継承する。本改訂ではRepositoryとローカル動作を監査したが、外部API仕様の最新版を再確認したものではない。Google公式のGoogle Analytics MCP Server実装、およびGoogle Cloud公式BigQuery Remote MCPを前提とする。MCP Tool schemaは更新され得るため、実装は起動時/デプロイ時の`tools/list`検証とCapability Registryで差分を吸収する。

## 想定利用形態

| **項目** | **前提** |
|---|---|
| 利用範囲 | 自社グループ／事業会社向けの内部Webアプリ |
| 人の認証 | Google Login → Firebase Authentication → Firebase UID。Membership／Role／GrantをBackendで検証 |
| GA4認証（標準） | **ログインユーザー本人のGoogle OAuth Credential**。アプリが`analytics.readonly` scopeを要求し、ユーザーが許可する |
| GA4認証（任意） | Data Source SetごとにService Account方式も選択可能。自動fallbackは禁止 |
| GA4閲覧範囲 | USER_OAUTH時は「ユーザー本人がGA4で閲覧可能なProperty」かつ「アプリで許可されたData Source Set」の積集合 |
| BigQuery認証 | Data Access ProfileごとのService Account。標準は1社1SA、必要に応じ店舗/Web/部門単位で分離 |
| 分析対象 | Data Source Set（例：A社 店舗、A社 Web）をセッション開始時に明示選択 |
| GA4 | Google公式 Google Analytics MCP Serverを基準とする互換Adapter（Admin API / Data API、read-only） |
| BigQuery | Google Cloud公式 BigQuery Remote MCP（https://bigquery.googleapis.com/mcp） |
| LLM | 自然言語理解・計画・SQL/Query候補生成・解釈・説明。権限・計算・安全性・描画はコード側で強制 |
| 高度AI | BigQuery AI（AI.GENERATE等）はPolicy許可時のみ利用。非構造化データの要約・分類・抽出に使用 |

> **最重要方針：LLMに「どの会社・プロパティ・Datasetを見てよいか」を決めさせない。Data Source Set / Access Profile / AllowlistをBackendが強制し、MCPには許可済みContextだけを渡す。**

# 0. v1.2改訂範囲・現在の到達点

旧ファイル `GA4_BigQuery_Dual_MCP_Analytics_AI_Requirements_v1.0.md` の本文はVersion 1.1だった。本書をその後継Version 1.2とし、旧ファイルは履歴として残す。既存要件IDは維持する。

- 直近の優先事項は**ローカル分析品質の検証・改善**。公開／本番移行は今回行わない。
- GA4は本人User OAuthが標準。BigQueryはProfile SAであり、実接続・高度分析は保留。
- 正式MVPは両データ経路を必要とする。現段階はローカル検証、その後の限定提供はGA4 Pilotと呼ぶ。
- 認可はGoogle側の権限 AND 管理登録済みProperty／Stream AND User Access Grant。Company所属やDiscoveryだけで許可しない。
- 既存実装にはDiscoveryからDSS／Grantを自動作成するP0差異が残る。要件を実装に合わせて弱めない。
- クラウド資源の追加作成・既存履歴の一括移行・認証方式の変更は本改訂に含めない。

### 状態の読み方

| 状態 | 意味 |
|---|---|
| 実装済み | コードと指定した回帰テストがある。実データ受入完了とは別 |
| 部分実装 | 対応する条件が限定され、未実装経路が残る |
| 未実装 | 今後実装が必要 |
| 実環境未検証 | 合成テストのみ、または必要な実Identity／数値突合が未完 |
| 保留 | 意図的に今の対象から除外。完了ではない |

### 現状とギャップ（2026-09-16）

実装状況の追補（2026-09-17）: 下表の引用元復元漏れはQ2aで修正・検証した。要件自体は変更せず、最新進捗は[TODO](todo.md)と[Q2a検証記録](evaluation/q2a-citation-recovery-2026-09-17.md)で管理する。

| 項目 | 到達点 | 未完了 |
|---|---|---|
| 会話・Semantic・GA4取得・Evidence | 実装済み／条件付き | 実GA4の体系的な数値突合、全組合せ対応 |
| 上位対象→各対象の内訳 | 部分実装 | 最大10対象・単一指標・2軸・単一期間。任意依存計画は未実装 |
| 記憶・履歴検索 | 部分実装 | 500候補／通常8件。引用済み元分析が検索外になる復元漏れを実履歴で確認 |
| Markdown・対象別グラフ | 実装済み | 認可済みCacheが欠損／期限切れのときの完全復元は保証しない |
| 定期レポート | ローカル実装済み | クラウド実適用、通知、失効の実Acceptance |
| Admin／正確なScope登録 | 部分実装・P0 | 管理変更画面、auto Grant解消、Stream・timezone・currency |
| 再送・再起動 | 部分実装 | 汎用Operation ledger、切断後の安全な自動再開 |
| BigQuery | Guard／Transportコードあり・保留 | 実接続受入、BQ AI、正式MVP判定 |

要件の優先順は最新のユーザー明示判断（認証・境界変更は明示承認が必要）→機能要件・システム構成書v1.2の安全契約→本書→ADR→実装。旧v1.1で変更されていない機能要件は継承する。外部仕様の未再確認事項は第30章の履歴として扱い、再開・更新時に公式仕様を確認する。

# 1. エグゼクティブサマリー

本システムは、事業会社ごと・分析対象ごとに分離されたGA4およびBigQueryデータを、日本語の自然言語会話から安全に分析する社内向けWebアプリケーションである。ユーザーは「A社 Web」「A社 店舗」のようなData Source Setを選択し、そのContextを維持したまま、全体KPIからチャネル、LP、デバイス、イベント系列、非構造化データの内容分析へ段階的に深掘りできる。

## 1.1 成功条件

- 非エンジニアがGA4のDimension/Metric名やBigQuery SQLを意識せず、日本語で分析できる。

- 同一セッションで「その中でOrganicだけ」「前月比較」「今の結果をグラフに」が成立し、不要な再取得をしない。

- 「ユーザー」「CV」「売上」等の曖昧語をSemantic LayerでData Source Set単位に正規化できる。

- 軽量・標準集計はGA4 MCP、イベントレベル・複雑SQL・BigQuery AIはBigQuery MCPへ自動ルーティングする。

- 店舗/Webなど複数Propertyが同一会社に存在しても、ユーザーに許可されたData Source Set以外のデータを混ぜない。

- BigQuery MCPの3,000行/3分制約を前提に、BigQuery内で集計・要約し、MCPを大量データ搬送用途に使わない。

- 各主要数値・結論から、Tool call / GA4 Query / SQL / Dataset / Semantic Versionへ追跡できる。

## 1.2 コアアーキテクチャ

```text
[Browser / Web App]
│ Google Login / Firebase or OIDC
│
├─ App Identity
│
└─ GA4 OAuth Consent
   └→ analytics.readonly
        │
        ▼
[Session & Data Source Set Context]
        │
        ▼
[Analysis Orchestrator]
├─ Semantic Resolver
├─ Analysis State / Evidence
├─ Dataset Cache
├─ Planner / Router
└─ MCP Gateway / Policy Enforcement
   ├─ GA4 Credential Broker
   │  ├─ USER_OAUTH（標準）
   │  ├─ SERVICE_ACCOUNT（任意）
   │  └─ SERVICE_ACCOUNT_AND_USER_ACCESS（将来候補・未採用）
   │       ↓
   │   GA4 MCP-compatible Adapter
   │       └→ GA4 Admin API / Data API
   │
   └─ BigQuery Data Access Profile SA
       ↓
       BigQuery Remote MCP
       ├→ Metadata / execute_sql_readonly
       └→ BigQuery SQL / ML / AI functions

[Deterministic Analysis + Visualization Renderer]
└→ Text / KPI / Table / Chart
```

> **MCPは「データに接続するTool層」であり、分析の安全性・Tenant境界・Semantic定義・状態管理はアプリ側の責務とする。**

# 2. 目的・設計原則・スコープ

## 2.1 目的

- GA4の専門知識がなくても事業KPIに沿った分析を会話形式で行えるようにする。

- GA4 APIとBigQueryの得意領域を自動選択し、ユーザーにデータソース選択の複雑さを見せない。

- 既存のGoogle IAM/GA4アクセス権に加え、アプリ内Data Source Set権限で分析対象を二重に制御する。

- Semantic Layer / Analysis State / Dataset Cacheを長期的なプロダクト資産としてモデルから独立させる。

- 分析結果を単発回答ではなく、再利用可能なEvidence付きFindingとして蓄積する。

## 2.2 設計原則

| **原則**                     | **内容**                                                                                   |
|------------------------------|--------------------------------------------------------------------------------------------|
| Context first                | すべてのTool callは user → company → 登録済みdata_source_set＋Grant → 明示auth modeを確定する。GA4 USER_OAUTHは本人接続、BigQuery／GA4 SA modeは検証済みAccess Profileを使用する。 |
| Read-only by default         | GA4はread-only scope、BigQueryはexecute_sql_readonlyを標準とする。                         |
| Aggregate before LLM         | 生データをLLMへ大量投入せず、BigQuery/コード側で集計・分類・縮約する。                     |
| Evidence before assertion    | 主要数値・原因仮説・比較はEvidence IDがない限り断定しない。                                |
| Deterministic where possible | 認可、数値計算、SQL validation、Chart renderingは決定論的に実装する。                      |
| Progressive analysis         | 同じAnalysis Stateを保ちながら粒度を上げる。毎回ゼロから分析しない。                       |

## 2.3 MVP対象 / 非対象

| **区分**  | **内容**                                                                                                                                             |
|-----------|------------------------------------------------------------------------------------------------------------------------------------------------------|
| MVP対象   | GA4標準/Realtime/Funnel、BigQuery read-only SQL、GA4 Export深掘り、基本BigQuery AI（会社単位でOpt-in）、Semantic Layer、State、Cache、グラフ、監査。 |
| MVP非対象 | GA4設定Write、BigQuery DDL/DML、任意SQLコンソール、Cross-company分析、自律的な外部アクション、大量データExport。                                     |
| 将来候補  | Alert／外部通知、Search Console/Ads/CRM追加、Cross-source join、BigQuery Storage Read、Long-running Query worker。週次／月次レポートのローカル実行は6.1節の対象。                          |

# 3. 利用者・権限ロール・代表ユースケース

| **ロール**             | **主な利用目的**                        | **権限**                                  |
|------------------------|-----------------------------------------|-------------------------------------------|
| 一般分析ユーザー       | KPI確認、原因探索、可視化               | Membershipで許可されたData Source Setのみ |
| 事業会社管理者         | 利用者/Data Source Set/Semantic定義管理 | 自社範囲の設定・接続テスト                |
| データ/分析担当        | Semantic定義、SQL Template、評価        | Query/Semantic/Evaluation資産を管理       |
| プラットフォーム管理者 | 会社・Access Profile・監査・コスト管理  | 全体管理。通常データ内容は必要最小限      |

## 3.1 代表ユースケース

- 「A社 Web」を選び「今月のCVどう？」→ GA4 MCPでKPI・前月比較・推移を返す。

- 「原因を見て」→ チャネル別に分解し、Organic低下を発見。Analysis StateへFindingを追加。

- 「OrganicをLP別に」→ 前の期間/Metric/Filterを継承し、CacheまたはGA4 MCPを利用。

- 「購入前7日間に見ていたページ」→ BigQuery MCPへルーティングし、GA4 Exportイベント列を分析。

- 「問い合わせ本文をテーマ別に要約して」→ BigQuery AIを許可済みの場合、BigQuery内で分類/要約して少数行に集約。

- 「グラフにして」→ 取得済みDatasetだけでVisualization Specを作り、外部APIを再実行しない。

# 4. 認証・認可・Data Access Profile

> **GA4とBigQueryでデータアクセスIdentityを分ける。GA4の標準はログインユーザー本人のOAuth Credential、BigQueryの標準はData Access Profileに紐づくService Accountとする。**

## 4.1 人のログインとGA4 OAuthは役割を分ける

アプリへのログインはGoogle OAuth / OIDC（Firebase Authenticationを利用する場合はFirebase Google Sign-In）で行う。

ただし、**アプリへのログインだけでGA4 APIの利用許可を得たものとして扱ってはならない。**

GA4をユーザー本人の権限で読む場合、アプリは別途Google OAuthで次のscopeを要求し、ユーザー本人から許可を得る。

```text
https://www.googleapis.com/auth/analytics.readonly
```

役割は以下のとおり。

```text
Google Login / Firebase Auth
→ 「このアプリ利用者が誰か」を確認

GA4 OAuth + analytics.readonly
→ 「このユーザー本人のGA4閲覧権限を使ってGA4 APIを読む」許可
```

ユーザーへGA4のViewer権限を本アプリが新たに付与するわけではない。  
USER_OAUTHモードでは、ユーザーがGoogle Analytics側ですでに閲覧可能なPropertyだけがAPIから利用可能となる。

## 4.2 GA4 Auth Mode

Data Source SetごとにGA4認証方式を明示する。

```text
USER_OAUTH                      ← 標準
SERVICE_ACCOUNT                 ← 任意
SERVICE_ACCOUNT_AND_USER_ACCESS ← 将来候補・現行実装では受け付けない
```

### USER_OAUTH（標準）

```text
User
↓ Google Login
Application User
↓ GA4 OAuth consent
analytics.readonly
↓
User OAuth Credential
↓
GA4 MCP-compatible Adapter
↓
GA4 Admin API / Data API
↓
ユーザー本人が閲覧可能なProperty
```

最終的なアクセス条件:

```text
ユーザー本人がGA4で対象Propertyを閲覧可能
AND
対象PropertyがData Source Setとして登録済み
AND
ユーザーがそのData Source SetへのAccess Grantを持つ
```

### SERVICE_ACCOUNT（任意）

利用企業の運用上、ユーザー個人のGA4権限ではなく管理されたService Accountで読む必要があるData Source Setにのみ利用する。

```text
Platform Runtime SA
↓ Service Account Impersonation
Data Access Profile SA
↓ analytics.readonly
GA4 Property
```

このモードでは、利用企業側で対象Service AccountをGA4 PropertyへViewer相当で追加する必要がある。

### SERVICE_ACCOUNT_AND_USER_ACCESS（将来候補・未採用）

現行のauth modeはUSER_OAUTHとSERVICE_ACCOUNTの2種類のみ。この候補を実装する場合は別途Architecture承認を必要とし、現在の受入対象に含めない。

より厳格なData Source Setでは以下の3条件をすべて要求できる。

```text
ユーザー本人が対象Propertyを閲覧可能
AND
Service Accountも対象Propertyを閲覧可能
AND
アプリのData Source Set Access Grantが存在
```

データ取得自体をService Accountで行う場合でも、事前にUser OAuth CredentialでPropertyアクセス可否を確認する。

**USER_OAUTHで権限不足になった場合にSERVICE_ACCOUNTへ自動fallbackしてはならない。**

## 4.3 GA4 OAuth Credential管理

WebアプリではGA4 APIアクセス用OAuth Credentialをユーザー単位で分離する。

最低限保持するメタデータ:

```text
ga4_user_connections
- user_id
- google_subject
- google_email
- granted_scopes
- token_ciphertext / token_reference
- status
- connected_at
- last_verified_at
```

要件:

- `analytics.readonly`のみをGA4分析の基本scopeとする。
- refresh token / access tokenをFrontendへ返さない。
- tokenをログへ出力しない。
- refresh tokenは平文保存しない。
- server-sideで暗号化またはSecret管理する。
- OAuthの`state`を検証する。
- Firebase/OIDCのGoogle IdentityとGA4 OAuth IdentityはMVPでは同一Google Accountを要求する。
- 異なるGoogle Accountが認可された場合は明示的に拒否または再認証する。

## 4.4 BigQuery Data Access Profile

Data Access Profileは、**主としてBigQueryについて「どのService Accountで、どのDataset / View / MCP Toolを利用できるか」を束ねる認可単位**である。

標準は1社1Profileだが、店舗/Web/部門などで機密境界が必要な場合はProfileを分ける。

```text
Company A
├─ Access Profile: A-common (Service Account A)
│  ├─ Data Source Set: A Web
│  └─ Data Source Set: A Store
│
└─ Access Profile: A-sensitive (Service Account A2)
   └─ Data Source Set: A Finance / restricted analytics
```

BigQueryでは利用企業側Dataset / ViewにProfile SAのread-only権限を付与し、Query JobはPlatform指定Projectで実行する。

## 4.5 認証・IAM要件

| **ID** | **要件** | **優先度** | **受入条件 / 補足** |
|---|---|---:|---|
| FR-AUTH-001 | Firebase Authenticationでユーザーを認証する。 | MUST | Firebase UIDをApplication Identityとする。Google subjectは別途OAuth接続の同一Account検証に使用し、認証成功だけではデータアクセスを許可しない。 |
| FR-AUTH-002 | ユーザーとCompanyのMembership/Roleを管理する。 | MUST | Company未所属・無効ユーザーは分析不可。 |
| FR-AUTH-003 | ユーザーごとに利用可能Data Source Setを制限できる。 | MUST | 同一会社でも店舗/Web/部門単位で許可範囲を分けられる。 |
| FR-GA4AUTH-001 | GA4の標準認証方式をUSER_OAUTHとする。 | MUST | `analytics.readonly`をユーザー本人にOAuth許可してもらう。 |
| FR-GA4AUTH-002 | USER_OAUTH時はユーザー本人のGA4 Property閲覧権限を利用する。 | MUST | Service AccountのProperty一覧で代替しない。 |
| FR-GA4AUTH-003 | GA4 OAuth Credentialをユーザー単位・request単位で分離する。 | MUST | User A/BのCredential混線を禁止。 |
| FR-GA4AUTH-004 | SERVICE_ACCOUNT方式をData Source Setごとに明示選択可能とする。 | SHOULD | 対象SAにGA4 Property Viewerが必要。 |
| FR-GA4AUTH-005 | USER_OAUTHからSERVICE_ACCOUNTへの自動fallbackを禁止する。 | MUST | 権限拡大を防止。 |
| FR-GA4AUTH-006 | USERアクセスとSAアクセスの積集合モードを将来評価する。 | FUTURE | 未採用・未実装。現行modeの暗黙切替を許可しない。 |
| FR-IAM-001 | BigQuery Data Access ProfileごとにService Accountを割り当てる。 | MUST | 1社複数SAを許容。別ProfileのCredentialを同一Tool callで混在させない。 |
| FR-IAM-002 | Runtime SAからProfile SAをService Account Impersonationする。 | MUST | 長期JSON Keyを本番保存・配布しない。 |
| FR-IAM-003 | BQ実行Projectで`roles/mcp.toolUser` + `roles/bigquery.jobUser`相当をProfile SAへ付与する。 | MUST | Remote MCP呼び出しとQuery Job作成に必要。 |
| FR-IAM-004 | 先方BigQuery Dataset/Viewにはread-only権限のみ付与する。 | MUST | 原則`roles/bigquery.dataViewer`。 |

# 5. Data Source Set（分析対象セット）

ユーザーが会話開始時に選ぶ単位はGA4 Property単体ではなくData Source Setとする。店舗PropertyとWeb Propertyが分かれている場合、それぞれ別Setとして登録し、対応するBigQuery Datasetを明示的に紐付ける。

```text
Data Source Set: A社 Web
ga4_property_id: 222222222
ga4_auth_mode: USER_OAUTH
bq_project_id: company-a-data
bq_dataset_id: analytics_222222222
allowed_tables: [events_*, optional views]
semantic_profile: a_web_v3
access_profile: a_common
Data Source Set: A社 店舗
ga4_property_id: 111111111
ga4_auth_mode: USER_OAUTH
bq_project_id: company-a-data
bq_dataset_id: analytics_111111111
semantic_profile: a_store_v2
access_profile: a_common
```

| **ID**     | **要件**                                                                | **優先度** | **受入条件 / 補足**                                           |
|------------|-------------------------------------------------------------------------|------------|---------------------------------------------------------------|
| FR-SRC-001 | Data Source SetにGA4 Propertyと対応BQ Dataset/Viewを登録できる。        | MUST       | どちらか片方のみのSetも許容。                                 |
| FR-SRC-002 | Data Source SetはCompanyに属し、SA方式で使用する場合はAccess Profileへ紐づく。                     | MUST       | 別Company/Profileへ付け替える場合は管理者操作と監査を必須化。 |
| FR-SRC-003 | ユーザーはAnalysis Session開始時にData Source Setを明示選択する。       | MUST       | LLMが自動で別Setに変更しない。                                |
| FR-SRC-004 | Discovery結果をそのまま分析対象にしない。Allowlist済みSetだけ利用する。 | MUST       | USER_OAUTHでユーザーが見える全Property、またはSAが見える全Property/Datasetを、そのままLLMに公開しない。               |
| FR-SRC-005 | GA4/BQ双方のtimezone/currency/identity差分をMetadataとして保持する。    | SHOULD     | 数値差の説明と期間境界に利用。                                |

## 5.1 オンボーディング

### 共通

- Companyを作成する。
- Data Source Setを作成する。
- Data Source Setごとに`ga4_auth_mode`を設定する。
- Semantic Profile（CV、売上、主要イベント、除外条件等）を登録・承認する。
- ユーザー/RoleへData Source SetをAllowlistする。

### GA4: USER_OAUTH（標準）

- Platform側Google Cloud ProjectでGoogle Analytics Admin API / Data APIを有効化する。
- OAuth Consent Screen / OAuth Clientを構成し、`analytics.readonly` scopeを要求可能にする。
- エンドユーザーがアプリへログイン後、「Google Analyticsを接続」から`analytics.readonly`を許可する。
- User OAuth CredentialでAdmin APIのAccount/Property discoveryを行う。
- ユーザー本人が閲覧可能で、かつアプリに登録・許可されたPropertyのみData Source Setとして利用可能にする。
- 利用企業側でService AccountをGA4へ追加する作業は、USER_OAUTHでは不要。

### GA4: SERVICE_ACCOUNT（任意）

- Data Access Profile / Service Accountを作成する。
- 先方へSAメールアドレスを提示し、対象GA4 PropertyへViewer相当のread-only権限を付与してもらう。
- GA4 MCP Adapterで対象Propertyの接続テストを行う。
- USER_OAUTHから自動fallbackしない。

### BigQuery（標準: Service Account）

- Data Access Profile / Service Accountを作成する。
- 先方へSAメールアドレスを提示し、対象BigQuery Dataset/Viewへread-only権限を付与してもらう。
- BigQuery MCPでDataset/Table metadataを接続テストし、dryRunの小規模SQLを実行する。
- Data Source SetにBigQuery Project / Dataset / View allowlistを登録する。

## 5.2 Property／Stream登録の安全条件

Discoveryは候補表示であり、DSS・Grant・Sessionの自動作成を許可するものではない。管理者が正確なProperty／Stream、timezone、currency、auth modeを検証して登録し、User Grantとともに有効化する。Web限定の対象では通常Report／Realtime／Funnelにも許可済みStream制約を強制する。Stream未登録をProperty全体の承認と解釈しない。

現行の `POST /api/ga4/sessions` による自動作成経路と既定timezone／currencyはこの要件とのP0差異としてS1で解消する。既存履歴は移行計画なしに一括書換え・削除しない。

# 6. 会話・Analysis State

Chat Transcriptはユーザーとの会話表示、Analysis Stateは機械可読な分析文脈であり、別Entityとして保存する。Analysis Stateは「現在何を見ているか」「何が判明したか」「どのEvidenceを使ったか」を保持する。

```text
analysis_session
company_id
data_source_set_id
access_profile_id（GA4 USER_OAUTHでは不要）
primary_period / comparison_period
canonical_metrics[] / dimensions[] / filters[]
focus / hypotheses[]
dataset_refs[]
findings[] -> evidence_ids[]
visualization_refs[]
semantic_version
router_history[]
```

| **ID**       | **要件**                                              | **優先度** | **受入条件 / 補足**                                |
|--------------|-------------------------------------------------------|------------|----------------------------------------------------|
| FR-CHAT-001  | 新規Analysis SessionをData Source Set付きで作成する。 | MUST       | Set未選択ではデータToolを実行しない。              |
| FR-CHAT-002  | 前発言の期間・指標・Filter・分析焦点を継承する。      | MUST       | 「その中で」「さっきの」を構造参照へ解決。         |
| FR-CHAT-003  | Data Source Set変更は明示UI操作でのみ行う。           | MUST       | 変更時はStateを分岐/新規化。                       |
| FR-STATE-001 | Analysis Stateをチャット本文とは別に保存する。        | MUST       | 再ログイン後も復元可能。                           |
| FR-STATE-002 | FindingはEvidenceを必須とする。                       | MUST       | 主要数値・結論→tool call/query/datasetへ追跡可能。 |
| FR-STATE-003 | 分析分岐を保持する。                                  | SHOULD     | 同じ元Datasetから別切り口へ戻れる。                |

## 6.1 会話エージェント・記憶・定期実行

GA4はAgentが選択するToolの一つとする。目的・達成条件→計画→実行→観測→再計画／説明を分離し、LLMに認可や計算を委ねない。

| ID | 要件 | 優先度／受入条件 |
|---|---|---|
| FR-AGENT-001 | 目的・制約・取得条件・完了条件を各ターンで再評価する | MUST。前ターンのFilterを継承し、明示変更と追加取得禁止を優先 |
| FR-AGENT-002 | 上位対象を確定してから各対象の内訳を調査する | MUST。全体の組合せtop-Nで代替しない。選定Evidenceと子分析Filterを結び付ける |
| FR-AGENT-003 | 予算・失敗・部分取得を説明する | MUST。アプリ回数／期限とGA4 quotaを区別。取得済み事実・未確認・次の選択を返す |
| FR-AGENT-004 | 通常の依頼内で段階計画を進める | MUST。Codexへの「続き」を製品の必須操作に転用しない。停止後の汎用自動再開はR1 |
| FR-MEMORY-001 | 目的、制約、対象、確認済み事実、仮説、未解決、残計画を分離保存 | MUST。会話本文だけを作業記憶にしない |
| FR-MEMORY-002 | 引用済み分析と関連分析の探索を分離する | MUST。直接引用は検索順位に依存させず、owner／DSS／Property・Stream／Semantic／Evidenceを再検証。曖昧・欠損なら未完了 |
| FR-MEMORY-003 | 長期合意はユーザー確認後に適用し、訂正・削除可能 | MUST。未承認候補を確定方針にしない。古い数値は当時の期間・保存時刻付き |
| FR-MEMORY-004 | 追加取得禁止では保存済み根拠のみを利用する | MUST。必要根拠が不足なら不足を明記。GA4を暗黙再取得しない |
| FR-SITE-001 | サイト基本情報、目標、技術的背景、施策の方向性をサイト理解の文脈として利用する。個人非表示設定も提供 | MUST。2026-09-26ユーザー訂正: 禁止・認可ポリシーではなく、分析や提案を設定だけで拒否しない。明示依頼と認可・計測Evidenceを優先。非表示は権限削除ではない。現状は個人×DSS、組織共有は別課題 |
| FR-SITE-002 | 改善仮説をサイト目的・評価指標の選択理由・計測限界・確認事項と結び付ける | MUST。代理指標を事業成果と同一視しない。適切な指標が未確定でも回答全体を止めず、未登録指標や未取得の経路・成果を作らない。評価方法を提案・回答履歴に残す。旧提案の互換性を維持。異なる事業目的の比較評価はQ3c-3 |
| FR-SCHEDULE-001 | 明示作成した週次／月次レポートをWorkerで実行 | MUST。毎回所有者とScopeを再認可、競合実行防止、失効時停止。ブラウザを閉じてもDocker稼働中は実行可能 |

現行の探索型は最大4分析／7判断、段階分析は選定1＋最大10対象、調査240秒／全体300秒、文章化55秒。これらはアプリの安全予算であってGA4 API上限や性能保証ではない。制限の拡大前に完了率・遅延・quota・失敗理由を計測する。

# 7. Semantic Layer

> **日本語の表記揺れだけでなく、「GA4の集計定義」と「BigQueryでの再現SQL」を同一Canonical Metricの2つのSource Mappingとして管理する。**

## 7.1 解決優先順位

- Data Source Set固有定義

- Company固有定義

- 全社共通辞書

- Analysis Stateの直近文脈

- Confidence不足時のみ短い確認質問

## 7.2 Semantic定義例

| **日本語** | **Canonical**  | **GA4 Mapping**                           | **BigQuery Mapping / 備考**                              |
|------------|----------------|-------------------------------------------|----------------------------------------------------------|
| ユーザー数 | active_users   | activeUsers                               | 事業定義に応じたdistinct user SQL。GA4との差分説明を保持 |
| CV         | conversion     | A Web: keyEvents or event filter          | event_name=generate_lead / purchase等。Set別Override     |
| 売上       | revenue        | purchaseRevenue                           | ecommerce.purchase_revenue等。税/返金/通貨定義を明示     |
| 自然検索   | organic_search | sessionDefaultChannelGroup=Organic Search | traffic source/session attribution SQL                   |
| LP         | landing_page   | landingPagePlusQueryString                | session_startからlanding page再構築SQL                   |

| **ID**     | **要件**                                                                         | **優先度** | **受入条件 / 補足**              |
|------------|----------------------------------------------------------------------------------|------------|----------------------------------|
| FR-SEM-001 | Canonical termはaliases, unit, aggregation, valid_grain, source_mappingsを持つ。 | MUST       | GA4/BQ mappingを同一termに保持。 |
| FR-SEM-002 | Global → Company → Data Source SetでOverrideする。                               | MUST       | 最も具体的な定義を優先。         |
| FR-SEM-003 | Semantic Versionを固定して回答に記録する。                                       | MUST       | 過去回答を再現可能。             |
| FR-SEM-004 | 率/金額/countの計算規則・分母・通貨を明示する。                                  | MUST       | LLMによる誤集計を防止。          |
| FR-SEM-005 | 日本語解決Confidenceを保持する。                                                 | SHOULD     | 重要語は高閾値。                 |
| FR-SEM-006 | Golden Setで日本語→Canonical→Source Mappingを回帰評価する。                      | MUST       | 略語・誤記・社内用語を含める。   |

# 8. Analysis Planner / Tool Router

Plannerは質問をNormalized Analysis Requestへ変換し、必要な分析Stepを生成する。RouterはCache / GA4 MCP / BigQuery MCP / BigQuery AIの中からPolicyに従って選択する。

| **条件**                                           | **優先ルート**                                  |
|----------------------------------------------------|-------------------------------------------------|
| 同一Normalized Queryの有効Cacheあり                | App Dataset Cache                               |
| KPI、日/月推移、チャネル、デバイス、LP等の標準集計 | GA4 MCP                                         |
| Realtime                                           | GA4 MCP realtime                                |
| Data APIで表現可能なFunnel                         | GA4 MCP funnel                                  |
| イベント順序、複雑JOIN、Raw event_params/items     | BigQuery MCP execute_sql_readonly               |
| 長期間・高カーディナリティだが結果は集計可能       | BigQuery MCP（必ずpre-aggregation）             |
| テキスト要約/分類/抽出、Forecast/ML                | BigQuery MCP + BigQuery AI/ML（Policy許可時）   |
| 3,000行超の結果搬送が必要                          | MCP外のBulk pathを別要件として扱う。MVPでは不可 |

| **ID**       | **要件**                                                    | **優先度** | **受入条件 / 補足**                                       |
|--------------|-------------------------------------------------------------|------------|-----------------------------------------------------------|
| FR-PLAN-001  | 質問をsource非依存のNormalized Analysis Requestへ変換する。 | MUST       | metric/dimension/filter/time/comparison/grain/intention。 |
| FR-PLAN-002  | 単一質問から複数StepのAnalysis Planを生成する。             | MUST       | 全体→チャネル→LP等。                                      |
| FR-ROUTE-001 | Cache/GA4/BQ/BQ-AIをPolicy + LLMで選ぶ。                    | MUST       | Policy Validatorが最終決定。                              |
| FR-ROUTE-002 | 既存Datasetのみで回答可能ならMCPを呼ばない。                | MUST       | 可視化変更など。                                          |
| FR-ROUTE-003 | 一質問の自動深掘りStep/Tool call上限を設定する。            | MUST       | コスト・Quota・暴走防止。                                 |

## 8.1 SkillとWorkflowの設計契約

- Skillはversion付きでTool、登録Metric／Dimension／Filter、互換性、行数、費用・実行予算、結果形状、Evidence、対応しない条件を宣言する。
- 個別Skillの実行と複数Skillに依存するWorkflowを分離する。成功判定には必要軸・指標・期間・各対象の根拠がそろうことを要求する。
- 現状はKPI／推移／順位／比較／深掘り／複合集計／Realtime／登録済みFunnel／異常候補の実装がある。全GA4分析の網羅を意味しない。
- 任意DAG、条件変更時の汎用差分再計画、driver decomposition、Admin承認済みcustom metadata拡張、version付きCVR計算契約は残課題。
- 情報サイトにCV定義がなければ購入等を勝手に代用しない。UUの月次値を日次UUの合計で代用しない。

# 9. MCP Gateway / Capability Registry

2つのMCPをLLMへ直接露出せず、BackendのMCP Gatewayを介して呼び出す。GatewayはCredential注入、Tool allowlist、引数上書き、監査、Timeout、Retry、Response縮約を統一的に行う。

| **ID**     | **要件**                                                                | **優先度** | **受入条件 / 補足**                               |
|------------|-------------------------------------------------------------------------|------------|---------------------------------------------------|
| FR-MCP-001 | MCP server / tool capabilitiesをRegistry化する。                        | MUST       | デプロイ時にtools/list等でTool schema差分を検証。 |
| FR-MCP-002 | Tool call前にSessionのData Source Set / Access Profileを再検証する。    | MUST       | LLM入力値を信用しない。                           |
| FR-MCP-003 | GA4/BQ Tool結果を共通Dataset形式へ正規化する。                          | MUST       | source, schema, rows, stats, lineageを共通化。    |
| FR-MCP-004 | Tool callへrequest_id / session / company等の監査メタデータを付与する。 | MUST       | BigQuery labelsにはPIIを入れない。                |
| FR-MCP-005 | Tool別にRead/Write classificationを保持し、MVPはread-onlyのみ許可する。 | MUST       | BigQuery execute_sqlは原則Deny。                  |

# 10. GA4 MCP統合

本書でいう「GA4 MCP」は、Google公式ドキュメントの[Try the Google Analytics MCP server](https://developers.google.com/analytics/devguides/MCP)および公式実装[`googleanalytics/google-analytics-mcp`](https://github.com/googleanalytics/google-analytics-mcp)を基準とする。

Google Analytics Admin API / Data APIはいずれもユーザーアカウントまたはService Accountで認証可能であり、本システムでは**USER_OAUTHを標準**、SERVICE_ACCOUNTを明示的な任意モードとして扱う。

read-only分析には以下のscopeを使用する。

```text
https://www.googleapis.com/auth/analytics.readonly
```

## 10.1 USER_OAUTH（標準）

ユーザー本人のOAuth CredentialをGA4 MCP-compatible Adapterへrequest-scopedで注入する。

```text
Logged-in User
↓
GA4 OAuth consent
↓ analytics.readonly
User OAuth Credential
↓
GA4 MCP-compatible Adapter
├─ Admin API: Account / Property discovery
└─ Data API: Report / Realtime / Funnel
```

Account/Property discoveryは必ずそのユーザー本人のCredentialで行う。  
Service Accountが見えるProperty一覧をユーザーの閲覧可能Property一覧として代用しない。

通常分析ではLLMがproperty_idを自由に決めず、Backendが選択済みData Source Setのproperty_idを注入・検証する。

## 10.2 SERVICE_ACCOUNT（任意）

Data Source Setの`ga4_auth_mode`が`SERVICE_ACCOUNT`の場合のみ、Data Access ProfileのService Accountを利用する。

```text
Platform Runtime SA
↓ Impersonation
Profile SA
↓ analytics.readonly
GA4 MCP-compatible Adapter
↓
対象GA4 Property
```

このモードでは利用企業側でProfile SAに対象GA4 PropertyのViewer相当権限を付与する。

## 10.3 Credential isolation

現行の公式実装は`analytics.readonly` scopeのApplication Default Credentials（ADC）を利用するため、Webマルチテナント用途ではCredential境界を明示的に扱うMCP互換Adapterが必要である。

共有Web Serverで、全ユーザー共通の`google.auth.default()`やglobal `_CREDENTIALS`をそのまま利用してはならない。

Credential Providerは少なくとも次のContextを受け取る。

```text
user_id
data_source_set_id
ga4_auth_mode
```

USER_OAUTHではuser_id単位、SERVICE_ACCOUNTではData Access Profile単位でCredentialを分離する。

## 10.4 GA4要件

| **ID** | **要件** | **優先度** | **受入条件 / 補足** |
|---|---|---:|---|
| FR-GA4-001 | USER_OAUTHのAccount/Property discoveryはユーザー本人Credentialで実行する。 | MUST | ユーザーがGA4で閲覧可能なPropertyを取得。 |
| FR-GA4-002 | Report / Realtime / Funnel / Metadata系Toolを利用可能にする。 | MUST | 利用可能ToolはRegistryで検出。 |
| FR-GA4-003 | property_idはBackendのSet Contextで上書き/検証する。 | MUST | Allowlist外Propertyを拒否。 |
| FR-GA4-004 | Credentialをrequest-scopedで利用する。 | MUST | global credential cacheによるユーザー/会社混線を防止。 |
| FR-GA4-005 | Quota / 429 / timeoutをRetry PolicyとCacheで処理する。 | MUST | ユーザーへ再試行/既存Cache利用を説明。 |
| FR-GA4-006 | GA4はMVPでread-onlyを維持する。 | MUST | write scope不要。 |
| FR-GA4-007 | USER_OAUTHからSERVICE_ACCOUNTへ自動fallbackしない。 | MUST | 閲覧範囲拡大を防止。 |
| FR-GA4-008 | USER_OAUTH時はGoogle側GA4アクセスとApp Access Grantの両方を要求する。 | MUST | 二重制御。 |
| FR-GA4-009 | OAuth Credentialをユーザー単位で安全に保存・更新・失効できる。 | MUST | tokenをFrontend/Logへ露出しない。 |

# 11. BigQuery Remote MCP統合

Google Cloud公式BigQuery Remote MCPを標準BigQuery接続とする。BigQuery APIを有効化するとRemote MCPが利用でき、HTTP endpointは https://bigquery.googleapis.com/mcp。認証はOAuth 2.0 + IAMでGoogle Cloud identitiesを利用できる。

## 11.1 採用Tool

| **Tool**             | **用途**                                          | **MVP Policy**          |
|----------------------|---------------------------------------------------|-------------------------|
| list_dataset_ids     | 接続テスト / 管理Discovery                        | Admin only              |
| get_dataset_info     | Dataset metadata                                  | Allowlisted projectのみ |
| list_table_ids       | Table discovery                                   | Allowlisted datasetのみ |
| get_table_info       | Schema / Policy Tag / metadata                    | SQL生成前に利用可       |
| execute_sql_readonly | SELECT / ML.EVALUATE / ML.PREDICT / AI.FORECAST等 | 標準実行Tool            |
| execute_sql          | DDL/DML含む任意Query                              | MVP Deny                |

## 11.2 公式Remote MCPの制約を設計要件化

> **BigQuery MCPは「分析結果を返すTool」として使い、大量行を運ぶデータパイプラインとして使わない。**

| **ID**       | **要件**                                               | **優先度** | **受入条件 / 補足**                                            |
|--------------|--------------------------------------------------------|------------|----------------------------------------------------------------|
| FR-BQMCP-001 | 実行ProjectはPlatformの指定Projectに固定する。         | MUST       | 課金先と監査を一元化。                                         |
| FR-BQMCP-002 | execute_sql_readonlyを標準としexecute_sqlをDenyする。  | MUST       | MVPはSELECT-only。                                             |
| FR-BQMCP-003 | Query結果は3,000行以内になるようSQLを設計する。        | MUST       | Top-N/集計/サンプリング/AI集約。超過想定SQLは実行前に修正。    |
| FR-BQMCP-004 | 3分以内に完了するQueryをMVP対象とする。                | MUST       | 長時間Queryは事前集計/条件絞り込み。将来Long-running Adapter。 |
| FR-BQMCP-005 | Google Drive external tableをMCP分析対象から除外する。 | MUST       | 公式Toolの制約。                                               |
| FR-BQMCP-006 | BigQuery job labelsを監査に利用する。                  | SHOULD     | MCP自動labelに加え、company/sessionの非PII短縮IDを付与。       |

# 12. BigQuery Query Safety / Cost Guard

Remote MCPのexecute_sql_readonlyはdryRunをサポートし、totalBytesProcessed等を返す。一方、現行Tool schemaにはmaximum_bytes_billedが公開されていないため、MVPでは「dry run → budget判定 → 実行」を必須とし、絶対的な1Query hard capが必要になった時点でSafety Adapter（BigQuery APIまたはカスタムMCP）を追加する。

| **ID**    | **要件**                                                                 | **優先度** | **受入条件 / 補足**                                        |
|-----------|--------------------------------------------------------------------------|------------|------------------------------------------------------------|
| FR-BQ-001 | SQLはSELECT-onlyをAST/Parserでも検証する。                               | MUST       | MCP Tool制約に加えアプリ側でも二重チェック。               |
| FR-BQ-002 | 参照Project/Dataset/TableをAllowlist検証する。                           | MUST       | Data Source Set外を拒否。                                  |
| FR-BQ-003 | 実行前にexecute_sql_readonly(dryRun=true)を呼ぶ。                        | MUST       | bytes estimate / syntax validationをExecution Logへ保存。  |
| FR-BQ-004 | Dry run推定がCompany/Query budgetを超えたら実行しない。                  | MUST       | 期間/列/粒度の絞り込みをPlannerへ返す。                    |
| FR-BQ-005 | GA4 events\_\*では日付suffix条件を必須にする。                           | MUST       | 無制限wildcard scan禁止。                                  |
| FR-BQ-006 | SELECT \*を原則禁止し、必要列を明示する。                                | MUST       | コストとPII露出を抑制。                                    |
| FR-BQ-007 | 結果行数はSQL側で\<=3,000を保証する。                                    | MUST       | LIMITだけでなく分析意図に応じた集約を優先。                |
| FR-BQ-008 | 絶対的maximum_bytes_billedが要件化された場合はSafety Adapterを導入する。 | SHOULD     | Remote MCP単独ではHard capを表現できない現行仕様への対応。 |

## 12.1 コスト制御

| **制御**              | **実装**                                                |
|-----------------------|---------------------------------------------------------|
| Dry Run               | ToolのdryRun=trueでtotalBytesProcessedを事前取得        |
| Company Budget        | 日次/月次のsoft/hard budgetと通知                       |
| Query Budget          | 分析種別ごとのdry-run上限                               |
| SQL Shape             | suffix/partition filter、列限定、Top-N、pre-aggregation |
| Auto Exploration      | 1回答あたりBQ Query数上限                               |
| Billing Observability | queryId / bytes processed / bytes billedを監査DBへ保存  |

# 13. BigQuery AI / 非構造化データ分析

BigQuery MCPはGoogleSQLを実行するため、SELECT内で利用可能なBigQuery AI / ML機能を分析計画に組み込める。非構造化テキストの要約・分類・構造化抽出はBigQuery内で実行し、LLMへは集約結果だけを返すことを基本とする。

```text
例：10万件の問い合わせ本文
↓ BigQuery MCP (SELECT + AI.GENERATE / AI.AGG 等)
↓ BigQuery内で分類・要約・GROUP BY
↓ 「テーマ / 件数 / 代表要約」数十行
↓ 外側LLMが事業示唆として説明
```

| **ID**      | **要件**                                                       | **優先度** | **受入条件 / 補足**                                                 |
|-------------|----------------------------------------------------------------|------------|---------------------------------------------------------------------|
| FR-BQAI-001 | BigQuery AI利用をCompany/Data Source Set単位でOpt-in管理する。 | MUST       | 未許可CompanyではAI.\* SQLを拒否。                                  |
| FR-BQAI-002 | 許可するAI/ML functionをAllowlistする。                        | MUST       | MVP例：AI.GENERATE系、AI.FORECAST、ML.EVALUATE/PREDICTのread-only。 |
| FR-BQAI-003 | AIへ渡してよい列/データ分類をPolicy化する。                    | MUST       | PII/機密列は原則禁止またはMask。                                    |
| FR-BQAI-004 | BigQuery AIのモデル利用コストをBQ Query costと別に計測する。   | MUST       | 予算Dashboardで分離表示。                                           |
| FR-BQAI-005 | 非構造化データを行単位で外側LLMへ大量送信しない。              | MUST       | BigQuery内で要約/分類後に縮約。                                     |
| FR-BQAI-006 | AI生成結果には生成モデル/Prompt/SQL/Evidenceを記録する。       | MUST       | 再現性と監査。                                                      |

## 13.1 Prompt Injection / データ由来命令への対策

page_title、URL、event_params、問い合わせ本文等は「データ」であり「命令」ではない。外側LLMへの投入時はデータ境界を明示し、BigQuery AIに渡す場合も固定Prompt Templateと列allowlistを使う。Remote MCPにModel Armorを適用する場合、ログへPayload全文が残る設定のプライバシー影響を事前評価する。

# 14. GA4 ↔ BigQuery 一貫性・Source Reconciliation

GA4 Data APIとGA4 BigQuery Exportは同じProperty由来でも、処理時刻、Attribution、Identity、モデリング、Semantic定義によって数値差が生じ得る。AIは差を「誤差」として隠さず、Sourceと定義をEvidenceとして明示する。

| **ID**     | **要件**                                                            | **優先度** | **受入条件 / 補足**    |
|------------|---------------------------------------------------------------------|------------|------------------------|
| FR-REC-001 | Data Source SetでGA4 Propertyと対応BQ Datasetを明示対応付ける。     | MUST       | 自動推測しない。       |
| FR-REC-002 | 同一Canonical MetricのGA4/BQ mappingをSemantic Layerで保持する。    | MUST       | 比較時に定義差を検出。 |
| FR-REC-003 | 同一質問でGA4/BQを跨ぐ場合はSourceごとの期間/timezoneを正規化する。 | MUST       | 日付境界ズレを防止。   |
| FR-REC-004 | 主要KPIで差が閾値超過時はユーザーへSource差を通知する。             | SHOULD     | 原因候補を表示。       |

# 15. Dataset Cache / Data Lineage / Evidence

CacheはHTTPレスポンスキャッシュではなく、会話中に再利用可能な「分析Dataset」として管理する。Cache Keyは自然言語ではなくNormalized Query + Data Source Set + Semantic Versionから生成する。

```text
cache_key = hash(
data_source_set_id + source +
normalized_dimensions + normalized_metrics +
normalized_filters + date_range + grain + semantic_version
)
```

| **ID**       | **要件**                                                                         | **優先度** | **受入条件 / 補足**                 |
|--------------|----------------------------------------------------------------------------------|------------|-------------------------------------|
| FR-CACHE-001 | Normalized Query Hashで同一データ取得を判定する。                                | MUST       | 表現違いでも同義なら再利用。        |
| FR-CACHE-002 | Datasetにsource/tool/query/semantic_version/fetched_at/TTL/row_countを保存する。 | MUST       | Lineage復元可能。                   |
| FR-CACHE-003 | Freshness Policyをsource/期間で分ける。                                          | MUST       | Realtime、当日、過去確定日を区別。  |
| FR-CACHE-004 | 可視化・追加計算は既存Datasetを優先する。                                        | MUST       | 「グラフにして」でMCP再実行しない。 |
| FR-CACHE-005 | Cache KeyとDB accessにcompany/source_set scopeを必須化する。                     | MUST       | 他社/他Set Cache混線を防止。        |
| FR-EVID-001  | Evidenceにmcp_server/tool_name/query_id/bytes/source_setを保存する。             | MUST       | GA4/BQ共通のEvidence schema。       |

# 16. LLM / Analysis Engine

| **役割**    | **LLM**                          | **コード/ルール**                          |
|-------------|----------------------------------|--------------------------------------------|
| 質問理解    | 意図・曖昧語・比較意図           | Company/Set/Access Profile強制             |
| 分析計画    | 必要な切り口・仮説・追加分析候補 | Router Policy、Step/Cost上限               |
| GA4/SQL生成 | 候補Query/SQL                    | Schema validation、AST、Allowlist、dry run |
| 数値処理    | 説明・意味付け                   | 増減率・率・統計をコードで計算             |
| 可視化      | 何を伝えるか                     | Chart rule / Spec validator / Renderer     |
| 最終回答    | 要点・示唆・次の質問候補         | Evidenceのない数値/断定を拒否              |

| **ID**     | **要件**                                            | **優先度** | **受入条件 / 補足**                            |
|------------|-----------------------------------------------------|------------|------------------------------------------------|
| FR-LLM-001 | LLM Providerを抽象化する。                          | SHOULD     | モデル差替えでState/Tools/DBを維持。           |
| FR-LLM-002 | Planner / Query Composer / Answererを論理分離する。 | MUST       | 同一モデルでもPrompt/Schema分離。              |
| FR-LLM-003 | すべての中間出力をStructured Outputで検証する。     | MUST       | Plan/Query/ChartSpecをJSON Schema validation。 |
| FR-ANL-001 | 増減率・比率・ランキングをコードで計算する。        | MUST       | LLM暗算禁止。                                  |
| FR-ANL-002 | FindingにEvidence IDを要求する。                    | MUST       | 根拠なし断定を防止。                           |
| FR-ANL-003 | ConfidenceとAssumptionをFindingへ保持する。         | SHOULD     | 仮説と確定事実を区別。                         |

# 17. 可視化・表示

LLMにHTML/JSを生成させず、許可されたVisualization Specだけを生成させる。Chart typeはRule-basedを優先し、LLMは「どの比較を見せると分析意図が伝わるか」を補助判断する。

| **データ形状 / 意図** | **推奨表示**                            |
|-----------------------|-----------------------------------------|
| 単一KPI + 前期間比較  | KPIカード + delta                       |
| 時間推移              | 折れ線                                  |
| カテゴリ比較 / Top N  | 棒（多カテゴリは横棒）                  |
| 構成の時系列          | 積み上げ棒                              |
| ファネル              | ファネル + Step率                       |
| 正確な多数値          | 表                                      |
| 少数カテゴリ構成比    | 円/ドーナツ（限定）                     |
| 2変数関係             | 散布図                                  |
| テキスト分類結果      | Topカテゴリ棒 + 要約 + 代表例（匿名化） |

| **ID**     | **要件**                                        | **優先度** | **受入条件 / 補足**                                     |
|------------|-------------------------------------------------|------------|---------------------------------------------------------|
| FR-VIZ-001 | Visualization Spec schemaを定義する。           | MUST       | type,title,x,y,series,format,dataset_ids,evidence_ids。 |
| FR-VIZ-002 | Renderer前に列存在・単位・grain整合を検証する。 | MUST       | 不整合Specを拒否。                                      |
| FR-VIZ-003 | ユーザーが表示形式を変更できる。                | SHOULD     | Dataset再利用。                                         |
| FR-VIZ-004 | グラフからData details/Evidenceを展開できる。   | MUST       | Source/期間/Metric/Filter/Queryを確認可能。             |

## 17.1 回答ドキュメントと可視化の永続性

| ID | 要件 | 優先度／受入条件 |
|---|---|---|
| FR-DOC-001 | 回答はMarkdown原文＋形式を保存し、プレビュー／原文／コピー／.md保存を提供 | MUST。HTML実行・外部画像読込み・危険URLは禁止。既存平文を破壊しない |
| FR-DOC-002 | 事実・改善仮説・限界・次の確認を見出し／表で区別 | MUST。未検証因果を事実としない。コード検証済み数値とEvidenceを保持 |
| FR-DOC-003 | グラフ・表を回答単位で保存し、再読込みで復元 | MUST。新発言で過去グラフを上書きしない。Cache欠損を無断再取得で補わない |
| FR-DOC-004 | タイトルに期間・指標ラベル・集計軸・対象Filter・比較／順位を反映 | MUST。各ページの参照元グラフに対象ページを表示。日付は順序を保証 |
| FR-DOC-005 | 通常の分析表示とレポート作成を分離 | MUST。「レポート不要」でも分析の表・グラフは表示。レポート生成は明示指示時のみ |

現行RendererはKPI／表／折れ線／棒／ドーナツ。上表の積み上げ・専用ファネル図・散布図は将来拡張であり実装済みではない。Funnelデータ取得と専用図の実装を混同しない。

# 18. アプリケーションデータモデル

| **Entity**           | **主要フィールド / 役割**                                                                                               |
|----------------------|-------------------------------------------------------------------------------------------------------------------------|
| users                | id, google_subject, email, status, last_login_at                                                                        |
| ga4_user_connections | user_id, google_subject, google_email, granted_scopes, encrypted_token_ref, status, connected_at, last_verified_at |
| companies            | id, name, status, default_semantic_version                                                                              |
| memberships          | user_id, company_id, role                                                                                               |
| user_source_access   | user_id/role, data_source_set_id, permissions                                                                           |
| data_access_profiles | company_id, service_account_email, allowed_mcp_tools, status（主にBigQuery / optional GA4 SA mode）                    |
| data_source_sets     | company_id, access_profile_id, display_name, ga4_property_id, ga4_auth_mode, bq_project_id, bq_dataset_id, allowlist, semantic_profile |
| semantic_terms       | canonical_name, aliases, type, unit, aggregation, mappings, version                                                     |
| analysis_sessions    | company_id, data_source_set_id, periods, state_json, semantic_version                                                   |
| chat_messages        | session_id, role, content, model_metadata                                                                               |
| tool_executions      | mcp_server, tool_name, args_hash, status, latency, request_id                                                           |
| query_executions     | source, normalized_query/sql, dry_run_stats, query_id, bytes_processed/billed                                           |
| datasets             | query_execution_id, schema, rows/object_uri, fetched_at, expires_at, hash                                               |
| findings             | session_id, statement, metric_value, evidence_ids, confidence, assumptions                                              |
| visualizations       | session_id, dataset_ids, spec_json                                                                                      |
| audit_events         | actor, company, source_set, action, resource, request_id, timestamp                                                     |
| feedback             | message/finding_id, rating, reason, correction                                                                          |

## 18.1 保存戦略

- Semantic / State / metadata / Audit metadataはCloud Firestoreへ保存する。論理Entity名と物理collection名は1対1に固定しない。Application Identityのusers.idはFirebase UID。

- Dataset CacheはFirestore metadata＋Cloud Storage blobを非localの基本とする。ユーザー承認済みのlocal限定構成ではDocker共有volumeにblobを保存し、API／Workerで共有する。新規GCS作成・費用は別途承認まで保留。大きな結果をFirestoreへ直接格納しない。

- BigQuery RawデータをアプリDBへ恒久複製しない。3,000行以内の縮約結果をTTL付きで保持する。

- SA秘密鍵は保持しない。Service Account email / Profile設定のみDBに保存する。

- GA4 USER_OAUTHのrefresh tokenは平文保存しない。Cloud KMS等で暗号化するか、同等以上のserver-side secret storageを利用する。

# 19. UI/UX要件

- 画面上部にCompany / Data Source Set（例：A社 Web）を常時表示し、分析対象を誤認させない。

- 同一会社で複数Setへの権限がある場合、Set切替は明示操作にする。

- 回答は「結論 → KPI → 可視化 → AI分析 → 次の深掘り候補 → Data details」の順を基本とする。

- Data detailsにはSource（GA4/BQ/BQ AI）、期間、Canonical Metric、Filter、取得時刻、Evidenceを表示する。

- BigQuery分析中は「Schema確認 → Dry run → 実行 → 分析」の進捗を表示する。

- 3分制約やBudget超過で実行できない場合、ユーザーに期間/粒度を絞る提案を返す。

```text
[A社 Web ▼] 期間: 8/1-8/24 vs 7/1-7/24
結論: CVは前月比 -12.4%。主因はOrganic Searchの低下。
[KPI cards]
[CV推移 chart] [Channel contribution chart]
AI分析
・Organic CV -28%
・/product/a の寄与が最大
次の深掘り: [LP別] [デバイス別] [購入前行動(BQ)]
Data details ▸ GA4 MCP / semantic v3 / evidence #...
```

# 20. セキュリティ・プライバシー

| **ID**      | **要件**                                                                    | **優先度** | **受入条件 / 補足**                    |
|-------------|-----------------------------------------------------------------------------|------------|----------------------------------------|
| NFR-SEC-001 | Service Account Keyを本番で使用しない。 | MUST | BigQuery / optional GA4 SA modeはRuntime identity + impersonation。 |
| NFR-SEC-002 | 全App data rowにcompany_id、分析Entityにはsource_set_idを持たせる。         | MUST       | Repository層でscope必須。              |
| NFR-SEC-003 | Tool call前後でData Source Set / Access Profile / allowlistを再検証する。   | MUST       | Prompt injectionでも境界越え不可。     |
| NFR-SEC-004 | BigQuery PII/識別子をData Classificationし、通常回答へのRaw出力を禁止する。 | MUST       | user_pseudo_id等はaggregate/mask。     |
| NFR-SEC-005 | 外部データ文字列を命令として扱わないPrompt boundaryを実装する。             | MUST       | URL/title/comment由来のInjection対策。 |
| NFR-SEC-006 | BigQuery AIへ渡す列をPolicy/Tagで制限する。                                 | MUST       | 機密列を生成AI処理へ送らない。         |
| NFR-SEC-007 | Remote MCPのread-write ToolをIAM/Deny policyでも制限可能にする。            | SHOULD     | execute_sqlを多層でDeny。              |
| NFR-SEC-008 | Model Armor適用はCompany/region/Logging policyを評価して選択する。 | SHOULD | Payload loggingによる機密露出を考慮。 |
| NFR-SEC-009 | GA4 USER_OAUTH tokenをFrontend/Logへ露出しない。 | MUST | Refresh tokenは暗号化/Secret管理。 |
| NFR-SEC-010 | Firebase/OIDC login identityとGA4 OAuth identityの不一致をMVPでは拒否する。 | MUST | 別Google Accountの誤接続を防止。 |
| NFR-SEC-011 | USER_OAUTH権限不足時にSAへ自動fallbackしない。 | MUST | 権限昇格防止。 |

# 21. 非機能要件

以下の応答時間・可用性等は目標値であり、現環境の測定済みSLOではない。単一APIの時間とAgent全体の時間を分け、品質・制限内の完走率を優先して評価する。

| **カテゴリ**     | **要件**                                                                  |
|------------------|---------------------------------------------------------------------------|
| セキュリティ     | Tenant/Set分離、GA4 User OAuth、最小権限、BQ SA impersonation、read-only、監査。 |
| 可用性           | MVP 99.5%/月目標。外部MCP/API障害時に明示エラーと再実行導線。             |
| 性能             | UI応答開始\<1秒、Cache\<5秒、通常GA4\<10秒目標。BQは段階進捗。            |
| BQ実行時間       | MCPの既定3分内に収まるSQLをMVP対象。超える分析はQuery rewrite。           |
| スケール         | Web/API水平スケール。StateはDBから復元。                                  |
| 観測性           | request_idでWeb→LLM→MCP→Dataset→AnswerをTrace。                           |
| コスト           | GA4 quota telemetry、BQ dry-run、Company budget、BigQuery AI model cost。 |
| 保守性           | MCP/LLM/Semantic/RendererをInterfaceで疎結合。                            |
| アクセシビリティ | キーボード操作、色だけに依存しない状態表現。                              |

# 22. 監査・Observability・評価

| **ID**      | **要件**                                                       | **優先度** | **受入条件 / 補足**                             |
|-------------|----------------------------------------------------------------|------------|-------------------------------------------------|
| FR-AUD-001 | 実行者・Company／Set・操作・時刻・相関IDを監査する。 | MUST | 質問本文は認可されたMessageとして保持。Log／Auditへraw promptやTokenを出さずmessage_id等で関連付ける。 |
| FR-AUD-002  | 全MCP Tool callを記録する。                                    | MUST       | server/tool/target/latency/status。             |
| FR-AUD-003  | BQ queryId / dryRun bytes / processed/billed bytesを保存する。 | MUST       | Cost追跡。                                      |
| FR-AUD-004 | 回答model／prompt template version／semantic／tool versionを保存する。 | MUST | 再現性。raw promptはAuditに複製しない。 |
| FR-EVAL-001 | Semantic Golden Setを持つ。                                    | MUST       | 日本語表記揺れ。                                |
| FR-EVAL-002 | Question→Route→Query mappingの正解テストを持つ。               | MUST       | GA4/BQ/BQ AI選択を評価。                        |
| FR-EVAL-003 | SQL Safety regression testを持つ。                             | MUST       | Cross-tenant/SELECT\*/no-date/too-many-rows等。 |
| FR-EVAL-004 | 回答Feedbackを収集する。                                       | SHOULD     | 👍/👎 + 理由 + 正しい定義。                     |

## 22.1 品質KPI

| **KPI**                         | **初期目標**                          |
|---------------------------------|---------------------------------------|
| Company/Data Source Set境界違反 | 0件                                   |
| 重要Semantic Top-1精度          | 99%目標                               |
| 一般Semantic Top-1精度          | 95%以上                               |
| Evidence付与率                  | 主要数値100%                          |
| BQ危険SQL実行                   | 0件                                   |
| BQ結果3,000行超過による失敗     | Pilotでほぼ0を目標（事前shapeで抑制） |
| Cache hit率                     | 定常利用40%以上を目標                 |

# 23. 主要シーケンス

## 23.1 GA4軽量分析

- 1. ユーザーがData Source Setを選択して質問。
- 2. BackendがUser Access GrantとData Source Setを検証。
- 3. Semantic ResolverがCanonical metric/filter/periodへ変換。
- 4. Cache検索。Hitなら外部Toolを呼ばない。
- 5. RouterがGA4 MCPを選択し、Backendがproperty_idをSetから注入。
- 6. Credential Brokerが`ga4_auth_mode`を確認。
- 7. `USER_OAUTH`の場合、対象ユーザーの`analytics.readonly` OAuth Credentialを取得する。
- 8. `SERVICE_ACCOUNT`の場合のみ、Access Profile SAの短期Credentialを取得する。
- 9. 未採用のauth modeは拒否する。`SERVICE_ACCOUNT_AND_USER_ACCESS`の運用は将来の別途承認事項。
- 10. GA4 MCP Adapter実行 → 共通Datasetへ正規化。
- 11. Google側で403等になった場合、SAへ自動fallbackせず、権限不足/再接続として処理する。
- 12. コードで比較/増減率を計算 → Finding/Evidence生成。
- 13. Answerer + Visualization Selectorが回答/Chart Specを作成。

## 23.2 BigQuery深掘り

- 1. RouterがBQ分析と判断。get_table_info等で必要Schemaを確認。

- 2. SQL ComposerがSet allowlistを前提に集約SQLを生成。

- 3. AST GuardがSELECT-only、参照先、日付条件、列、結果shapeを検証。

- 4. BigQuery MCP execute_sql_readonly(dryRun=true)でbytes estimate。

- 5. Budget Policy通過後、dryRun=falseで実行。

- 6. 3,000行以下の結果をDataset Cacheへ保存し、queryId/bytesをEvidenceへ記録。

- 7. Analysis Engineが既存GA4 Datasetと必要に応じて比較し、回答・Chartを生成。

## 23.3 BigQuery AIによる非構造化分析

- 1. Company/SetのBQ AI Opt-inとData Classificationを確認。

- 2. Plannerが「分類/要約/抽出」TaskとOutput schemaを定義。

- 3. SQL ComposerがAI function + aggregationを含むSELECTを生成。

- 4. GuardがAI function/endpoint/対象列をAllowlist検証。

- 5. dryRun/Cost Policyを通過後にMCPで実行。

- 6. BigQuery内で大量rowを処理し、MCPには集約結果のみ返す。

- 7. 外側LLMは結果を事業文脈へ解釈し、Evidence付きで説明。

# 24. 外部API・主要コンポーネント

| **コンポーネント**                      | **用途**                                         | **MVP**          |
|-----------------------------------------|--------------------------------------------------|------------------|
| Google Analytics MCP-compatible Adapter | GA4 Admin/Data API Tool                          | 必須             |
| Google BigQuery Remote MCP              | BQ metadata / read-only SQL / AI/ML SQL          | 必須             |
| IAM Service Account Credentials API     | BigQuery Profile SA / optional GA4 SA Impersonation | 必須          |
| Google OAuth/OIDC                       | 人のログイン + GA4 USER_OAUTH (`analytics.readonly`) | 必須          |
| MCP Gateway / Capability Registry       | 2-MCP統一Policy/監査                             | 必須             |
| Analysis Orchestrator                   | Semantic/Planner/Router/State                    | 必須             |
| Cloud Firestore | App DB / Semantic / State / Audit metadata | 採用 |
| Object Storage                          | 大きめDataset Cache / export                     | 候補             |
| Cloud Run                               | Web/API/Worker                                   | 候補             |
| Model Armor                             | MCP prompt/response security                     | 任意 / 評価後    |
| BigQuery API Safety Adapter             | maximum_bytes_billed / long-running escape hatch | MVP外 / 条件付き |

# 25. 採用技術スタック

| 層 | 採用 |
|---|---|
| Frontend | SvelteKit／TypeScript strict／Firebase Web SDK／Apache ECharts |
| API／Orchestrator | Python 3.12／FastAPI／Pydantic／MCP Client |
| Identity | Firebase UID、GA4 User OAuth、BigQuery Profile SA（保留） |
| Runtime | local Docker Compose、クラウドCloud Run（リリース検証未完） |
| Metadata | Cloud Firestore |
| Dataset blob | 非localはCloud Storage、localは承認済みDocker共有volume |
| Credential | Cloud KMS暗号化／Secret Manager／keyless SA Impersonation |
| LLM | Vertex AI Gemini。数値・認可・取得条件・描画はコードで検証 |
| Infrastructure | Terraform／Docker／GitHub Actions |
| Quality | pytest／Ruff／mypy、Vitest／ESLint／Prettier／Playwright |

# 26. 開発フェーズと実行順序

以下のID・順序を[TODO](todo.md#現在の実行順序)と[実装計画](implementation-plan.md)で共通使用する。
P0〜P5は既存バックログの分類であり、番号順に作業する指示ではない。

| 順序／ID | 小さな区切り | 完了条件 |
|---|---|---|
| Q1 | ローカル品質ベースライン | 合成テスト、実Gemini、実保存履歴、画面を区別して記録。実GA4突合未完を明示 |
| Q2a | 引用済み履歴の確実な復元 | 一般検索上位8件から漏れても引用元の段階計画・根拠を再認可して復元。追加取得0。権限失効・曖昧・欠損は拒否 |
| Q2b | 実GA4の分析品質Acceptance | 承認済み対象・期間・定義で数値突合。参照元比較→深掘り→説明→再読込み。正しいScope未登録の場合は止めてS1の該当登録を先行 |
| Q3 | Skill／計画の品質拡張 | Q2の失敗例を1件ずつGolden testに固定。任意DAGや全GA4対応を一括約束しない |
| R1 | 切断・再送・再起動の信頼性 | Operation ledger、安全な再試行、回答再現、段階進捗。手動「続き」を通常操作に要求しない |
| S1 | P0認可差異＋Admin | 正確なProperty／Stream／timezone登録、明示Grant、監査、既存データ移行、negative test |
| A1 | 実Identity／運用Acceptance | OAuth失効・分離・同時実行・定期実行停止・保存復旧を確認 |
| REL | GA4 Pilot | P0を残さず、品質・運用・リリース受入完了。正式Dual-MCP MVPとは区別 |
| BQ | BigQuery再開（保留） | SA／Query Guard／実データ受入を完了して正式MVPへ |

Q1は本改訂時に実施。結果と残課題は[ローカル品質評価](evaluation/local-analysis-quality-2026-09-16.md)を参照。
順序変更はP0の免除や、既存の自動Grant作成の承認を意味しない。

# 27. MVP受入シナリオ

以下は正式Dual-MCP MVPの要件。BigQueryを必要とする12〜18・20は現在保留であり、GA4 Pilotの合格をもって代替しない。ローカルの先行評価は27.1節で別管理する。

| **#** | **シナリオ** | **期待結果** |
|---:|---|---|
| 1 | A社ユーザーがログイン | 許可されたA社のData Source Setのみ選択可能。 |
| 2 | GA4接続時に`analytics.readonly`を許可 | ユーザー本人のOAuth CredentialでGA4 Property discovery可能。 |
| 3 | User AはGA4上でWeb閲覧可・店舗閲覧不可、App GrantはWebのみ | Web分析成功。店舗は実行不可。 |
| 4 | User AはGA4上でWeb/店舗とも閲覧可、App GrantはWebのみ | Web分析成功。店舗はBackend Authorizationで拒否。 |
| 5 | USER_OAUTHでGA4側Web権限を削除 | 次回Tool callは403等を処理し、再接続/権限確認を案内。SAへ自動fallbackしない。 |
| 6 | User A/Bが同時利用 | OAuth Credentialが混線せず、各ユーザー本人のGA4範囲だけを利用。 |
| 7 | SERVICE_ACCOUNT modeのSet | SAにViewerがあるPropertyのみ分析成功。 |
| 8 | A社 Webで「今月のユーザー数」 | Semantic定義済みactiveUsersをGA4 MCPで取得。 |
| 9 | 「前月と比較」 | 同一Set/Metricで比較し、増減率をコード計算。 |
| 10 | 「Organicだけ」 | 直前Stateを継承しFilter追加。 |
| 11 | 「グラフにして」 | 既存Datasetを使いMCP再実行なし。 |
| 12 | 「購入前7日間のページ」 | BigQuery MCPへルーティング、Company SAでdryRun→safe SELECT。 |
| 13 | LLMが別SetのDataset SQLを生成 | AST/Allowlist/IAMのいずれかで拒否、実行されない。 |
| 14 | events_*を日付条件なしで読むSQL | Guardで拒否/修正。 |
| 15 | 推定結果が3,000行超 | SQLを集約/Top-N化してから実行。 |
| 16 | QueryがdryRun budget超過 | 本実行せず、期間/粒度縮小を提案。 |
| 17 | 問い合わせ10万件を分類 | BQ AI許可時のみBigQuery内で分類し、集約結果を返す。 |
| 18 | BQ AI未許可CompanyでAI.GENERATEを生成 | Policyで拒否。 |
| 19 | 回答の数値を確認 | EvidenceからMCP Tool/GA4 Query/SQL/queryIdへ追跡可能。 |
| 20 | GA4/BQ同一KPIに差 | SourceとSemantic mapping差を明示し、無理に一致させない。 |

## 27.1 ローカル品質の先行受入

| ID | シナリオ | 判定条件 |
|---|---|---|
| QA-01 | 5月と6月の参照元比較 | 両月・参照元軸を保持。全体KPIだけで完了しない |
| QA-02 | Organic→mobile→期間比較 | 明示Filter／比較条件と認可Scopeを継承 |
| QA-03 | 月別UU／日付順序 | 正しい月次クエリ、非加算性、Cache分離、時系列順序 |
| QA-04 | PV上位5ページ→各参照元上位5 | 選定1＋対象別5の根拠・Filter・表示。通常「続き」不要 |
| QA-05 | 新規取得なしの説明／競合履歴 | 引用済み計画・全6分析復元、追加取得0、仮説と事実を区別 |
| QA-06 | 再読込み・Markdown・狭い画面 | 回答単位のグラフ、対象タイトル、原文保持、安全描画 |
| QA-07 | 未定義CV・非互換・権限失効 | 推測実行／権限越境しない。理由と可能な代替を提示 |
| QA-08 | 実GA4数値の突合 | Property／Stream／期間／timezone／指標／軸／Filterを合わせ、UIまたは独立した基準と突合 |

QA-01〜07の合成検証、実Gemini検証、実保存履歴検証とQA-08を混同しない。件数・所要時間・失敗理由を日付付き評価記録に残す。最新結果は[評価記録](evaluation/local-analysis-quality-2026-09-16.md)を参照。

# 28. リスクと対策

| **リスク**                               | **影響**           | **対策**                                                  |
|------------------------------------------|--------------------|-----------------------------------------------------------|
| 日本語Semanticの誤解                     | 誤KPI              | Set別辞書、Confidence、Golden Set、Evidence               |
| GA4 MCPのlocal ADC/global credential前提 | User OAuth/SA Credential混線 | request-scoped Adapter / user_id + auth_mode単位のcredential isolation |
| BigQuery MCP 3,000行制限                 | 分析失敗           | BQ内集計・Top-N・Result Shape Policy                      |
| BigQuery MCP 3分制限                     | 長時間分析失敗     | partition/filter/preaggregation、将来Long-running Adapter |
| Remote MCPにmaximum_bytes_billedなし     | コストHard cap不足 | dryRun + Budget、必要時Safety Adapter                     |
| 他Set/他社データ誤参照                   | 情報漏えい         | GA4 User permission + user→Set allowlist、BQ Access Profile、AST、IAM |
| BQ AIへの機密データ送信                  | プライバシー事故   | Opt-in、column policy、Mask、監査                         |
| GA4/BQ数値差                             | 利用者混乱         | Source/定義/処理差を説明、Reconciliation Rule             |
| USER_OAUTH失敗時のSA fallback            | 権限昇格/情報漏えい | 自動fallback禁止、auth mode固定、監査                      |
| LLM結論飛躍                              | 誤意思決定         | Finding-Evidence必須、計算コード化、Confidence            |

# 29. 実装前に確定する設計判断

| **論点**             | **推奨初期案**                                                    | **決定タイミング**  |
|----------------------|-------------------------------------------------------------------|---------------------|
| GA4認証標準          | USER_OAUTH + `analytics.readonly`。SAはData Source Set単位の任意モード | Phase 0          |
| GA4 MCP形態          | Upstream Tool contractを維持したrequest-scoped credential Adapter | Phase 0             |
| BQ MCP auth          | Data Access Profile SAの短期CredentialでRemote MCP                | Phase 0             |
| Access Profile粒度   | BigQueryは標準1社1SA。GA4 SA modeを使うSetのみ必要に応じ共有/分離 | Onboarding設計      |
| BigQuery AI          | BigQuery再開後にCompany／DSS Opt-in                                           | Pilot前             |
| BQ hard cap          | MVP dryRun budget。必要時Safety Adapter                           | Pilotでコスト実績後 |
| Long-running query   | MVP対象外。3分超はrewrite                                         | Pilot後             |
| Semantic ownership   | 事業会社管理者 + データ担当の承認フロー                           | Pilot前             |
| Chat/Dataset保持期間 | Company policy化                                                  | Pilot前             |
| Cross-company分析    | MVP無効。専用Access Profile/承認モデル後                          | Phase 3+            |

# 30. 参考仕様・外部制約（2026-09-01確認）

## Google Analytics / GA4

- [Google公式: Try the Google Analytics MCP server](https://developers.google.com/analytics/devguides/MCP)
- [Google公式実装: googleanalytics/google-analytics-mcp](https://github.com/googleanalytics/google-analytics-mcp)
- [Google Analytics MCP README](https://github.com/googleanalytics/google-analytics-mcp/blob/main/README.md)
- [Google Analytics Data API quotas](https://developers.google.com/analytics/devguides/reporting/data/v1/quotas)
- [Google Analytics Admin API quotas](https://developers.google.com/analytics/devguides/config/admin/v1/quotas)
- [Google OAuth 2.0 Scopes — `analytics.readonly`](https://developers.google.com/identity/protocols/oauth2/scopes)
- [GA4 BigQuery Export schema](https://support.google.com/analytics/answer/7029846?hl=ja)

## BigQuery / Google Cloud

- [BigQuery Remote MCP: Use the server](https://docs.cloud.google.com/bigquery/docs/use-bigquery-mcp)
- [BigQuery MCP reference](https://docs.cloud.google.com/bigquery/docs/reference/mcp)
- [BigQuery `execute_sql_readonly` MCP Tool](https://docs.cloud.google.com/bigquery/docs/reference/mcp/tools_list/execute_sql_readonly)
- [BigQuery AI.GENERATE](https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/bigqueryml-syntax-ai-generate)
- [BigQuery pricing](https://cloud.google.com/bigquery/pricing)
- [Service account impersonation](https://docs.cloud.google.com/iam/docs/service-account-impersonation)
- [Service account best practices](https://docs.cloud.google.com/iam/docs/best-practices-service-accounts)

# 付録A. 実装時の「絶対に守る」チェックリスト

☐ Data Source Setを選択していない状態でデータToolを実行しない。

☐ Company / Set / Access Profile境界をLLM promptだけで制御しない。

☐ 同じ会社でもユーザーごとにData Source Setの許可範囲を制限できるようにする。

☐ GA4の標準認証はUSER_OAUTH + `analytics.readonly`とし、ユーザー本人のGA4閲覧範囲を利用する。

☐ Firebase/OIDCログインだけでGA4 API認可済みとみなさず、GA4 OAuth Consentを実装する。

☐ GA4 MCPのCredentialをrequest-scopedにし、User OAuth / SAのglobal credential cacheを共有しない。

☐ USER_OAUTHの権限不足時にService Accountへ自動fallbackしない。

☐ BigQueryおよびoptional GA4 SA modeでService Account JSON Keyを配布・保存しない。

☐ GA4 User OAuthのrefresh tokenをFrontend/Logへ出さず、平文保存しない。

☐ BigQueryはexecute_sql_readonlyを標準とし、execute_sqlをMVPでDenyする。

☐ BQ実行前にdryRunし、参照先・日付条件・列・bytes・結果shapeを検証する。

☐ BigQuery MCPの3,000行/3分制約を超える設計にしない。

☐ BigQuery AIはCompany Opt-in + function/column allowlistを必須とする。

☐ 数値計算をLLMに任せず、Evidenceを必ず紐付ける。

☐ Semantic LayerでGA4/BQ両方のMappingをVersion管理する。

☐ Analysis StateとChat Transcriptを分離する。

☐ Cache Keyは自然言語ではなくNormalized Queryで作る。

☐ グラフは検証済みVisualization Specから描画する。

☐ Raw identifier / PIIを通常回答へ出さない。

# 付録B. 本システムの価値定義

> **MVPの価値は「チャットができること」ではない。同一のAnalysis StateとSemantic Layerの上で、GA4 MCPとBigQuery MCPを安全に行き来し、分析粒度を上げ、必要に応じBigQuery AIで非構造化データまで処理し、その根拠を再利用・可視化できることにある。**

この構造が成立すれば、将来Search Console、Google Ads、CRM、売上DBなどのTool/MCPを追加しても、ユーザー体験とSemantic/Evidenceモデルを維持したまま「社内Conversational Analytics Platform」へ拡張できる。
