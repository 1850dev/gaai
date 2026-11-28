# Google Analytics MCP

公式[Google Analytics MCP](https://developers.google.com/analytics/devguides/MCP)のTool contractを再利用し、`analytics-mcp==0.7.0`へpinする。すべてread-onlyである。

## Credential modes

Data Source Setごとに明示する。

| Mode | 用途 | Credential |
|---|---|---|
| `USER_OAUTH` | 標準 | Requesting Firebase UID自身が接続したGoogle OAuth |
| `SERVICE_ACCOUNT` | 特殊契約／内部運用 | Data Access Profile Service Account |

自動fallbackは禁止する。BigQueryのData Access ProfileをGA4 USER_OAUTHへ流用しない。

## OAuth connection

`POST /api/integrations/ga4/connect`がBackend Authorization Code Flowを開始する。`analytics.readonly`、openid/email/profile、offline access、incremental consent、PKCE、stateを使用する。CallbackはServer-sideで処理し、Firebase Google IdentityとOAuth Identityを一致させる。

Refresh TokenはCloud KMSで暗号化し、Firestoreには暗号文だけを保存する。`GET /status`、`GET /properties`、`POST /disconnect`を提供する。Disconnectは保存Tokenを削除しrevokeを試行する。

## Request-scoped adapter

UpstreamはADC credentialをmodule-globalにcacheする前提を持つため、Webマルチユーザーではそのまま使わない。Adapterはupstreamのcredential hookを`ContextVar` providerへ差し替え、Tool callの直前に選択したUserまたはService Account credentialを設定し、finallyで必ずresetする。Pin更新時はcontract testとUser A/B concurrency testを必須とする。

## Property discovery and execution

`get_account_summaries`はrequesting userのOAuth credentialだけで実行し、複数Account、ordinary Property、360 subproperty、roll-upを候補として返す。候補は自動でData Source SetやGrantにしない。

通常分析はGateway allowlistの`get_property_details`、`get_custom_dimensions_and_metrics`、`run_report`、`run_realtime_report`、`run_funnel_report`に限定する。Property IDとStream scopeはData Source SetからBackendが注入する。分析結果は最大3,000行に制限し、Cache、Evidence、Auditへ接続する。

## Access loss

Refresh Tokenが失効した場合はconnectionを`reauthorization_required`にする。GA4 APIがProperty access 403を返した場合はユーザー×Property statusを`denied`へ更新し、`ga4_property_access_denied`を返す。Service Accountへ切り替えて処理を継続してはいけない。

## Analysis Skills and Data API safeguards

PlannerはPropertyやTool名ではなくversion付きSkill IDとSemantic termを返す。Backend Registryが`run_report`、`run_realtime_report`、`run_funnel_report`のいずれか、Metric／Dimension数、row上限、cache可否、timeout budgetを決める。

Core reportは同じrequest-scoped OAuth Credentialで`checkCompatibility`を先に実行し、非互換Metric／DimensionであればData API report call前に拒否する。`return_property_quota=true`と返却metadataからquota、sampling、thresholding、`(other)` rowの影響をEvidence／Auditへ保存する。

### Result row policy / comparison

`rowCount`はページング前の総候補件数であり、実際に返された`rows`の長さとは異なる。空の配列がProtobuf JSONで省略されても総候補件数で代用しない。不正な配列型は拒否する。公式MCP `0.7.0`は`limit`をData APIへ渡すだけで、自動的に全ページ取得することはない。

2026-09-13の実GA4検証では、`limit=10`の2期間比較に対して対象10行＋比較10行が返り、以前の「全期間を合わせて10行以内」のチェックで誤拒否していた。現在は、Coreの2〜4期間について、返却ヘッダーの`dateRange`と各行の期間名を要求と照合し、**期間ごとに要求limit以内**と確認できる場合だけ、その行数を受け入れる。期間名不明・ヘッダー欠落・片期間の件数超過は拒否する。Realtime等にはこの扱いを適用しない。

**全期間合計の絶対上限3,000行は維持する。** Gatewayは取得前に要求limitを`3000 // 期間数`以下に抑え、Executorも返却総行数を検査する。Skillの`max_rows`はCoreでは1期間あたりの取得予算であり、全体上限とは別。期間数だけを見て無条件に超過を許可する変更ではない。

総候補件数が返却件数より大きい結果は取得失敗ではないが、全量を評価したともみなせない。Evidence／Auditの`quality.notes`に`result_row_limit_applied`を記録する。上位ページやチャネルだけの比較から、未取得カテゴリの値をゼロ、全体の寄与や重複除去済みUUを単純合計として推定してはならない。全体KPIはdimensionなしの別集計を使う。

件数超過時もguardを解除したりraw結果をAIへ渡したりしない。Auditには要求limit、実返却件数、実効上限、期間数とquery hashを記録し、結果行・Tokenは記録しない。UIの`mcp_result_row_limit_exceeded`だけでは、どの条件が超過したか／GA4側のQuotaかを断定できない。

仕様参照: [runReportのlimit](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/runReport)、[ページングと複数期間](https://developers.google.com/analytics/devguides/reporting/data/v1/basics)。

`GET /api/sessions/{session_id}/ga4/custom-metadata`はCompany Admin／Platform Adminだけが使用できる。現在のSessionとGrantをGatewayで再認可し、Property固有custom dimension／metricを「承認候補」として返す。このAPIはSemantic Definitionを自動作成・有効化しない。
