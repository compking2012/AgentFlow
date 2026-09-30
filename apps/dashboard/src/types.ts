export type Entity = { id: string; revision: number; [key: string]: unknown };
export type Target = 'web' | 'api' | 'ios_native' | 'android_native' | 'windows_native' | 'macos_native' | 'linux_native';
export type ProductTarget = 'web' | 'api' | 'ios' | 'android' | 'windows' | 'macos' | 'linux';
export type ProductLanguage = 'zh-CN' | 'en';
export type Project = Entity & { name: string; local_path: string; base_commit: string; base_ref: string };
export type WorkItem = Entity & {
  step: string; role: string; status: string; quality_result: string; dependencies: string[];
  artifact_ids: string[]; generation: number; approval_required: boolean; output_fingerprint?: string;
  blocking_reason?: string; attempt_id?: string;
};
export type Run = Entity & {
  display_name?: string;
  run_id: string; plan_id: string; project_id: string; goal: string; execution_state: string;
  input_fingerprint: string;
  quality_result: string; purpose: string; blocking_reasons?: string[]; work_items?: WorkItem[];
  active_attempt_count?: number; pending_approval_count?: number; total_work_count?: number;
  completed_work_count?: number; budget_limit?: { currency: string; limit_micros: number; cost_mode?: string;
    max_model_requests?: number; max_active_seconds?: number; max_tool_calls?: number };
};
export type ExecutionBudgetKey = 'max_tool_calls' | 'max_active_seconds' | 'max_steps';
export type WorkExecutionBudget = {
  run_id: string; work_item_id: string; run_revision: number; work_revision: number; budget_revision: number | null;
  work_title?: string;
  work_status: string; run_state: string; metering: 'known' | 'unknown' | 'missing'; can_extend: boolean;
  adjustment_blockers: { code: string; message: string }[];
  dimensions: { key: ExecutionBudgetKey; label: string; unit: string; used: number | null; limit: number | null;
    remaining: number | null; balance: number | null; overrun: number | null; exhausted: boolean | null;
    usage_kind?: string }[];
};
export type ModelUncertainty = {
  run_id: string; work_item_id: string; work_title: string; attempt_id: string; invocation_id: string;
  run_revision: number; work_revision: number; attempt_revision: number; invocation_revision: number;
  attempt_budget_revision: number; expected_state_digest: string; state: string; cost_mode: string;
  usage: unknown; actual_micros: number | null; request_counted: boolean; eligible: boolean;
  blockers: { code: string; message: string }[]; acknowledged: boolean; acknowledgment_id: string | null;
  acknowledged_at?: string; requires_separate_retry: true;
};
export type ModelUncertainties = { run_id: string; run_revision: number; items: ModelUncertainty[] };
export type Approval = Entity & {
  run_id: string; work_item_id: string; fingerprint: string; generation?: number;
  stale: boolean; decision: 'approve' | 'reject' | null; reason?: string;
};
export type Profile = Entity & {
  model_profile_id?: string; name?: string; label?: string; provider?: string; protocols?: string[];
  requested_model_id?: string; requested_model?: string; accepted_api_model?: string;
  acceptance_status?: string; credential_status?: string; revision: number;
};
export type Backend = Entity & { backend_id?: string; name?: string; backend?: string; state?: string };
export type Meta = { version: string; agent_concurrency: number; trusted_project_execution: boolean;
  targets: Target[]; executor_configured: boolean };
export type ExecutionSettingField = {
  key: string; label: string; unit: string; integer: boolean; boolean?: boolean; minimum: number | null; maximum: number | null;
  exclusive_minimum: number | null; default_value: number | boolean; saved_value: number | boolean; loaded_value: number | boolean;
  source: 'configuration_file' | 'application_default'; restart_required: boolean; restart_on_change: true;
  effect_scope: string; description: string; zero_meaning: string | null; group: 'common' | 'advanced';
};
export type ExecutionSettings = {
  configuration_path: string; configuration_revision: string; restart_required: boolean;
  saved_values: Record<string, number | boolean>; loaded_values: Record<string, number | boolean>; fields: ExecutionSettingField[];
  current_runs_changed: false; operation_id?: string; saved_at?: string;
  model_parameters: { source: 'model_configuration'; description: string;
    roles: { model: string; max_output_tokens: number; source: 'models.roles'; configured: boolean };
    coding: { model: string; max_output_tokens: number; source: 'models.coding'; configured: boolean } };
};
export type MissingInput = { code: string; step?: string; message: string };
export type Plan = Entity & {
  plan_id: string; actual_steps: string[]; state: string; missing_inputs: MissingInput[];
  base_commit: string; app_targets: Target[]; goal: string; input_fingerprint: string;
  work_specs: { key: string; step: string; role: string; dependencies: string[] }[];
  target_configs?: { target_config_id: string; app_target: Target; [key: string]: unknown }[];
  approval_steps?: string[];
};
export type Artifact = Entity & { step?: string; digest: string; stale?: boolean; run_id?: string;
  work_item_id?: string; media_type?: string; content_type?: string; size?: number; name?: string };
export type MatrixEntry = { matrix_entry_id: string; test_case_id: string; app_target: Target;
  target_config_id: string; target_config_revision: number; required?: boolean };
export type MatrixRow = Entity & { run_id: string; state: string; candidate_fingerprint?: string;
  plan?: { entries: MatrixEntry[]; required_app_targets?: Target[] }; required_count?: number };
export type Check = Entity & { run_id: string; matrix_entry_id: string; candidate_fingerprint: string;
  execution_key: string; execution_status: string; quality_result: string; evidence_verified: boolean;
  executed_case_count?: number; raw_report_artifact_id?: string; node_result_id?: string };
export type Node = Entity & { node_id?: string; label?: string; state?: string; location?: string;
  allowed_app_targets?: Target[]; last_heartbeat_at?: string; capabilities?: unknown[] };
export type Resource = Entity & { node_id?: string; kind?: string; resource_type?: string; state?: string; label?: string };
export type VersionRef = { object_id: string; kind: 'artifact'; revision: number; fingerprint: string };
export type LocalExecutionStatus = {
  state: 'unprepared' | 'not_prepared' | 'preparing' | 'ready' | 'partial' | 'blocked';
  phase?: string; error_code?: string | null; message?: string; detail?: string; preparing?: boolean;
  target_configs?: { target_config_id: string; app_target?: Target; [key: string]: unknown }[]; ready_targets?: Target[];
};
export type ProductSetup = {
  ready: boolean; models_ready: boolean; requirements: { code: string; message: string }[];
  model_bindings: { role_model_profile_id?: string | null; coding_model_profile_id?: string | null };
  profiles: Profile[];
  local_execution: LocalExecutionStatus;
  cost_mode?: string; cost_notice?: string;
  configuration_path?: string; configuration_fingerprint?: string; restart_required?: boolean;
  product_defaults?: { target?: 'web' | 'api'; review_mode?: 'auto' | 'milestones' | 'every_step'; language?: ProductLanguage;
    max_model_requests?: number; output_root?: string; max_active_seconds?: number; max_tool_calls?: number };
};
export type Product = {
  id: string; revision?: number; name: string; goal: string; state: 'registered' | 'preparing' | 'running' | 'waiting_approval' | 'blocked' | 'completed' | 'cancelled';
  target?: ProductTarget | null; review_mode?: 'auto' | 'milestones' | 'every_step'; run_id?: string | null; project_id?: string | null;
  targets?: ProductTarget[]; creation_mode?: 'new' | 'import'; project_path?: string | null;
  execution_supported?: boolean; diagnosis?: ProductImportDiagnosis; run_ids?: string[]; current_change_id?: string | null;
  current_change?: ProductChange | null; can_add_change?: boolean;
  language?: ProductLanguage; initial_language?: ProductLanguage;
  max_model_requests?: number; deleted_at?: string | null; needs_restart?: boolean;
  config_revision?: number; run_config_revision?: number | null;
  management?: { can_edit: boolean; can_delete: boolean; can_restore: boolean; can_restart: boolean;
    blocked_reasons: { code: string; message: string }[] };
  output_directory: string; blocking_reasons: string[]; restore_reconciliation_required?: boolean; finalization_error?: boolean;
  delivery?: { source_commit: string; path: string; archive_download_url: string; launch_command: string; working_directory: string };
  launch?: { url?: string; state: string; detail?: string };
};
export type ProductImportDiagnosis = {
  project_path: string; detected_targets: ProductTarget[]; frameworks: string[];
  markers: { path: string; target: ProductTarget; reason: string }[];
  execution_supported: boolean; blocking_reasons: { code: string; message: string }[];
  git_detected: boolean; diagnosis_kind: 'static';
};
export type ProductChange = {
  id: string; product_id: string; title?: string; description: string; base_run_id?: string | null;
  kind?: 'change' | 'restart';
  config_revision?: number;
  acceptance_criteria?: string | null;
  language?: ProductLanguage;
  run_id?: string | null; plan_id?: string | null; state: string; phase?: string;
  start_stage: 'prd' | 'goal'; architecture_policy: 'review_existing' | 'design_for_current_goal'; reused_input_versions?: VersionRef[];
  blocking_reasons: string[]; created_at: string;
};
export const PRODUCT_TARGETS: ProductTarget[] = ['web', 'api', 'ios', 'android', 'windows', 'macos', 'linux'];
export const TARGETS: Target[] = ['web', 'api', 'ios_native', 'android_native', 'windows_native', 'macos_native', 'linux_native'];

export function recordId(value: Entity): string {
  return value.id || String(value.model_profile_id || value.node_id || value.backend_id || '');
}
export function stringValue(value: unknown, fallback = '未提供'): string {
  return typeof value === 'string' && value ? value : fallback;
}

export type WorkflowVersion = {
  id: string; label: string; run_id: string | null; change_id: string | null;
  kind: 'initial' | 'baseline' | 'restart' | 'change' | 'run'; state: string; base_run_id?: string | null; created_at?: string;
};
export type WorkflowProject = {
  id: string; name: string; project_id: string | null; product_id: string | null; deleted: boolean;
  versions: WorkflowVersion[]; default_version_id: string;
};
