# 企業オンボーディング

GA4とBigQueryは認証モデルが異なる。標準GA4オンボーディングで、顧客へ「Platform Service Accountを全Propertyへ追加してください」と依頼してはいけない。

## 1. CompanyとUserを登録

特定の会社のメールドメインやProperty IDをアプリの利用条件にしていない。会社ごとにCompanyを分け、Firebase Googleログインで確認したUIDをUserとして登録し、対象CompanyのMembershipを付与する。Membershipだけではデータを許可しない。

開発Pilotでは、プラットフォーム運用者が[監査付きの開発用bootstrap](local-development.md)でCompany・User・Membershipを作成する。CompanyやUserのセルフ登録機能・本番用の管理UIはない。会社のGA4管理担当者をCompany Admin、分析担当者をAnalystとし、各人を明示的に登録する。新規会社の管理者は自社のSetのみ管理でき、別会社のGrantを付与できない。

別組織の実ユーザーを招待する前に、GA4 OAuthクライアントのAudienceを確認する。External / Testingなら対象をTest usersに登録し、GA4の認可が7日で失効することを前提にPilotを行う。一般利用に移す際はExternal / Productionへの切り替えとGoogleが要求する審査を完了する。Firebaseログインの承認済みドメインとGA4 OAuthの許可リダイレクトURIは別々に設定する。[GoogleのAudience設定](https://support.google.com/cloud/answer/15549945?hl=en)

## 2. GA4標準接続（USER_OAUTH）

1. エンドユーザーがFirebase Google Loginする。
2. 画面の「Google Analyticsを接続」を押す。
3. 同じGoogle Accountで`analytics.readonly`を許可する。
4. BackendがGoogle subject/email一致を検証する。
5. ユーザー本人が閲覧できるProperty候補を取得する。
6. Company Adminが対象Property ID、数値のStream IDとplatform、timezone、currencyを確認する。
7. `ga4.auth_mode=USER_OAUTH`として管理用draftへ登録し、管理者本人のOAuthでexact metadataを検証する。
8. 検証した管理者が同じrevisionを有効化する。この時点ではUser Access Grantを作らない。
9. 別操作で対象ユーザーへアプリ内Grantを明示付与し、そのユーザー自身のGoogle側権限と併せてpositive／negative testを行う。

登録・検証・明示Grantは[管理画面と管理APIの手順](admin-registration.md)を参照。既存Setへ自動適用しない。

Property discoveryで見えただけでは利用可能にしない。PropertyがData Source Setへ登録され、User Access Grantがあり、Google側でもそのユーザーが閲覧できる場合だけ許可する。Account ID、Google Tag ID（`G-...`）、Measurement IDをProperty IDとして使用しない。

## 3. GA4 Service Account mode（例外）

特殊な契約または内部運用で明示的に必要な場合だけ`auth_mode=SERVICE_ACCOUNT`を選ぶ。この場合に限り、対象Data Access Service Accountのメールアドレスを対象GA4 PropertyのProperty Access ManagementへViewerとして追加する。Account全体への付与を標準にしない。

USER_OAUTH失敗時のfallback目的でこのmodeを設定してはいけない。

## 4. BigQuery（将来追加予定、Pilotでは実施しない）

1. Platform側でCompany／境界ごとのData Access Service Accountをkeylessで作成する。
2. Customer側は必要なDatasetまたはAuthorized Viewだけに`roles/bigquery.dataViewer`を付与する。
3. Project全体のData Viewerを標準にしない。
4. Data Project ID、Dataset ID、許可Table／View patternをData Source Setへ登録する。
5. Platform Query Project側でProfile Service Accountへ`roles/bigquery.jobUser`と必要な`roles/mcp.toolUser`を付与する。
6. Runtime Service Accountへ対象Profile限定のToken Creatorを付与する。End Userには付与しない。

## 5. Semanticと有効化

Timezone、Currency、KPI、CV／Key Event、Revenue、Channel、BigQuery logical fieldを確認する。情報サイトでCVが存在しない場合は`available=false`とし、AppのKey EventをWeb CVへ流用しない。接続検証、positive/negative Grant test、Audit確認が完了するまでData Source SetとAnalytics Tool gateを有効化しない。

## Checklist

- [ ] Firebase UserとGA4 OAuth Google Accountが一致する。
- [ ] OAuth Scopeは`analytics.readonly`である。
- [ ] GA4 Property IDを登録した。
- [ ] 数値Stream IDとplatformを明示し、正確なPropertyのmetadataと一致を検証した。
- [ ] Data Source Setごとの`ga4.auth_mode`を明示した。
- [ ] BigQuery利用時だけData Access ProfileとService Accountを登録した。
- [ ] BigQuery Data Viewerを正確なDataset／Viewへ付与した。
- [ ] Project、Dataset、Table／View allowlistを登録した。
- [ ] User Access Grantのpositive/negative testが通った。
- [ ] Token、Secret、Service Account KeyがBrowser／Repository／Logに存在しない。
