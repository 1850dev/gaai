# Analysis state

Chat messages are presentation history. Analysis State is machine-readable continuation context.

State contains immutable source set, primary/comparison periods, multiple metrics/dimensions, typed semantic filters, time grain, selected Skill/version, legacy focus, dataset IDs, findings/evidence, semantic version, and router history. `dimension` is the grouping axis (for example `user_gender`, `session_channel_group`, or `device_category`). `channel` and `device` are active filter values (for example `Organic Search` and `mobile`), not grouping axes.

A reducer applies structured deltas. Missing values inherit; explicit filters narrow. Source-set changes require a new session. Visualization-only requests reuse datasets and do not call MCP.

```text
今月のCV → Organicだけ → モバイルだけ → 前月比較 → グラフにして
```

This sequence is an acceptance fixture for a Data Source Set with a conversion definition. The state retains the period/metric, adds channel/device, computes comparison dates in code, and marks the last request visualization-only with prior datasets.

The UI displays Dimension separately and labels Channel/Device as filters. Explicit registered aliases such as `Organic Search`, `自然検索`, `mobile`, and `モバイル` are detected deterministically after structured planning. The trusted compiler resolves them again through the scoped Semantic Layer before adding them to GA4 filters or Analysis State. An unregistered filter value remains fail-closed.

An information-site Data Source Set may explicitly mark conversion as unavailable. In that case the request returns deterministic guidance before data retrieval, does not create conversion state/dataset/evidence, and suggests registered alternative metrics. Its normal conversation sequence starts from an available metric such as active users, sessions, engagement rate, or page views.

Every completed analysis increments a Session revision. Firestore commits the revised Session, Assistant Message, and optional Finding in one optimistic transaction. A concurrent update fails with `analysis_session_conflict` instead of silently replacing Analysis State.

## Deterministic calculation facts

Each evidence-backed Finding may contain typed `calculations`. The application, not the LLM, calculates the selected value, difference, current/comparison ratio, change rate, category rank, and contribution. A comparison is labelled `mom`, `yoy`, or `custom` from the actual normalized periods; leap-year dates are clamped in code.

Contribution is emitted only when the resolved Semantic Definition explicitly declares `aggregation: sum`. New and disappeared categories are treated as zero on the missing side. A repeated category is not silently aggregated because it may hide an unmodelled dimension. Non-additive metrics such as Active Users may be ranked but are never summed for contribution.

CVR uses the same zero-safe ratio function, but its numerator and denominator must be declared by a versioned calculated Semantic Definition before the Orchestrator can emit it as a Finding. The LLM cannot select or invent those operands.
