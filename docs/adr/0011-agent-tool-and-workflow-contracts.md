# ADR 0011 — Agent ToolとWorkflow Skillの契約

日付: 2026-09-13 / 状態: Accepted（段階実装。実環境Acceptanceは別途）

## 背景

ADR 0010のbounded loopと個別GA4 AnalysisSkillは、認可済み集計の取得・保存を可能にした。一方、実Geminiで空の初期計画が観測され、「5月と6月の流入元比較」のような依頼でも汎用の停止表示で終了する報告がある。行動選択・GA4引数・施策提案を一つの大きなSchemaで生成し、任意比較期間を直接表せないこと、目的の達成条件と復旧経路が弱いことを改善する。

半期regex seedと固定4観点agendaは限定的な復旧策であり、任意の質問に対応するAgentの完成ではない。ユーザーの明示Dimensionを一般的な観点へ置換する方式は、特定領域の深掘りにも適さない。例外をすべて`agent_interrupted`と表示する方式では、計画不備・外部API失敗・ユーザー停止を判別できない。報告された停止の具体的原因は、表示文だけで断定しない。

## 決定

### 1. 行動選択とTool引数生成を分離する

Agentは目的・会話・サイト制約・取得済みEvidenceから、確認質問、GA4調査、根拠に基づく回答を選ぶ。GA4は一つのApplication Capabilityであり、必ず実行する主役ではない。GA4を選択した場合だけ、GA4専用の小さい型付きSchemaでSemantic引数を生成する。

GA4の引数は既存AnalysisSkill Registry、Compiler、Authorization Guard、MCP Gatewayで検証する。Property、Stream、Credential、SQL、任意MCP Toolは生成Schemaへ含めない。最終回答は既存の根拠参照専用Schemaとコードによる数値差込みを利用する。

### 2. Workflow SkillとGA4 AnalysisSkillを分ける

- Workflow Skill: 目的、適用条件、必要情報、明示条件の保持、調査観点、達成条件、観測後の分岐、回答・質問・部分失敗時の方針。
- GA4 AnalysisSkill: 具体的な集計入力、互換性、Tool、行数・時間・回数上限、State更新、計算、Evidenceと可視化の契約。

Workflow Skillはサーバー管理・version付きとし、権限を付与しない。説明文の指示だけに安全性を依存せず、入力と実行可能Capabilityをコード検証する。登録情報はPlannerへ提供するが、宣言した達成条件を持つことと全条件の自動検証が完成したことを区別する。

まず流入比較を一つのVertical Sliceとして検証し、期間評価・深掘り・施策相談へ展開する。2026-09-13の追加実装は集客・ページ・属性／地域・環境・イベント・時系列を共通Capabilityで記述し、明示Metric／Dimension／時間粒度の検査をWorkflow共通にする。同じFieldが別々に存在するだけでなく、要求された集計軸と指標の組合せで根拠があるかを確認する。新しいMCPや外部操作を自由に追加する設計にはしない。BigQueryは保留を維持する。

### 3. 明示意図を保存する

対象期間と任意比較期間を別々に保持し、比較方向・日数差・指標補完の仮定を明示する。明示Metric／Dimension／Filterを一般テンプレートで上書きしない。指標が未指定の場合だけ、Workflowが必要とする承認済み指標を使用する。情報サイトに未定義CVを作らない。

期間・比較・粒度・FilterはCompilerからState、Cache、Evidenceまで一貫して渡す。アクティブユーザーを期間・カテゴリ跨ぎで合算せず、比較カテゴリの欠落をゼロと見なさない。

### 4. 観測・実行trace・復旧を明示する

Workspaceへ行動と実行結果、失敗理由、Evidence参照を記録する。GA4取得後は観測済み結果と未達成の観点を使って、追加取得・確認・回答を再判断する。固定した観点の列挙だけを汎用の計画能力と呼ばない。

計画検証失敗、Planner SDK失敗、GA4失敗、アプリ時間予算、ユーザー停止、通信切断を区別する。追加取得できない場合も、再認可できた取得済みFindingから確認済み事実・不足・次の選択を返す。認可失敗は回復可能な分析エラーとして継続しない。Token・raw prompt・大きな結果・SDK例外本文をログへ流さず、安全な分類だけを残す。

既存の取得・時間上限は維持する。二段階生成を追加しても全体予算を無制限に増やさない。実行traceの保存は汎用Operation ledgerやexactly-onceを保証しない。再送・再開の完全な契約はP3として引き続き実装する。

Coreの複数期間については、実GA4で期間別に最大limit行が返るケースを確認した。期間ラベルと各期間の件数を検証し、全体3,000行を取得前後で強制する。単純に全期間の行数を1期間のlimitと比べる誤拒否を修正するが、無条件の上限拡張にはしない。詳細は[GA4 MCPの件数方針](../ga4-mcp.md#result-row-policy--comparison)を参照。

## 維持する境界

- v1.2、ADR 0009のFirebase Identity、GA4 delegated OAuth、明示SA mode、DSS／Grant／Streamの境界を変更しない。失敗時のCredential fallbackを追加しない。
- v1.2との差異である候補からのDSS／Grant自動作成等は、別のP0 release blockerのまま残す。
- レポートartifactはユーザーの明示指示または有効化済み定期レポートの場合のみ。分析回答を自動でレポートへ変えない。
- Cloud Storage新規作成は保留。local限定のDocker共有volume＋既存Firestoreを維持する。クラウド作成や費用発生を本ADRで承認しない。
- 外部サイトの変更・施策実施・広告操作・通知送信をAgentへ許可しない。

## Acceptance

### 2026-09-14追記: 会話継承と段階別予算

- GA4引数の省略Metricは、LLMの自由補完ではなく現在の認可済みAnalysis Stateから検証前に継承する。Semantic解決・Gateway認可は従来どおり必須。
- 計画入力は保存履歴からのbounded projectionとする。現在の質問全文・条件・確認質問・観測値を優先し、過去のFinding全文を毎回Stateへ重複挿入しない。
- 行動選択30秒／引数生成60秒／提案生成60秒。段階ごとの1回のSchema修復は同じ絶対deadline内で実行し、成功済みの段階は再実行しない。調査240秒・turn300秒は引き続き外側の上限。
- 補助レビューの結果と会話の主条件を区別する。主計画のCompiler検証済みdeltaから条件を保存し、レビュー完了時に最新Stateへ条件だけ戻す。Dataset・Finding・revision・認可境界は巻き戻さない。
- 既存結果の説明は、明示的な再利用依頼・同一期間／比較／Filter・再認可済みFindingに限り過去sectionを完了判定へ使用する。通常の新規分析を過去の全sectionの和集合で完了させない。
- 数値主張とEvidenceの検証は緩和しない。修復済みの一時的形式エラーは診断履歴に残すが、最終回答の未解決エラーとしては表示しない。

合成回帰test、実Geminiの計画品質、実GA4結果の突合を分ける。

- 明示2期間・逆順の期間・非連続期間・指標未指定の流入元分析・明示UUを正しく解釈する。
- 会話のFilterを保持し、ユーザーの明示解除だけ反映する。明示分析軸を一般agendaへ置換しない。
- 必要な取得と再利用を区別し、根拠が足りる相談ではGA4を実行しない。
- Planner失敗や一部GA4失敗でも取得済み根拠と未確認事項を回答する。API利用上限とアプリ予算を区別する。
- 失効したGrantや他ユーザーのEvidenceは使用せず、レポートを無断作成しない。
- 保存した実行traceを再読込できる。完全なOperation再実行は別途検証する。

進捗は[todo.md](../todo.md)、詳細契約は[GA4分析Skill設計](../ga4-analysis-skills.md)、利用手順は[Agent Harness](../agent-harness.md)で管理する。
