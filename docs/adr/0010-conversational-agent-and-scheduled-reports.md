# ADR 0010 — GA4会話エージェントと定期レポート

日付: 2026-09-10 / 状態: Accepted（実環境Acceptanceは別途）

2026-09-26追補: サイト設定による提案領域・制作可否の強制はユーザー訂正により[ADR 0013](0013-site-understanding-not-policy.md)で置換。サイト設定は理解の文脈であり、権限・禁止ポリシーではない。以下の当時の決定は履歴として残す。

## 背景

単発のIntentPlan→Reportでは、結果を観察して追加調査すること、事業制約を踏まえた施策相談、会話をまたぐレポート作成ができない。今回の明示要求に基づき、既存の認可・Semantic・MCP Gatewayを変えずに上位の実行制御を追加する。Source of Truthはv1.2とADR 0009。BigQueryはPendingを維持する。

## 決定

- Directorは`ask / query / finish`の構造化判断のみ返す。コード側が最大7判断・4分析・全体300秒・各判断45秒を管理する。調査は240秒までとし、予算到達時の追加取得を伴わない最終要約に最大45秒を確保する。停止理由・検証エラー・取得済みFindingを渡して回答させ、失敗時はコード側の説明へfallbackする。1分析内部のCompatibility／MCP call数は既存Skill Budgetに従うため、4分析は4 HTTP呼出しとは限らない。
- レポート表示・出力はユーザーの明示的な作成指示（または有効化した定期レポート）がある場合だけとする。通常の調査回答と区別し、内部queryをユーザー発言として記録しない。
- 全queryは既存Orchestrator→Compiler→Gatewayへ渡す。Property、Credential、Tool、SQLをDirectorへ選択させない。各stepで所有者・現在のGrantを再確認し、GA4は所有者のdelegated OAuth（明示SA modeの場合のみ既存のSA経路）を使用する。fallbackは追加しない。
- Workspaceは目的、直近会話、調査section、計算済みFindingの要約と参照、Evidence、改善仮説、確認事項を保存する。分析StateやChat Messageは置き換えない。GA4の再取得なしで確認質問や施策相談を行える。
- 各取得結果の先頭12行×最大6フィールド、直近4sectionだけを観測コンテキストに使う。長い文字列は送らない。ページ等への深掘りは観測済みdimension/valueの完全一致だけを承認し、サーバー側で一時Semantic filterへ変換する。解除できるのは分析filterだけで、Property／Stream scopeは解除できない。
- サイト設定は**Firebase UID×Data Source Setの個人設定**とする。同一Propertyでも他ユーザーへの影響を避けるため共有設定にしない。将来のCompany共有設定はAdmin権限・継承設計が必要。非表示はユーザー個人の表示設定であり、Grant／履歴／定期実行を変更しない。
- 改善案は未検証仮説と明示する。許可画面、新規作成・リライト可否、存在するEvidence、Semantic評価指標をコード検査する。自由記述制約の解釈や提案本文の意味まで機械的に保証はできないため、公開・実施は人が確認する。実サイトへの変更・広告操作・通知送信は行わない。
- 定期実行はユーザーが明示的に有効化する。Firebase Tokenの偽造やpublic cron APIは作らず、内部Workerがschedule ownerとして再認可する。新規Sessionへ前の完了週／月のReportを作る。OAuth失効は失敗として保存し再接続を要求する。
- Firestore CASでoccurrenceをclaimし、次回時刻を先に進める。重複dispatchは同じoccurrenceを再実行しない。worker強制終了はlease期限後に中断を記録し、同一回の自動再実行はしない（exactly-onceの保証ではない）。通常の一時停止は将来の実行に適用する。
- 開発はComposeの内部Worker（60秒poll）、本番構成はCloud Scheduler→IAM認証→Cloud Run Job（5分dispatch）。外部クラウドの作成・適用はこの変更では実施しない。
- 追加のユーザー判断によりCloud Storage新規作成を保留。localではFirestore metadata＋Docker named volumeのfilesystem blobを使う。ファイル名はscopeを含むblob名のSHA256、書込みは0600 temporary file→atomic replace。UIへvolumeを公開しない。`filesystem`はlocal/testかつFirestore metadata時だけ許可し、dev/stg/prodで拒否する。本番GCS構成は変更しない。
- 実Geminiでは複合Pydantic Schemaをそのまま渡すと400になったため、生成用JSON Schemaから長さ等の補助制約を除去する。返却後のPydantic検証・Gateway検証は省略しない。架空サイトによる実Gemini応答を確認済み。

## 制約と未完了

### 2026-09-12: 期間レビューと最終評価の分離

計画中の説明の年月・製品名まで拒否していたため、妥当なqueryの実行と数値を表示する最終回答の検証を分離する。最終回答はquery／toolフィールドを持たない小さなSchemaで生成し、計算済みFact IDへの参照を検証してコードで数値を差し込む。修正再生成は最大1回、時間・権限・取得上限は緩和しない。

1〜3指標の明示的な多角分析に、期間全体・時系列・チャネル・ページの最大4観点のサーバー管理agendaを導入する。期間・指標・比較・フィルタを引き継ぎ、各実行は既存CompilerとGatewayで再認可する。成功した観点だけを完了とし、失敗しても残りを評価する。その他の会話は従来のDirector loopを維持する。重複判定は意図文ではなく実効クエリ引数で行い、検証だけの失敗で取得枠を消費しない。

カテゴリ比較は同じ次元タプルでのみ計算し、欠落カテゴリをゼロと仮定しない。限定ランキングの寄与を全サイトの寄与と呼ばない。実GA4での多角的評価品質は別途Acceptance対象である。

全GA4 APIの無制限エージェント化はしない。利用可能範囲は承認済みSemantic／Skill内。自由なraw journey、未登録funnel、因果推論、売上予測や施策の自動実施は対象外。v1.2の既存P0（候補選択からDSS／Grant自動作成、Stream登録フロー）は残っており、正式なProduction releaseの前提にはできない。

## 参照

- [Cloud Run Jobsの定期実行](https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule)
- [Cloud Schedulerのat-least-once配信](https://docs.cloud.google.com/scheduler/docs/overview)
- [GA4 Data API Quota](https://developers.google.com/analytics/devguides/reporting/data/v1/quotas)
