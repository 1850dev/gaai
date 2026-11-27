<script lang="ts">
  import PublicPage from '#lib/components/PublicPage.svelte';
</script>

<PublicPage title="プライバシーポリシー" label="ANALYTICS AI">
  <p>
    このポリシーはAnalytics AIによるGoogleアカウント、Google Analytics 4（GA4）のデータ、
    および本サービスの利用に伴う情報の取り扱いを説明します。
  </p>
  <h2>1. 取得する情報と利用目的</h2>
  <ul>
    <li>
      <strong>Googleログイン：</strong
      >メールアドレス、表示名、GoogleおよびFirebaseのユーザー識別子を、
      本人確認、画面表示、組織・データソースごとの閲覧権限確認に使います。
    </li>
    <li>
      <strong>GA4接続：</strong>別途許可された閲覧専用スコープ
      <code>https://www.googleapis.com/auth/analytics.readonly</code>で、
      アカウント・プロパティの名称やID、ディメンション、指標とその集計値を取得します。
      接続候補の表示、対象プロパティの確認、分析・グラフ・根拠の表示に使います。GA4への書き込みは行いません。
    </li>
    <li>
      <strong>サービス内の情報：</strong
      >質問・サイト設定、分析条件・結果・会話履歴を分析と履歴の再表示に使います。
      リクエスト識別子、処理結果、時刻等の監査記録を、アクセス管理と障害・不正利用の調査に使います。
    </li>
  </ul>
  <h2>2. 保存と保護</h2>
  <p>
    接続情報・設定・履歴はCloud Firestoreに、集計結果のファイルは非公開のCloud Storageに保存します。
    OAuthリフレッシュトークンはCloud KMSで暗号化して保存し、ブラウザには渡しません。
    通信にはHTTPSを使用し、履歴にもデータソースへの閲覧権限を求めます。
    ログイン状態やダウンロードしたモデルの保持には、ブラウザの保存機能を利用します。
  </p>
  <p>
    接続情報はGA4との連携を維持するため、履歴と設定は再表示のため、監査記録は安全な運用のために保持します。
    これらには一律の保存期限を設けていません。削除を希望する場合は、下記の窓口にご連絡ください。
  </p>
  <h2>3. 処理先とAIの利用</h2>
  <p>
    ログイン、GA4からの取得、サービスの実行・保存・保護にFirebase AuthenticationとGoogle
    Cloudを使用します。 開発版のQwen推論は利用者の端末内で実行します。端末内分析の利用対象では、
    ログイン後に処理を準備し、未保存の場合はHugging Faceからモデルを取得しますが、
    モデルの配布元に質問やGA4の集計値は送信しません。実行を確定した質問・計画は分析実行と履歴保存のため本サービスのAPIに送信します。
  </p>
  <p>
    Google
    APIから取得した利用者データは、ここに記載した利用者向け機能とその安全な運用に必要な範囲で扱い、
    販売、広告ターゲティング、汎用AIモデルの開発・改善・学習には使いません。
    上記の処理の委託、法令上必要な場合など、Googleのポリシーで認められる場合を除き、第三者に提供しません。その利用・転送は、
    <a href="https://developers.google.com/terms/api-services-user-data-policy"
      >Google API Services User Data Policy</a
    >のLimited Use（使用制限）要件に従います。
  </p>
  <h2>4. 接続の解除とデータの削除依頼</h2>
  <p>
    「GA4接続・プロパティ」の「接続解除」を選ぶと、本サービスに保存した認証情報を取り除き、
    新たなGA4取得を停止します。Google側でも<a href="https://myaccount.google.com/connections"
      >Googleアカウントの接続管理</a
    >から許可を取り消せます。接続を解除しても、保存済みの分析結果や履歴は自動削除されません。
    セッションの一覧からの削除はアーカイブであり、保存データの完全な消去ではありません。
  </p>
  <p>
    保存済みデータの削除・訂正やお問い合わせは、Googleの同意画面に表示される Analytics
    AIの「ユーザーサポートメール」にご連絡ください。本人確認のために追加情報をお願いする場合があります。
    パスワード、認証コード、トークンは送らないでください。
  </p>
</PublicPage>
