# Security

## 絶対条件

- Firebase AuthenticationはApplication Identityであり、GA4 delegated OAuthとは分離する。
- GA4は標準でユーザー本人の`analytics.readonly` OAuthを使う。
- BigQueryはData Access Profile Service Accountをkeyless impersonationして使う。
- GA4の`USER_OAUTH`から`SERVICE_ACCOUNT`へ自動fallbackしない。
- Service Account Key JSONを作成・配布・commitしない。
- OAuth Refresh Token、Access Token、Authorization Code、Client SecretをFrontend、Firestore平文、Log、Auditへ出さない。
- End UserへToken Creator、KMS Decrypter、Secret Accessorを付与しない。
- Company所属だけでData Source Setを許可しない。
- Caller／LLMにProperty、Project、Dataset、Credential、MCP Toolを選ばせない。
- BigQueryはread-only、allowlist、dry-run budget、最大3,000行を必須にする。
- 数値計算をLLMへ任せず、FindingからEvidenceへ追跡可能にする。

## OAuth controls

Backend Authorization Code Flow、PKCE、一度限りのstate、短いstate TTL、exact redirect URIを使う。MVPではFirebase Google subject/emailとOAuth Identityを一致させる。必要Scopeが欠ける、stateが期限切れ、別Google Account、KMS暗号化失敗のいずれもCredentialを保存しない。

Refresh TokenはユーザーUIDをAdditional Authenticated DataとしてCloud KMSで暗号化する。Firestore Security RulesはBrowserから`ga4UserConnections`を含む全collectionのread/writeを拒否する。Disconnect時は暗号文を消し、可能な限りGoogle revokeを行う。

## Authorization

2026-09-29（S1a）: Property候補からの分析開始API `POST /api/ga4/sessions` は監査付きHTTP 410で廃止。候補表示ではDSS／Grant／Semantic／Sessionを作成しない。分析開始は `POST /api/sessions` の登録済み・enabled・Grant済みDSSだけを使用する。旧自動作成レコードの承認・移行は別作業で、履歴や既存Grantを一括書換えしていない。この修正だけでAdmin登録／Stream検証を含むS1全体の完了とはしない。

S1b: 新規Set用の管理登録APIを追加。管理用draftは公開Setと分離し、exact Property／Stream／platform／timezone／currency検証後に有効化する。登録／検証／有効化とGrantは別操作。管理変更と成功監査を同一transactionで保存し、transaction内でも管理者・Company所属・revisionを検証する。active登録のScope変更は許可しない。ブラウザのFirestore直接アクセス拒否と認証方式は変更せず、管理画面・旧登録の移行・実Identity受入は残す。[契約と限界](admin-registration.md)

GA4 `USER_OAUTH`は次を毎回検証する。

1. active Firebase User
2. active Company Membership
3. exact User Access Grant
4. enabled Data Source Set
5. Data Source Set登録済みProperty／Stream scope
6. requesting UID自身のconnected OAuth Credential
7. Google側のProperty access

BigQueryはさらにverified Data Access Profile、Profile Tool allowlist、Project／Dataset／Table allowlist、Service Account IAMを要求する。USER_OAUTH GA4の呼び出しにBigQuery Service Account metadataを持ち込まない。

## Credential isolation

GA4 Credentialはrequest-localに選択し、公式MCPのglobal ADC cacheを共有しない。並行するUser A／Bのcredential contextを分離する。Auditは実際に選択したcontextからGoogle subjectまたはService Account emailを記録するが、Tokenは記録しない。

## Error handling

- OAuth grant失効: `ga4_reauthorization_required`
- Google側Property権限なし: HTTP 403相当、`ga4_property_access_denied`
- Application Grantなし: generic `access_denied`
- USER_OAUTH未接続: `ga4_user_connection_required`

権限エラーは別Identityへのfallbackやretryを行わない。Property access結果はユーザー×Propertyで更新し、再接続またはGA4権限確認を案内する。

## Production blockers

2026-09-30: 承認済みの旧Setを同じIDでWeb限定へ移行した際、旧全Streamの履歴・作業記憶が混ざる経路を共通Session認可で閉じた。新規SessionはSource scope fingerprintへ固定する。fingerprintのない旧Sessionは、移行時の旧source baselineと現在Setが一致しなければ保存したままアクセスを拒否する。過去の広いScopeの数値を現在Scopeへ付け替えて公開しない。操作・停止・Grant移行は成功監査と同一transaction。合成回帰と実Repository読み取りを確認したが、実Google側失効・2Identity受入は未完。[範囲と実測](evaluation/s1c-r1-a1-release-2026-09-30.md)

実Firebase／OAuth／GA4／BigQueryを使い、User A/B並行分離、別Google Account拒否、Token revoke、Property権限剥奪403、未登録Property注入拒否、Dataset denial、KMS IAM、Secret Manager、Audit無漏洩を確認するまでProductionへ進めない。
