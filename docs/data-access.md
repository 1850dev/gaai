# Data access model

## Identityの分離

```text
Human/Application Identity
  Google Login → Firebase Authentication → Firebase UID

GA4 data identity (default)
  Firebase UID → same Google account OAuth → analytics.readonly

GA4 data identity (explicit optional mode)
  Data Source Set → Data Access Profile → Service Account

BigQuery data identity
  Data Source Set → Data Access Profile → Service Account
```

`DataSourceSet`が分析とSessionの境界である。Company Membershipは組織上の所属にすぎず、data authorizationではない。

## Required records

- `users/{uid}`: Firebase UIDに対応するApplication User。
- `companies/{companyId}` / `members`: 組織Roleとstatus。
- `dataSourceSets/{id}`: GA4 Property／Stream、`ga4.auth_mode`、BigQuery allowlist、Semantic version。
- `userAccessGrants/{id}`: UserからData Source Setへの明示Grant。
- `ga4UserConnections/{uid}`: Google subject/email、Scope、KMS暗号文、status。Frontend read/write禁止。
- `ga4OAuthTransactions/{stateHash}`: one-use state transaction。TTL対象。
- `ga4PropertyAccess/{uid}_{propertyId}`: 最終access status。
- `dataAccessProfiles/{id}`: BigQueryおよび明示的GA4 Service Account modeのIdentityとTool policy。
- `analysisSessions/{id}`: User、Company、Data Source Set、Profile参照、Analysis State revision。

## GA4 USER_OAUTH

Property discoveryで見えることはData Source Set利用許可ではない。ユーザーがGoogle側で閲覧できるPropertyのうち、管理者がData Source Setに登録し、そのUIDにGrantを作ったものだけを分析できる。Session中にPropertyを切り替えず、MCP argumentsのProperty IDはBackendが登録値で上書きする。

Data Source SetがBigQuery Profileも参照していても、GA4 USER_OAUTH呼び出しはそのService Accountを選択・検証・fallbackに使用しない。

## BigQuery／GA4 SERVICE_ACCOUNT

Runtime Service AccountはProfileごとのService Accountだけをimpersonateする。BigQuery Data ViewerはCustomer Project全体ではなくDatasetまたはAuthorized Viewへ付与する。Query Projectの`roles/bigquery.jobUser`とRemote MCPに必要な`roles/mcp.toolUser`はPlatform Project側でProfile Service Accountへ付与する。

GA4でService Accountが必要な特殊契約だけ、Data Source Setへ`auth_mode=SERVICE_ACCOUNT`を明示し、対象PropertyにViewerを付与する。これは標準オンボーディングではない。

## Stream scope

GA4 PropertyにWebとAppが混在する場合、数値のStream IDとplatformを登録する。Google Tag ID（`G-...`）は収集設定でありreporting scopeには使用しない。登録済みStream filterはCaller filterとANDし、Callerは狭めることしかできない。
