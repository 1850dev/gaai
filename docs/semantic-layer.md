# Semantic Layer

Semantic Definitionは`Global → Company → Data Source Set`の順に解決し、最も具体的なScopeの定義を採用します。AliasはNFKC正規化とcase foldingを行い、同一Scopeで解決結果が曖昧な場合は実行を拒否します。

定義にはCanonical Name、Alias、Kind、Unit、Aggregation、GA4 mapping、BigQuery field／macro、Visualization metadata、Versionを保持します。Rateはnumeratorとdenominatorを明示します。使用したVersionはQuery、Dataset、Evidence、Findingへ引き継ぎます。

```text
Global: UU → activeUsers
Company A: CV → key event
A Web: CV → generate_lead
A EC: CV → purchase
```

## CV／Key Eventは必須ではない

CVは、すべてのWebサイトに必ず存在するMetricではありません。情報サイトなど事業上のConversionを定義していないData Source Setでは、CVを0件として扱ったり、同じCompanyのApp Key Eventを流用したりしてはいけません。

利用不可の用語は、そのData Source Setに`available: false`の定義を置いて明示的に遮断します。これにより、将来Company／Global定義が追加されても意図せず継承しません。利用不可定義にはGA4／BigQuery mappingを設定できません。

```json
{
  "canonical_name": "conversion",
  "aliases": ["CV", "コンバージョン"],
  "kind": "metric",
  "scope": "data_source_set",
  "company_id": "example-company",
  "data_source_set_id": "example_web",
  "version": "example-web-v1",
  "available": false
}
```

この状態で「今月のCV」と質問された場合、Queryを実行せず、`このData Source SetではCVが定義されていません`と説明し、アクティブユーザー、セッション、エンゲージメント率、ページビューなど利用可能なKPIを提案します。User-facingなエラー変換はMessage Orchestrationで実装します。

Native Appの`in_app_purchase`や`app_store_subscription_renew`などはApp用Data Source Setだけに定義します。WebとAppが同じGA4 Propertyに含まれる場合、Property IDだけでは境界にならないため、登録済みの`platform`と`streamId`をBackendが固定注入します。LLMにStreamを選択させません。

Golden-set testでは、日本語Alias、略語、Company固有用語、Override、曖昧性に加え、利用不可用語とWeb／App間の非継承を検証します。LLMは用語を提案できますが、Resolverを迂回できません。

## 時系列粒度

期間と集計粒度は別の概念として扱います。たとえば「今月」は期間指定であり、「月別」「月ごと」「月次」は集計粒度の指定です。明示された粒度はLLM判断よりルールベースの解析を優先し、GA4 Data APIの公式Dimensionへ次のように解決します。

| ユーザー表現 | Canonical Name | GA4 Dimension | Cache grain |
|---|---|---|---|
| 日別、日ごと | `date` | `date` | `day` |
| 週別、週ごと、週次 | `year_week` | `yearWeek` | `week` |
| 月別、月ごと、月次 | `year_month` | `yearMonth` | `month` |
| 年別、年ごと、年次 | `year` | `year` | `year` |

粒度はAnalysis StateへCanonical Dimensionとして保存し、Cache Descriptorの`dimensions`と`grain`の両方へ含めます。このため、同じProperty・期間・Metricでも日別Datasetと月別Datasetは異なるQuery Hashとなり、相互にcache hitしません。
