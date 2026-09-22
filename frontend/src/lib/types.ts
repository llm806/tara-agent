export type DatasetStatus = {
  key: string;
  filename: string;
  ready: boolean;
};

export type HealthResponse = {
  status: "ok" | "degraded";
  data_ready: boolean;
  agent_ready: boolean;
  model: string;
  datasets: DatasetStatus[];
};

export type QuestionSuggestion = {
  id: string;
  category: string;
  question: string;
};

export type QuestionSuggestionListResponse = {
  items: QuestionSuggestion[];
};

export type AuthUser = {
  id: string;
  email: string;
  display_name: string;
  is_guest: boolean;
};

export type AuthResponse = {
  user: AuthUser;
  expires_at: string;
};

export type AgentStep = {
  stage: "route" | "understand" | "execute" | "answer" | "respond";
  title: string;
  detail: string;
};

export type RouteDecision = {
  kind: "direct_answer" | "analysis" | "clarify" | "unsupported";
  rationale: string;
  response?: string | null;
  capability_requirements: string[];
  analysis_reference_ids: string[];
};

export type ResultWarning = {
  code: string;
  message: string;
  details: Record<string, unknown>;
};

export type ChartSpec = {
  kind: "sample_map" | "bar" | "scatter";
  title: string;
  x: Array<string | number>;
  y: number[];
  labels: string[];
  x_label: string;
  y_label: string;
};

export type AgentResponse = {
  question: string;
  reasoning: string;
  answer: string;
  model: string;
  route?: RouteDecision | null;
  tool?: {
    name: string;
    arguments: Record<string, unknown>;
    summary: string;
  } | null;
  steps: AgentStep[];
  result: Record<string, unknown>;
  charts: ChartSpec[];
  warnings: ResultWarning[];
  sources: string[];
  session_id?: string;
  trace_id?: string;
  message_id?: string;
};

export type AgentStreamEvent = {
  event: "run_started" | "step" | "reasoning_delta" | "answer_delta" | "complete" | "error";
  step?: AgentStep;
  delta?: string;
  response?: AgentResponse;
  error?: string;
  session_id?: string;
  trace_id?: string;
};

export type PageInfo = {
  limit: number;
  offset: number;
  total: number;
};

export type SessionSummary = {
  id: string;
  title: string | null;
  status: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
  pinned_at: string | null;
};

export type ChatMessage = {
  id: string;
  session_id: string;
  trace_id: string | null;
  parent_message_id: string | null;
  sequence_no: number;
  role: string;
  message_type: string;
  status: string;
  content: string;
  content_parts: Array<Record<string, unknown>>;
  metadata: Record<string, unknown>;
  agent_response: AgentResponse | null;
  created_at: string;
  updated_at: string;
};

export type SessionListResponse = {
  items: SessionSummary[];
  page: PageInfo;
};

export type SessionDetail = SessionSummary & {
  messages: ChatMessage[];
};

export type SessionDeleteResponse = {
  deleted_ids: string[];
  deleted_count: number;
};

export type DataSource = {
  filename?: string;
  [key: string]: unknown;
};

export type TraceSummary = {
  id: string;
  session_id: string;
  question: string | null;
  correlation_id: string | null;
  workflow_name: string;
  workflow_version: string | null;
  status: string;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  retry_count: number;
  model_provider: string | null;
  model_name: string | null;
  markers: string[];
  sample_count: number | null;
  data_sources: DataSource[];
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
};

export type TraceSpan = {
  id: string;
  trace_id: string;
  parent_span_id: string | null;
  sequence_no: number;
  name: string;
  span_kind: string;
  status: string;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  retry_count: number;
  input_data: Record<string, unknown> | null;
  output_data: Record<string, unknown> | null;
  error_code: string | null;
  error_message: string | null;
  error_data: Record<string, unknown> | null;
  model_provider: string | null;
  model_name: string | null;
  model_parameters: Record<string, unknown>;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  context_length: number | null;
  tool_name: string | null;
  filters: Record<string, unknown>;
  marker: string | null;
  sample_count: number | null;
  sample_ids: string[];
  data_sources: DataSource[];
  artifact_refs: Array<Record<string, unknown>>;
  attributes: Record<string, unknown>;
};

export type TraceListResponse = {
  items: TraceSummary[];
  page: PageInfo;
};

export type TraceDetail = TraceSummary & {
  model_parameters: Record<string, unknown>;
  input_data: Record<string, unknown> | null;
  output_data: Record<string, unknown> | null;
  error_data: Record<string, unknown> | null;
  attributes: Record<string, unknown>;
  spans: TraceSpan[];
};
