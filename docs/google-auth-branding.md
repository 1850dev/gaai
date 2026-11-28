# Google Auth Platform — 外部公開の準備

Googleログインと、利用者が別途同意するGA4の閲覧専用アクセスを分ける。ブランド／OAuthの公開状態は、Analytics AIの会社・データソースへの閲覧権限とは別。OAuthを公開してもアプリ内の閲覧許可を自動付与しない。

## Branding に入力する内容

既存開発用Firebase Hostingに、この版を配備した場合のURL。正式な審査に提出する場合は、公開用プロジェクトと所有確認済みの公開ドメインを使用する。

| 項目 | 内容 |
|---|---|
| アプリ名 | Analytics AI |
| アプリのホームページ | `https://analytics.example.com/about`（実際の公開ドメインに置換） |
| プライバシーポリシー | `https://analytics.example.com/privacy` |
| 利用規約 | `https://analytics.example.com/terms` |
| ユーザーサポートメール | 運営者が継続して確認できる公開用窓口を選択する。 |
| デベロッパーの連絡先 | Googleの審査・変更通知を受け取る運営側の窓口を指定する。 |

## 公開前の確認

1. 運営者名と公開用サポート窓口を決め、プライバシーポリシーとコンソールで一致させる。削除・訂正依頼の受付と処理、履歴・バックアップ・監査記録の保持方針を運営者が確認する。現版は個人メールを掲載せず、同意画面のサポートメールを案内する。接続解除やセッションのアーカイブを、保存済みデータの完全削除とは表現しない。
2. 3ページを同じドメインに配備し、ログインなしで 200、ホームからポリシーへ移動できることを確認する。正式審査には、プロジェクトのオーナーがGoogle Search Consoleで所有権を確認したドメインを使用する。Googleの審査ガイドはDNSレベルのドメイン確認を案内しているため、既定のHostingサブドメインで足りるとは仮定しない。満たせない場合はDNSを管理できるカスタムドメインを用意し、OAuthクライアントの承認済みオリジン・コールバックのドメインも合わせて確認する。
3. Branding に入力・保存する。Data Accessで `https://www.googleapis.com/auth/analytics.readonly` の分類と審査要否を確認し、権限の用途を「利用者が閲覧でき、アプリでも登録・許可されたGA4データの集計と可視化」と説明する。書き込み権限は追加しない。
4. Audience の「本番環境に移行」と、Verification Centerでのブランド／スコープ審査は別の操作。変更前に公開先を確認し、必要な審査に提出する。審査用の操作動画やアクセス案内には、顧客のGA4プロパティ・トークン・実データを含めない。

公式: [Branding](https://support.google.com/cloud/answer/15549049)、[ドメイン確認](https://support.google.com/cloud/answer/13804266)、[審査要件](https://support.google.com/cloud/answer/13464321)、[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy)。
