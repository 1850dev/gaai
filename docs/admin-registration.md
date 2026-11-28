# S1b／S1c — 管理者によるGA4 Set登録・承認移行

更新日: 2026-09-30

## 対象と前提

新規GA4 Data Source Set用の管理APIと`/admin`管理画面。既存Setの棚卸し・個別承認移行・停止をS1cで追加した。未選択Set／Grant／Sessionは自動変更しない。BigQuery登録は保留のまま。

- Firebase ID Tokenで呼出者を認証する。active User、active Company、対象Companyのactive Membership、Company AdminまたはPlatform Adminを必須とする。Platform Adminにも他Companyへの暗黙アクセスを付けない（ADR 0008の管理者境界を継承）。
- `ANALYTICS_AI_ENABLE_ADMIN_CONNECTION_CHECKS=true` が管理APIのgate。既定OFF。Analytics Tool／OAuthのgateは独立し、この変更で自動的にONにしない。
- USER_OAUTHでは検証を実行する管理者自身の接続を使う。他ユーザーのCredentialの指定は不可。明示SERVICE_ACCOUNTのみ同じCompanyのready Profileを使い、自動fallbackしない。
- Google側のProperty ViewerやIAMは付与しない。ここでのGrantはアプリ内の閲覧許可だけであり、利用者本人のGoogle側権限も引き続き必要。

## 保存と状態遷移

`dataSourceRegistrations/{server-generated-id}` を管理用の保留記録として追加。Firestore Rulesの全ブラウザread/write拒否を維持する。

1. **draft**: 管理者の明示入力を保存。Property ID、1〜20件のnumeric Stream ID＋platform、timezone、currency、auth modeは省略不可。全Propertyの暗黙許可は作らない。
2. **verify**: 先に旧証跡を消してrevisionを進める。公式MCPの `get_property_details` でProperty／timezone／currencyを照合し、Google Admin SDKで登録した各StreamだけをGETし、Propertyの帰属とplatformを照合する。
3. **verified**: 登録内容のhash、検証者UID、実Credentialの非秘密Identity、検証時刻、SA利用時のProfile hashを保存する。検証中の編集・権限失効・競合はcommit時にも拒否する。
4. **active**: 同じ管理者が15分以内に検証済みの同じrevisionを有効化する。別管理者が担当する場合は自身のCredentialで再検証する。公開Setと標準Semantic定義と監査を同一transactionで作成。Grantは作らない。
5. **Grant**: 別APIで受領者UIDとenabledを明示する。enabled=trueには同じCompanyのactive User／Membershipが必要。roleはanalyst固定で、管理者ロールは付与しない。停止はenabled=false。無効化済みUserのGrantも停止可能。

各変更はrevisionによるcompare-and-set。古いrevisionは409。draft編集は証跡を無効化する。active SetのProperty／Stream／Company／auth mode変更は拒否する（過去Sessionの分析対象を書き換えない）。既存レコードの承認移行・有効Setの変更／無効化管理はS1cで扱う。

検証の上限は45秒、Stream GETは各5秒・自動retryなし。失敗・timeout・プロセス停止後はdraftのままであり、以前の成功証跡では有効化できない。GETで最新revisionを取り直して明示的に再検証する。分析処理の実行回数・期限を変える設定ではない。

## API

基点: `/api/admin/companies/{company_id}/data-source-registrations`

| Method／path | 操作 | Body |
|---|---|---|
| POST 基点 | draft作成 | 下の登録例 |
| GET 基点/{id} | 現状態・revision確認 | なし |
| PUT 基点/{id} | draft変更・検証破棄 | expected_revision, definition |
| POST 基点/{id}/verify | exact metadata検証 | expected_revision |
| POST 基点/{id}/activate | 検証済みSet有効化 | expected_revision |
| PUT 基点/{id}/grant | 明示Grant付与／停止 | expected_revision, user_id, enabled |

登録例（合成ID。実環境へこのまま送信しない）:

```json
{
  "name": "A社 Web",
  "ga4": {
    "property_id": "222222222",
    "display_name": "A社 Web",
    "auth_mode": "USER_OAUTH",
    "timezone": "Asia/Tokyo",
    "currency_code": "JPY",
    "stream_scopes": [
      {"stream_id": "3333333333", "platform": "web"}
    ]
  }
}
```

作成結果はrevision=1。verifyは「開始」と「成功」の2回更新するので成功時は通常revision=3。固定値を次の要求へ使わず、毎回応答のrevisionを使う。有効化後もSet一覧・分析開始に現れるのはGrant済みの利用者だけ。

不一致は自動修正しない。実Google metadataを管理者が確認してdraftを修正し、再検証する。標準Semanticテンプレートは `ga4-oauth-v1`。CV未定義はunavailableのまま作成し、会社固有CV／Revenueの設定を推測しない。

## 原子的な監査と衝突

Firestore transaction内で管理者・Company・Membership・Profile・対象revision・公開Setを読み直す。Grant付与時は受領者とそのMembershipも読み直す。成功監査が保存できなければ変更自体もcommitしない。拒否は別の拒否監査を記録する。Token・上流エラー本文をレスポンス／監査へ保存しない。

管理APIのGrant IDはuser×Setから一意生成。別IDの既存Grantや重複を検出した場合は409で拒否し、旧許可を黙って移行・上書きしない。revisionはGrant変更でも更新し、同じSetへの管理変更を直列化する。

## 検証の範囲

合成APIテスト、Google SDKの実protobuf型によるMetadata契約テスト、Firestore transactionを模した保存・復元・監査失敗テストを実施する。FirestoreテストはEmulator／実FirestoreのAcceptanceとは区別する。

2026-09-30にユーザー承認済みの4Setだけを実Firestoreで移行・停止した。管理画面の一連の流れ・analyst拒否は合成Firebase／APIによるE2E。実Firebase画面操作・Identity失効・並行分離・Google権限削除は未完。実移行成功でもS1全体のP0解除・Pilot公開可能とは扱わない。

Stream GETはanalytics.readonlyで利用可能な管理用metadata呼出し。Agent／LLMのTool allowlistには追加しない。[Google公式のData Stream GET仕様](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties.dataStreams/get)

## S1c — 管理画面と旧レコード移行

`/admin`ではAdmin所属Companyだけを表示し、公開Setと旧Grantを100件ずつ棚卸しする。対象と閲覧許可を確認したチェックを要求し、legacy登録・verify・activate・UID Grant・disableを別操作にする。画面の新規フォームはUSER_OAUTH・単一Stream対応。複数Streamや明示SA登録は管理APIを利用する。未登録draftはIDを保管してAPIから取得する必要がある。

- `GET /api/admin/companies/{company}/data-source-inventory?after=...`: 公開Setと登録状態・Grant・source/grants hash。Grant100件超では移行を拒否し、隠れたGrantを無視して承認しない。
- `POST /api/admin/companies/{company}/data-source-registrations/{id}/legacy`: 既存Setと同じID、Property・Company・auth mode・Profile・Semanticを保持。登録Streamへの限定だけを許可し、棚卸し時のsource/grants hashをtransactionでも検査する。
- 同じIDの`verify`→`activate`: 旧Grantを同じtransactionでenabled=falseにする。旧Grant記録・履歴・Evidence・Semantic・site設定は削除／上書きしない。利用者には別の明示Grantが必要。
- 同じIDの`disable`: 登録revisionと棚卸しhashを照合し、Setと旧Grantを停止する。disabledを自動で再有効化しない。

SUBPROPERTYの場合、公式Property metadataが返すnumeric `properties/{parent}`を使って、その親の登録Streamだけを同じCredentialで検証する。親PropertyのGA4分析・Grantは作らず、利用者のデータ取得は登録したサブPropertyのまま。親metadataの閲覧不可・stream帰属不一致では拒否し、自動SA fallbackしない。検証証跡にmetadata parentを保持する。[継承されるStreamの公式説明](https://support.google.com/analytics/answer/11525732?hl=en)

## 同じIDのScope限定と履歴

新規Session（API／schedule）はSource scope fingerprintを持ち、Property・Stream・auth・Profile等が変われば共通Session認可で拒否する。fingerprintを持たない旧Sessionは、承認移行の旧source baselineが現在Setと一致するときだけ互換利用する。不一致の旧Sessionを現在Scopeへ付け替えない。文章・作業記憶・根拠・Chartも同じ境界で拒否するため、検索や再描画だけで制限を迂回できない。

今回、以前全Streamだった札幌Setの27旧Sessionは保存したまま現在の履歴一覧から除外した。新しいWeb限定分析は新規Sessionで開始できる。もともとWeb限定のQ2b Setは旧baselineが一致するため16メッセージの履歴を維持した。削除ではないが、以前の広い履歴を今のGrantで読めることは保証しない。
