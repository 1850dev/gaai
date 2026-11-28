# GA4 × BigQuery Analytics AI
## 機能要件・システム構成書 v1.2

更新日: 2026-09-01

Status: Current Source of Truth

本書はv1.1のGA4認証モデルを訂正する。Semantic Layer、Analysis State、Query Router、Cache、Evidence、Visualization、BigQuery Query Guardなど、ここで変更しないv1.1要件は引き続き必須である。競合する場合は本書とADR 0009を優先する。

# 1. 絶対条件

```text
GA4標準:
  ログインユーザー本人のGoogle OAuth delegated access

GA4追加オプション:
  Data Source Setで明示したService Account managed access

BigQuery標準:
  Company / Data Access Profile Service Account
```

Firebase AuthenticationとGA4 OAuth Authorizationを同一Credentialとして扱ってはならない。GA4 `USER_OAUTH`失敗時にService Accountへfallbackしてはならない。BigQueryをユーザーOAuthで実行してはならない。

# 2. Identity model

## 2.1 Application Identity

Google Login → Firebase Authentication → Firebase UIDをApplication User Identityとする。Firebase ID TokenはBackendが検証し、Frontendから受け取ったUIDを信用しない。

## 2.2 GA4 USER_OAUTH

エンドユーザーはBackend Web Server Authorization Code Flowで`analytics.readonly`を追加同意する。openid、email、profileを使いOAuth Identityを確認し、MVPではFirebase Google subject/emailと完全に同じGoogle Accountを要求する。

必須control:

- `access_type=offline`
- `include_granted_scopes=true`
- PKCE S256
- 高entropyで一度限りのstate
- state transactionの短いTTL
- exact redirect URI
- Backend-only callback／token exchange
- Client SecretはSecret Manager
- Refresh TokenはCloud KMS暗号化
- Firestoreへ暗号文だけ保存
- Token／Code／PKCE verifierをFrontend、Log、Auditへ出さない

## 2.3 GA4 SERVICE_ACCOUNT

特殊契約や内部運用のため残す。Data Source Setの`ga4.auth_mode=SERVICE_ACCOUNT`を必須とし、verified Data Access Profileを利用する。このmodeだけ対象PropertyへService Account Viewerを付与する。標準オンボーディングやfallbackとして使用しない。

## 2.4 BigQuery

BigQueryはData Access Profile Service AccountをCloud Run Runtimeからkeyless impersonationする。Service Account Keyは禁止する。Customer側IAMはDataset／Authorized View単位の`roles/bigquery.dataViewer`を標準とし、Project全体Data Viewerを標準にしない。

# 3. Data Source Set authorization

Data Source SetはCompanyより細かいデータ境界である。Membershipだけでは許可しない。

GA4 USER_OAUTHの実効許可:

```text
Google UserがPropertyを閲覧可能
AND Data Source Setに正確なProperty／Streamが登録済み
AND Firebase UIDにUser Access Grantが存在
```

BigQueryの実効許可:

```text
Profile Service AccountがDataset／Viewへアクセス可能
AND Data Source Set allowlistにResourceが登録済み
AND Firebase UIDにUser Access Grantが存在
```

LLMまたはHTTP callerが指定したProperty、Project、Dataset、Table、Credential、Toolは信用しない。BackendがData Source SetとAuthorized Sessionから注入する。

# 4. Data model

最低限次を管理する。

```text
users
companies
companyMembers
dataAccessProfiles
dataSourceSets
userAccessGrants
ga4UserConnections
ga4OAuthTransactions
ga4PropertyAccess
semanticDefinitions
analysisSessions
messages
findings
datasetCache
auditEvents
```

`DataSourceSet.ga4`:

```json
{
  "property_id": "100000001",
  "display_name": "Example Site Overall",
  "auth_mode": "USER_OAUTH",
  "timezone": "Asia/Tokyo",
  "currency_code": "JPY",
  "stream_scopes": [
    {"stream_id": "200000001", "platform": "web"}
  ]
}
```

`ga4UserConnections/{uid}`はuser_id、google_subject、google_email、granted_scopes、encrypted_refresh_token、status、connected_at、last_verified_at、token timestampsを持つ。Frontend read/writeは禁止する。

既存GA4 recordに`auth_mode`がない場合は`USER_OAUTH`へ移行する。意図的なService Account tenantだけ明示的`SERVICE_ACCOUNT`へ変更し、Acceptance Testをやり直す。

# 5. OAuth API／UI

Backend API:

```text
GET  /api/integrations/ga4/status
POST /api/integrations/ga4/connect
GET  /api/integrations/ga4/callback
POST /api/integrations/ga4/disconnect
GET  /api/integrations/ga4/properties
```

UIは接続状態、Google email、利用可能Property候補、再接続、接続解除を表示する。Property候補はユーザー本人がGoogle側で閲覧できる範囲だが、自動でData Source SetまたはGrantを作成しない。

Disconnect時は保存暗号文を削除し、Access Token cacheを破棄し、可能な限りGoogle token revokeを行う。

# 6. GA4 MCP Adapter

公式Google Analytics MCPのread-only Tool contractを維持する。CredentialはTool callごとにrequest-scopedで注入し、global ADC cacheをUser／Company間で共有しない。

主要Tool:

- `get_account_summaries`
- `get_property_details`
- `get_custom_dimensions_and_metrics`
- `run_report`
- `run_realtime_report`
- `run_funnel_report`

Property IDはData Source Setから強制注入する。Web／App混在Propertyでは登録済みnumeric Stream IDとplatform filterを強制し、Caller filterはANDで狭めることだけ許可する。

Google側でProperty権限が削除されGA4 APIが403を返した場合、user×Property access statusをdeniedへ更新し、明示的な`ga4_property_access_denied`を返す。Service Accountへ切り替えない。

# 7. BigQuery MCP

BigQuery Remote MCP endpointと`execute_sql_readonly`を使用する。Query Projectは環境のPlatform Projectへ固定し、Productionは`analytics-ai-prod`とする。MCP前段Query Guardはread-only AST、Project／Dataset／Table allowlist、wildcard date range、no general `SELECT *`、no unnecessary CROSS JOIN、outer limit、dry-run bytes budgetを検証する。

BigQuery側認証・IAM・Credential Brokerはv1.1から変更しない。GA4 OAuth TokenをBigQueryへ使用しない。

# 8. Analysis requirements

次のv1.1要件は変更せずMUSTとする。

- Chat historyとは独立したAnalysis State
- Global → Company → Data Source SetのSemantic override
- GA4／BigQuery Query Router
- Canonical JSON hashによるDataset Cache
- Firestore metadata＋Cloud Storage result blob
- MoM、YoY、CVR、Difference、Ratio、Ranking、Contributionのコード計算
- 全主要FindingからEvidenceへ追跡
- rule-first Visualization SpecificationとECharts描画
- Planner → Gateway → Cache → Finding／Evidence → SSE
- 最大3,000行、小さな集計結果だけを外側LLMへ渡す
- Structured LoggingとApplication Audit

# 9. Audit

GA4 callはuser_id、company_id、data_source_set_id、session_id、auth_mode、Google subjectまたはService Account email、granted scope、Property、MCP tool、Query hash、cache、latency、error、Evidence IDを記録する。Token、Client Secret、Authorization Code、raw prompt、large result rowは記録しない。

Audit Identity metadataはRepositoryの後読みではなく、実際にそのrequestで選択したCredential contextから取得し、並行requestで混線させない。

# 10. Feature gates

```text
ANALYTICS_AI_ENABLE_GA4_USER_OAUTH=false
ANALYTICS_AI_ENABLE_ADMIN_CONNECTION_CHECKS=false
ANALYTICS_AI_ENABLE_ANALYTICS_TOOLS=false
```

各gateは独立し、既定OFFとする。OAuth接続を有効にしてもMessage Analyticsを公開しない。実環境Acceptance完了前にAnalytics Tool gateを有効化しない。

# 11. Acceptance Tests

必須:

1. User A OAuthでUser Aの許可Propertyだけ成功する。
2. Google側で見えるPropertyでもApplication GrantがなければBackendで拒否する。
3. Property権限削除後の次回callが403となり、access statusが更新される。
4. User A／Bの同時callでCredential、Property、Audit Identityが混ざらない。
5. 明示的`SERVICE_ACCOUNT` modeが対象Propertyで成功し、Viewer削除後は失敗する。
6. USER_OAUTHでProperty accessがなく、Service Accountに権限があってもfallbackしない。
7. 別Google Account OAuth、state replay、Scope不足、KMS失敗を拒否しTokenを保存しない。
8. Disconnectで暗号文を削除しrevokeを試行する。
9. BigQuery Service Account／Query Guard／Dataset isolationの既存testが回帰しない。
10. Data Source Set negative test、Conversation、Cache、Semantic、Evidence testが通る。

# 12. Onboarding

標準GA4 onboarding:

```text
Firebase Google Login
→ 同じGoogle AccountでGA4 OAuth consent
→ User-visible Property discovery
→ AdminがData Source SetへProperty／Streamを登録
→ User Access Grant
→ Connection／negative test
```

BigQuery onboarding:

```text
Data Access Service Account発行
→ Customer Dataset／ViewへData Viewer
→ Platform Query Project IAM
→ Data Source Set allowlist
→ Impersonation／dry-run／negative test
```

標準GA4 onboardingで顧客へPlatform Service AccountのProperty追加を依頼してはならない。

# 13. Production blockers

- Service Account Key配布
- Token／CredentialのFrontend保存
- Refresh TokenのFirestore平文保存
- UserへのToken Creator／KMS Decrypter付与
- Firebase TokenをGA4 delegated credentialとして誤用
- USER_OAUTHからService Accountへのfallback
- Global Credential cache共有
- Company所属だけのData Source access
- LLMによるProperty／Dataset自由選択
- Auditなし
- 実Identity／Property permission removal／concurrency test未実施

以上が解消され、GA4 USER_OAUTHとBigQuery Service Accountの両経路が一貫して動作した時点をMVP候補とする。
