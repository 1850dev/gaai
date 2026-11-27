export type IdentitySummary = {
  analysis_provider?: 'browser_qwen';
  uid: string;
  email: string | null;
  can_manage_data_sources?: boolean;
  browser_agent_available?: boolean;
};

export type SourceSetSummary = {
  id: string;
  company_id: string;
  name: string;
  has_ga4: boolean;
  has_bigquery: boolean;
  ga4_auth_mode: 'USER_OAUTH' | 'SERVICE_ACCOUNT' | null;
  ga4_property_id: string | null;
};

export type Ga4ConnectionSummary = {
  status: 'connected' | 'disconnected' | 'reauthorization_required';
  google_email: string | null;
  granted_scopes: string[];
  connected_at: string | null;
  last_verified_at: string | null;
};

export type Ga4PropertyCandidate = {
  account_id: string;
  account_display_name: string;
  property_id: string;
  property_display_name: string;
  property_type: 'PROPERTY_TYPE_ORDINARY' | 'PROPERTY_TYPE_SUBPROPERTY' | 'PROPERTY_TYPE_ROLLUP';
  parent: string;
  can_edit: boolean;
};

export type Focus = {
  metric: string | null;
  dimension: string | null;
  channel: string | null;
  device: string | null;
};

export type AnalysisState = {
  data_source_set_id: string;
  period: { start: string; end: string } | null;
  comparison_period: { start: string; end: string } | null;
  focus: Focus;
  metrics: string[];
  dimensions: string[];
  filters: Array<{
    semantic_name: string;
    kind: string;
    field_name: string;
    value: string;
    operator: 'exact';
  }>;
  time_grain: string | null;
  selected_skill: string | null;
  skill_version: string | null;
  dataset_ids: string[];
  findings: Array<{
    id: string;
    text: string;
    evidence_ids: string[];
    confidence: number;
    assumptions: string[];
  }>;
  semantic_version: string;
  router_history: string[];
};

export type AnalysisSession = {
  id: string;
  title: string;
  user_id: string;
  company_id: string;
  data_source_set_id: string;
  data_access_profile_id: string | null;
  state: AnalysisState;
  status: 'active' | 'archived';
  revision: number;
  created_at: string;
  updated_at: string;
};

export type ChartType = 'kpi' | 'line' | 'bar' | 'donut' | 'table';

export type VisualizationSpec = {
  type: ChartType;
  title: string;
  subtitle?: string | null;
  dataset_ids: string[];
  evidence_ids: string[];
  x?: string | null;
  category?: string | null;
  dimensions?: string[];
  series: Array<{ field: string; label: string; unit?: string | null }>;
};

export type DatasetRow = Record<string, string | number | boolean | null>;

export type AnalysisMessage = {
  content_format?: 'plain' | 'markdown';
  kind?: 'chat' | 'agent_execution';
  id: string;
  request_id: string;
  session_id: string;
  user_id: string;
  company_id: string;
  data_source_set_id: string;
  role: 'user' | 'assistant';
  status: 'completed' | 'error';
  content: string;
  dataset_ids: string[];
  evidence_ids: string[];
  finding_ids: string[];
  visualization: VisualizationSpec | null;
  result_references?: { session_id: string; message_id: string }[];
  error_code: string | null;
  created_at: string;
};

export type AnalysisFinding = {
  id: string;
  session_id: string;
  user_id: string;
  company_id: string;
  data_source_set_id: string;
  dataset_id: string;
  query_hash: string;
  evidence_ids: string[];
  semantic_version: string;
  skill_id: string | null;
  skill_version: string | null;
  compiler_version: string | null;
  text: string;
  metric_field: string;
  metric_label: string;
  dimension_field: string | null;
  dimension_kind: string | null;
  value: string | null;
  comparison_value: string | null;
  change_rate: string | null;
  comparison_kind: 'mom' | 'yoy' | 'custom' | null;
  category_comparisons?: Array<{
    dimensions: Record<string, string>;
    current_value: string | null;
    comparison_value: string | null;
    difference: string | null;
    change_rate: string | null;
    status: 'matched' | 'current_only' | 'comparison_only' | 'ambiguous';
  }>;
  calculations: Array<{
    kind:
      | 'value'
      | 'difference'
      | 'ratio'
      | 'change_rate'
      | 'rank'
      | 'contribution'
      | 'period_total'
      | 'period_average'
      | 'latest_value'
      | 'anomaly_score';
    value: string;
    category: string | null;
    rank: number | null;
    numerator: string | null;
    denominator: string | null;
    comparison_kind: 'mom' | 'yoy' | 'custom' | null;
    unit: string | null;
  }>;
  assumptions: string[];
  created_at: string;
};

export type DatasetEvidence = {
  id: string;
  company_id: string;
  data_source_set_id: string;
  dataset_id: string;
  source: string;
  semantic_version: string;
  skill_id: string | null;
  skill_version: string | null;
  compiler_version: string | null;
  mcp_server: string | null;
  tool_name: string | null;
  ga4_property_id: string | null;
  bigquery_project_id: string | null;
  query_hash: string;
  query_id: string | null;
  bytes_processed: number | null;
  rows: number;
  cache_hit: boolean;
  latency_ms: number | null;
  quality: {
    sampled: boolean;
    subject_to_thresholding: boolean;
    data_loss_from_other_row: boolean;
    empty_reason: string | null;
    time_zone: string | null;
    currency_code: string | null;
    notes: string[];
  };
  property_quota: Record<string, number>;
  created_at: string;
};

export type AnalysisResponse = {
  request_id: string;
  session: AnalysisSession;
  user_message: AnalysisMessage;
  assistant_message: AnalysisMessage;
  findings: AnalysisFinding[];
  evidence: DatasetEvidence[];
  visualization: VisualizationSpec | null;
  rows: DatasetRow[];
  cache_hit: boolean;
};

export type AnalysisHistoryResult = {
  message_id: string;
  parent_message_id?: string | null;
  source_session_id?: string | null;
  source_message_id?: string | null;
  findings: AnalysisFinding[];
  evidence: DatasetEvidence[];
  visualization: VisualizationSpec;
  rows: DatasetRow[];
  cache_hit: boolean;
};

export type SessionHistoryResponse = {
  messages: AnalysisMessage[];
  results: AnalysisHistoryResult[];
  latest_result: AnalysisHistoryResult | null;
};

export type AnalysisStreamEvent =
  | { event: 'status'; data: { stage: string; request_id: string } }
  | { event: 'finding'; data: AnalysisFinding }
  | { event: 'evidence'; data: DatasetEvidence }
  | { event: 'visualization'; data: VisualizationSpec }
  | { event: 'done'; data: AnalysisResponse }
  | { event: 'error'; data: { code: string } };
