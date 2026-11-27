<script lang="ts">
  import { resolve } from '$app/paths';
  import PublicPage from '#lib/components/PublicPage.svelte';
</script>

<PublicPage title="Analytics AI" label="GOOGLE ANALYTICS 4 向け会話型分析">
  <p>
    Analytics AIは、Webサイト担当者が自身のGoogle Analytics
    4（GA4）のデータを日本語で分析するサービスです。
    「先月の日別ページビューをグラフにして」といった質問から、期間・指標を選び、
    集計結果、グラフ、数値の根拠を確認できます。ページ別ランキングや期間比較にも対応します。
  </p>
  <h2>GoogleアカウントとGA4の接続</h2>
  <p>
    Googleログインは本人確認に使います。GA4にアクセスするには、その後「Google Analyticsを接続」で
    閲覧専用の権限を別途許可します。分析対象は、Google側の閲覧範囲のうち、本サービスに登録され、
    ご自身に閲覧権限が付与されたデータソースに限られます。GA4の情報は変更しません。
  </p>
  <h2>分析の流れ</h2>
  <p>
    開発版ではブラウザ内のQwenが分析計画を提案し、確認した条件でサーバーがGA4のデータを取得します。
    集計結果は履歴から再表示できます。データの取り扱いは
    <a href={resolve('/privacy')}>プライバシーポリシー</a>をご確認ください。
  </p>
  <p><a href={resolve('/')}>分析画面を開く</a> · <a href={resolve('/terms')}>利用規約</a></p>
  <p><small>Analytics AIは独立したサービスであり、Googleが運営するものではありません。</small></p>
</PublicPage>
