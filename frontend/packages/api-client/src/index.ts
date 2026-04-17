import type {
  ChatMessage,
  InteractionCard,
  IntentDefinition,
  IntentInput,
  PerfFailureSample,
  PerfMetrics,
  PerfRunProgress,
  PerfStageResult,
  PerfTestCase,
  PerfTestRunCreateInput,
  PerfTestRunDetail,
  PerfTestRunSummary,
  PerfTestStepInput,
  RouterSnapshot,
  RouterSseEvent,
  SessionActionInput,
  RouterTaskEvent,
  SessionCreateInput,
  TaskSummary
} from "@intent-router/shared-types";

export interface MessagePayload {
  sessionId: string;
  content: string;
  custId?: string;
}

export interface ApiClientOptions {
  routerBaseUrl?: string;
  adminBaseUrl?: string;
}

export interface MessageStreamHandlers {
  onEvent?: (event: RouterSseEvent) => void;
}

interface BackendMessage {
  role: ChatMessage["role"];
  content: string;
  created_at: string;
}

interface BackendTask {
  task_id: string;
  intent_code: string;
  status: TaskSummary["status"];
  confidence: number;
  updated_at?: string;
}

interface BackendCandidate {
  intent_code: string;
  confidence: number;
  reason: string;
}

interface BackendTaskEvent {
  task_id?: string;
  taskId?: string;
  session_id?: string;
  sessionId?: string;
  intent_code?: string;
  intentCode?: string;
  status: TaskSummary["status"];
  message?: string | null;
  ishandover?: boolean | null;
  payload?: Record<string, unknown>;
  created_at?: string;
  createdAt?: string;
}

interface BackendInteraction {
  source?: string;
  type?: string;
  card_type?: string;
  cardType?: string;
  title?: string;
  summary?: string;
  card_code?: string;
  cardCode?: string;
  version?: number;
  fields?: Array<{ key?: string; label?: string; value?: string }>;
  items?: Array<{
    task_id?: string;
    taskId?: string;
    intent_code?: string;
    intentCode?: string;
    title?: string;
    status?: string;
  }>;
  actions?: Array<{ code?: string; label?: string }>;
  confirm_token?: string;
  confirmToken?: string;
}

interface BackendPlanItem {
  task_id?: string;
  taskId?: string;
  intent_code?: string;
  intentCode?: string;
  title?: string;
  status?: string;
  confidence?: number;
}

interface BackendPendingPlan {
  source?: string;
  card_type?: string;
  cardType?: string;
  type?: string;
  status?: string;
  title?: string;
  summary?: string;
  version?: number;
  plan_id?: string;
  planId?: string;
  confirm_token?: string;
  confirmToken?: string;
  items?: BackendPlanItem[];
}

interface BackendSnapshot {
  session_id: string;
  cust_id: string;
  messages: BackendMessage[];
  tasks: BackendTask[];
  candidate_intents: BackendCandidate[];
  pending_plan?: BackendPendingPlan | null;
  plan_status?: string;
  active_task_id?: string | null;
  expires_at?: string;
}

interface BackendIntent {
  intent_code: string;
  name: string;
  description: string;
  examples: string[];
  agent_url: string;
  status: IntentDefinition["status"];
  is_fallback: boolean;
  dispatch_priority: number;
  request_schema: Record<string, unknown>;
  field_mapping: Record<string, string>;
  resume_policy: string;
  created_at?: string;
  updated_at?: string;
}

interface BackendPerfMetrics {
  total_requests?: number | string | null;
  totalRequests?: number | string | null;
  success_count?: number | string | null;
  successCount?: number | string | null;
  failure_count?: number | string | null;
  failureCount?: number | string | null;
  success_rate?: number | string | null;
  successRate?: number | string | null;
  rps?: number | string | null;
  avg_ms?: number | string | null;
  avgMs?: number | string | null;
  p50_ms?: number | string | null;
  p50Ms?: number | string | null;
  p95_ms?: number | string | null;
  p95Ms?: number | string | null;
  p99_ms?: number | string | null;
  p99Ms?: number | string | null;
  max_ms?: number | string | null;
  maxMs?: number | string | null;
  timeout_count?: number | string | null;
  timeoutCount?: number | string | null;
  status_code_breakdown?: Record<string, number | string>;
  statusCodeBreakdown?: Record<string, number | string>;
  error_type_breakdown?: Record<string, number | string>;
  errorTypeBreakdown?: Record<string, number | string>;
}

interface BackendPerfStep {
  name?: string;
  concurrency?: number | string;
  duration_sec?: number | string;
  durationSec?: number | string;
  warmup_sec?: number | string | null;
  warmupSec?: number | string | null;
  request_limit?: number | string | null;
  requestLimit?: number | string | null;
  cooldown_sec?: number | string | null;
  cooldownSec?: number | string | null;
  timeout_ms?: number | string | null;
  timeoutMs?: number | string | null;
}

interface BackendPerfFailureSample {
  sample_id?: string;
  sampleId?: string;
  id?: string;
  stage_name?: string | null;
  stageName?: string | null;
  step_index?: number | string | null;
  stepIndex?: number | string | null;
  status_code?: number | string | null;
  statusCode?: number | string | null;
  error_type?: string | null;
  errorType?: string | null;
  message?: string;
  latency_ms?: number | string | null;
  latencyMs?: number | string | null;
  request_summary?: string | null;
  requestSummary?: string | null;
  occurred_at?: string | null;
  occurredAt?: string | null;
}

interface BackendPerfCase {
  case_id?: string;
  caseId?: string;
  id?: string;
  name?: string;
  title?: string;
  description?: string;
  category?: string;
  tags?: string[];
  target_route?: string;
  targetRoute?: string;
  default_steps?: BackendPerfStep[];
  defaultSteps?: BackendPerfStep[];
  ladder_steps?: BackendPerfStep[];
  ladderSteps?: BackendPerfStep[];
  notes?: string[];
}

interface BackendPerfProgress {
  completed_stages?: number | string | null;
  completedStages?: number | string | null;
  total_stages?: number | string | null;
  totalStages?: number | string | null;
  current_stage_index?: number | string | null;
  currentStageIndex?: number | string | null;
  current_stage_name?: string | null;
  currentStageName?: string | null;
  elapsed_sec?: number | string | null;
  elapsedSec?: number | string | null;
  last_heartbeat_at?: string | null;
  lastHeartbeatAt?: string | null;
}

interface BackendPerfStageResult {
  stage_id?: string;
  stageId?: string;
  step_index?: number | string;
  stepIndex?: number | string;
  name?: string;
  status?: PerfStageResult["status"];
  concurrency?: number | string;
  duration_sec?: number | string;
  durationSec?: number | string;
  warmup_sec?: number | string | null;
  warmupSec?: number | string | null;
  request_limit?: number | string | null;
  requestLimit?: number | string | null;
  cooldown_sec?: number | string | null;
  cooldownSec?: number | string | null;
  timeout_ms?: number | string | null;
  timeoutMs?: number | string | null;
  started_at?: string | null;
  startedAt?: string | null;
  finished_at?: string | null;
  finishedAt?: string | null;
  metrics?: BackendPerfMetrics;
  failure_samples?: BackendPerfFailureSample[];
  failureSamples?: BackendPerfFailureSample[];
}

interface BackendPerfRun {
  run_id?: string;
  runId?: string;
  case_id?: string;
  caseId?: string;
  case_name?: string;
  caseName?: string;
  target_base_url?: string | null;
  targetBaseUrl?: string | null;
  ladder_steps?: BackendPerfStep[];
  ladderSteps?: BackendPerfStep[];
  status: PerfTestRunSummary["status"];
  started_at?: string | null;
  startedAt?: string | null;
  updated_at?: string | null;
  updatedAt?: string | null;
  finished_at?: string | null;
  finishedAt?: string | null;
  current_stage_index?: number | string | null;
  currentStageIndex?: number | string | null;
  total_stages?: number | string | null;
  totalStages?: number | string | null;
  aggregate_metrics?: BackendPerfMetrics;
  aggregateMetrics?: BackendPerfMetrics;
  progress?: BackendPerfProgress;
  step_results?: BackendPerfStageResult[];
  stepResults?: BackendPerfStageResult[];
  error_samples?: BackendPerfFailureSample[];
  errorSamples?: BackendPerfFailureSample[];
}

const defaultOptions: Required<ApiClientOptions> = {
  routerBaseUrl: process.env.NEXT_PUBLIC_ROUTER_BASE_URL ?? "/api/router",
  adminBaseUrl: process.env.NEXT_PUBLIC_ADMIN_BASE_URL ?? "/api/admin"
};

async function readError(response: Response): Promise<string> {
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    const payload = (await response.json()) as { detail?: string; message?: string };
    return payload.detail ?? payload.message ?? JSON.stringify(payload);
  }
  return await response.text();
}

function getArrayPayload<T>(payload: unknown, keys: string[]): T[] {
  if (Array.isArray(payload)) {
    return payload as T[];
  }

  if (!payload || typeof payload !== "object") {
    return [];
  }

  for (const key of keys) {
    const value = (payload as Record<string, unknown>)[key];
    if (Array.isArray(value)) {
      return value as T[];
    }
  }

  return [];
}

function getObjectPayload<T>(payload: unknown, keys: string[]): T | null {
  if (payload && typeof payload === "object" && !Array.isArray(payload)) {
    const record = payload as Record<string, unknown>;
    const directRunId = record.runId ?? record.run_id;
    const directCaseId = record.caseId ?? record.case_id;
    if (typeof directRunId === "string" || typeof directCaseId === "string") {
      return payload as T;
    }

    for (const key of keys) {
      const value = record[key];
      if (value && typeof value === "object" && !Array.isArray(value)) {
        return value as T;
      }
    }
  }

  return null;
}

function readString(...values: Array<unknown>): string | undefined {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) {
      return value;
    }
  }
  return undefined;
}

function readNumber(...values: Array<unknown>): number | undefined {
  for (const value of values) {
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
    if (typeof value === "string" && value.trim()) {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) {
        return parsed;
      }
    }
  }
  return undefined;
}

function mapNumberRecord(input: Record<string, number | string> | undefined): Record<string, number> | undefined {
  if (!input) {
    return undefined;
  }

  const mapped = Object.entries(input).reduce<Record<string, number>>((result, [key, value]) => {
    const next = readNumber(value);
    if (typeof next === "number") {
      result[key] = next;
    }
    return result;
  }, {});

  return Object.keys(mapped).length > 0 ? mapped : undefined;
}

function mapPerfMetrics(metrics: BackendPerfMetrics | undefined): PerfMetrics {
  if (!metrics) {
    return {};
  }

  return {
    totalRequests: readNumber(metrics.totalRequests, metrics.total_requests),
    successCount: readNumber(metrics.successCount, metrics.success_count),
    failureCount: readNumber(metrics.failureCount, metrics.failure_count),
    successRate: readNumber(metrics.successRate, metrics.success_rate),
    rps: readNumber(metrics.rps),
    avgMs: readNumber(metrics.avgMs, metrics.avg_ms),
    p50Ms: readNumber(metrics.p50Ms, metrics.p50_ms),
    p95Ms: readNumber(metrics.p95Ms, metrics.p95_ms),
    p99Ms: readNumber(metrics.p99Ms, metrics.p99_ms),
    maxMs: readNumber(metrics.maxMs, metrics.max_ms),
    timeoutCount: readNumber(metrics.timeoutCount, metrics.timeout_count),
    statusCodeBreakdown: mapNumberRecord(metrics.statusCodeBreakdown ?? metrics.status_code_breakdown),
    errorTypeBreakdown: mapNumberRecord(metrics.errorTypeBreakdown ?? metrics.error_type_breakdown)
  };
}

function mapPerfStep(step: BackendPerfStep | undefined, index: number): PerfTestStepInput {
  return {
    name: readString(step?.name) ?? `阶段 ${index + 1}`,
    concurrency: readNumber(step?.concurrency) ?? 0,
    durationSec: readNumber(step?.durationSec, step?.duration_sec) ?? 60,
    warmupSec: readNumber(step?.warmupSec, step?.warmup_sec),
    requestLimit: readNumber(step?.requestLimit, step?.request_limit),
    cooldownSec: readNumber(step?.cooldownSec, step?.cooldown_sec),
    timeoutMs: readNumber(step?.timeoutMs, step?.timeout_ms)
  };
}

function mapPerfFailureSample(sample: BackendPerfFailureSample, index: number): PerfFailureSample {
  return {
    sampleId: readString(sample.sampleId, sample.sample_id, sample.id) ?? `sample-${index + 1}`,
    stageName: readString(sample.stageName, sample.stage_name),
    stepIndex: readNumber(sample.stepIndex, sample.step_index),
    statusCode:
      typeof sample.statusCode === "number" || typeof sample.statusCode === "string"
        ? sample.statusCode
        : typeof sample.status_code === "number" || typeof sample.status_code === "string"
          ? sample.status_code
          : null,
    errorType: readString(sample.errorType, sample.error_type) ?? null,
    message: readString(sample.message) ?? "未知错误",
    latencyMs: readNumber(sample.latencyMs, sample.latency_ms),
    requestSummary: readString(sample.requestSummary, sample.request_summary) ?? null,
    occurredAt: readString(sample.occurredAt, sample.occurred_at) ?? null
  };
}

function mapPerfStageResult(stage: BackendPerfStageResult, index: number): PerfStageResult {
  return {
    stageId: readString(stage.stageId, stage.stage_id) ?? `stage-${index + 1}`,
    stepIndex: readNumber(stage.stepIndex, stage.step_index) ?? index,
    name: readString(stage.name) ?? `阶段 ${index + 1}`,
    status: stage.status ?? "pending",
    concurrency: readNumber(stage.concurrency) ?? 0,
    durationSec: readNumber(stage.durationSec, stage.duration_sec) ?? 0,
    warmupSec: readNumber(stage.warmupSec, stage.warmup_sec),
    requestLimit: readNumber(stage.requestLimit, stage.request_limit),
    cooldownSec: readNumber(stage.cooldownSec, stage.cooldown_sec),
    timeoutMs: readNumber(stage.timeoutMs, stage.timeout_ms),
    startedAt: readString(stage.startedAt, stage.started_at) ?? null,
    finishedAt: readString(stage.finishedAt, stage.finished_at) ?? null,
    metrics: mapPerfMetrics(stage.metrics),
    failureSamples: (stage.failureSamples ?? stage.failure_samples ?? []).map(mapPerfFailureSample)
  };
}

function mapPerfProgress(
  progress: BackendPerfProgress | undefined,
  stepResults: PerfStageResult[],
  ladderSteps: PerfTestStepInput[],
  fallbackStageIndex?: number,
  fallbackTotalStages?: number
): PerfRunProgress {
  const totalStages = readNumber(progress?.totalStages, progress?.total_stages, fallbackTotalStages) ?? ladderSteps.length;
  const currentStageIndex = readNumber(
    progress?.currentStageIndex,
    progress?.current_stage_index,
    fallbackStageIndex
  );
  const completedFromStages = stepResults.filter((step) => step.status === "completed").length;

  return {
    completedStages: readNumber(progress?.completedStages, progress?.completed_stages, completedFromStages) ?? 0,
    totalStages,
    currentStageIndex,
    currentStageName:
      readString(progress?.currentStageName, progress?.current_stage_name) ??
      (typeof currentStageIndex === "number" ? stepResults[currentStageIndex]?.name ?? null : null),
    elapsedSec: readNumber(progress?.elapsedSec, progress?.elapsed_sec) ?? null,
    lastHeartbeatAt: readString(progress?.lastHeartbeatAt, progress?.last_heartbeat_at) ?? null
  };
}

function mapPerfCase(item: BackendPerfCase): PerfTestCase {
  const defaultSteps =
    item.defaultSteps ?? item.default_steps ?? item.ladderSteps ?? item.ladder_steps ?? [];

  return {
    caseId: readString(item.caseId, item.case_id, item.id) ?? "unknown-case",
    name: readString(item.name, item.title, item.caseId, item.case_id, item.id) ?? "未命名用例",
    description: readString(item.description),
    category: readString(item.category),
    tags: Array.isArray(item.tags) ? item.tags.filter((tag) => typeof tag === "string") : undefined,
    targetRoute: readString(item.targetRoute, item.target_route),
    defaultSteps: defaultSteps.map(mapPerfStep),
    notes: Array.isArray(item.notes) ? item.notes.filter((note) => typeof note === "string") : undefined
  };
}

function mapPerfRunSummary(run: BackendPerfRun): PerfTestRunSummary {
  const ladderSteps = run.ladderSteps ?? run.ladder_steps ?? [];
  const stepResults = run.stepResults ?? run.step_results ?? [];

  return {
    runId: readString(run.runId, run.run_id) ?? "unknown-run",
    caseId: readString(run.caseId, run.case_id) ?? "unknown-case",
    caseName: readString(run.caseName, run.case_name),
    status: run.status,
    startedAt: readString(run.startedAt, run.started_at) ?? null,
    updatedAt: readString(run.updatedAt, run.updated_at) ?? null,
    finishedAt: readString(run.finishedAt, run.finished_at) ?? null,
    currentStageIndex: readNumber(run.currentStageIndex, run.current_stage_index),
    totalStages: readNumber(run.totalStages, run.total_stages, ladderSteps.length, stepResults.length),
    aggregateMetrics: mapPerfMetrics(run.aggregateMetrics ?? run.aggregate_metrics)
  };
}

function mapPerfRunDetail(run: BackendPerfRun): PerfTestRunDetail {
  const ladderSteps = (run.ladderSteps ?? run.ladder_steps ?? []).map(mapPerfStep);
  const stepResults = (run.stepResults ?? run.step_results ?? []).map(mapPerfStageResult);
  const errorSamples = (run.errorSamples ?? run.error_samples ?? []).map(mapPerfFailureSample);
  const summary = mapPerfRunSummary(run);

  return {
    ...summary,
    targetBaseUrl: readString(run.targetBaseUrl, run.target_base_url) ?? null,
    progress: mapPerfProgress(run.progress, stepResults, ladderSteps, summary.currentStageIndex ?? undefined, summary.totalStages ?? undefined),
    ladderSteps,
    stepResults,
    errorSamples
  };
}

function mapSnapshot(snapshot: BackendSnapshot): RouterSnapshot {
  return {
    sessionId: snapshot.session_id,
    custId: snapshot.cust_id,
    messages: (snapshot.messages ?? []).map((message, index) => ({
      id: `${message.created_at}-${index}`,
      role: message.role,
      content: message.content,
      createdAt: message.created_at
    })),
    tasks: (snapshot.tasks ?? []).map((task) => ({
      taskId: task.task_id,
      intentCode: task.intent_code,
      status: task.status,
      confidence: task.confidence,
      updatedAt: task.updated_at
    })),
    candidateIntents: (snapshot.candidate_intents ?? []).map((candidate) => ({
      intentCode: candidate.intent_code,
      confidence: candidate.confidence,
      reason: candidate.reason
    })),
    pendingPlan: mapPendingPlan(snapshot.pending_plan, snapshot.plan_status),
    activeTaskId: snapshot.active_task_id,
    expiresAt: snapshot.expires_at
  };
}

function mapTaskEvent(event: BackendTaskEvent): RouterTaskEvent {
  const payload = event.payload ?? {};
  const interaction = mapInteraction(
    payload.interaction as BackendInteraction | undefined,
    typeof payload.plan_status === "string" ? payload.plan_status : undefined
  ) ?? mapPlanFromPayload(payload);
  return {
    taskId: event.taskId ?? event.task_id ?? "unknown-task",
    sessionId: event.sessionId ?? event.session_id ?? "unknown-session",
    intentCode: event.intentCode ?? event.intent_code ?? "unknown-intent",
    status: event.status,
    message: event.message ?? null,
    ishandover: event.ishandover ?? null,
    payload,
    interaction,
    createdAt: event.createdAt ?? event.created_at ?? new Date().toISOString()
  };
}

function mapPendingPlan(plan: BackendPendingPlan | null | undefined, planStatus?: string): InteractionCard | null {
  if (!plan || typeof plan !== "object") {
    return null;
  }
  return {
    source: plan.source === "agent" ? "agent" : "router",
    type: typeof plan.type === "string" ? plan.type : "plan_card",
    cardType:
      (typeof plan.cardType === "string" && plan.cardType) ||
      (typeof plan.card_type === "string" && plan.card_type) ||
      "plan_confirm",
    status: typeof plan.status === "string" ? plan.status : planStatus,
    title: typeof plan.title === "string" ? plan.title : "请确认执行计划",
    summary: typeof plan.summary === "string" ? plan.summary : undefined,
    version: typeof plan.version === "number" ? plan.version : undefined,
    items: mapPlanItems(plan.items),
    actions:
      (typeof plan.status === "string" ? plan.status : planStatus) === "waiting_confirmation"
        ? [
            { code: "confirm_plan", label: "开始执行" },
            { code: "cancel_plan", label: "取消" }
          ]
        : undefined,
    confirmToken:
      (typeof plan.confirmToken === "string" && plan.confirmToken) ||
      (typeof plan.confirm_token === "string" && plan.confirm_token) ||
      undefined
  };
}

function mapPlanItems(items: BackendPlanItem[] | undefined): InteractionCard["items"] {
  if (!Array.isArray(items)) {
    return undefined;
  }
  const mapped = items
    .filter((item) => item && typeof (item.intentCode ?? item.intent_code) === "string")
    .map((item) => ({
      taskId: typeof (item.taskId ?? item.task_id) === "string" ? String(item.taskId ?? item.task_id) : undefined,
      intentCode: String(item.intentCode ?? item.intent_code ?? ""),
      title: typeof item.title === "string" ? item.title : String(item.intentCode ?? item.intent_code ?? ""),
      status: typeof item.status === "string" ? item.status : "pending",
      confidence: typeof item.confidence === "number" ? item.confidence : undefined
    }));
  return mapped.length > 0 ? mapped : undefined;
}

function mapPlanFromPayload(payload: Record<string, unknown>): InteractionCard | null {
  if (typeof payload.plan_id !== "string" && typeof payload.plan_status !== "string") {
    return null;
  }
  const status = typeof payload.plan_status === "string" ? payload.plan_status : undefined;
  return {
    source: "router",
    type: "plan_card",
    cardType: "plan_confirm",
    status,
    title: typeof payload.title === "string" ? payload.title : "执行计划",
    summary: typeof payload.summary === "string" ? payload.summary : undefined,
    items: mapPlanItems(payload.items as BackendPlanItem[] | undefined),
    actions:
      status === "waiting_confirmation"
        ? [
            { code: "confirm_plan", label: "开始执行" },
            { code: "cancel_plan", label: "取消" }
          ]
        : undefined,
    confirmToken:
      typeof payload.confirm_token === "string"
        ? payload.confirm_token
        : typeof payload.confirmToken === "string"
          ? payload.confirmToken
          : undefined
  };
}

function mapInteraction(interaction: BackendInteraction | undefined, status?: string): InteractionCard | null {
  if (!interaction || typeof interaction !== "object") {
    return null;
  }
  const source = interaction.source === "agent" ? "agent" : interaction.source === "router" ? "router" : null;
  const type = typeof interaction.type === "string" ? interaction.type : null;
  const cardType =
    (typeof interaction.cardType === "string" && interaction.cardType) ||
    (typeof interaction.card_type === "string" && interaction.card_type) ||
    null;
  if (!source || !type || !cardType) {
    return null;
  }
  return {
    source,
    type,
    cardType,
    status: typeof status === "string" ? status : undefined,
    title: typeof interaction.title === "string" ? interaction.title : undefined,
    summary: typeof interaction.summary === "string" ? interaction.summary : undefined,
    cardCode:
      (typeof interaction.cardCode === "string" && interaction.cardCode) ||
      (typeof interaction.card_code === "string" && interaction.card_code) ||
      undefined,
    version: typeof interaction.version === "number" ? interaction.version : undefined,
    fields: Array.isArray(interaction.fields)
      ? interaction.fields
          .filter((item) => item && typeof item.key === "string" && typeof item.label === "string")
          .map((item) => ({
            key: item.key as string,
            label: item.label as string,
            value: typeof item.value === "string" ? item.value : ""
          }))
      : undefined,
    items: Array.isArray(interaction.items)
      ? interaction.items
          .filter((item) => item && typeof (item.intentCode ?? item.intent_code) === "string")
          .map((item) => ({
            taskId: typeof (item.taskId ?? item.task_id) === "string" ? String(item.taskId ?? item.task_id) : undefined,
            intentCode: String(item.intentCode ?? item.intent_code ?? ""),
            title: typeof item.title === "string" ? item.title : String(item.intentCode ?? item.intent_code ?? ""),
            status: typeof item.status === "string" ? item.status : "pending",
            confidence: typeof (item as { confidence?: unknown }).confidence === "number"
              ? (item as { confidence: number }).confidence
              : undefined
          }))
      : undefined,
    actions: Array.isArray(interaction.actions)
      ? interaction.actions
          .filter((item) => item && typeof item.code === "string")
          .map((item) => ({ code: item.code as string, label: typeof item.label === "string" ? item.label : item.code as string }))
      : undefined,
    confirmToken:
      (typeof interaction.confirmToken === "string" && interaction.confirmToken) ||
      (typeof interaction.confirm_token === "string" && interaction.confirm_token) ||
      undefined
  };
}

function mapIntent(intent: BackendIntent): IntentDefinition {
  return {
    intentCode: intent.intent_code,
    name: intent.name,
    description: intent.description,
    examples: intent.examples,
    agentUrl: intent.agent_url,
    status: intent.status,
    isFallback: intent.is_fallback,
    dispatchPriority: intent.dispatch_priority,
    requestSchema: intent.request_schema,
    fieldMapping: intent.field_mapping,
    resumePolicy: intent.resume_policy,
    createdAt: intent.created_at,
    updatedAt: intent.updated_at
  };
}

function toIntentPayload(intent: IntentInput): Record<string, unknown> {
  return {
    intent_code: intent.intentCode,
    name: intent.name,
    description: intent.description,
    examples: intent.examples,
    agent_url: intent.agentUrl,
    status: intent.status,
    is_fallback: intent.isFallback,
    dispatch_priority: intent.dispatchPriority,
    request_schema: intent.requestSchema,
    field_mapping: intent.fieldMapping,
    resume_policy: intent.resumePolicy
  };
}

export class IntentRouterApiClient {
  private readonly options: Required<ApiClientOptions>;

  constructor(options: ApiClientOptions = {}) {
    this.options = { ...defaultOptions, ...options };
  }

  async createSession(input: SessionCreateInput): Promise<{ sessionId: string; custId: string }> {
    const response = await fetch(`${this.options.routerBaseUrl}/sessions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        cust_id: input.custId,
        session_id: input.sessionId
      })
    });
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    const payload = await response.json();
    return {
      sessionId: payload.session_id,
      custId: payload.cust_id
    };
  }

  async getSession(sessionId: string): Promise<RouterSnapshot> {
    const response = await fetch(`${this.options.routerBaseUrl}/sessions/${sessionId}`);
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    const payload = (await response.json()) as BackendSnapshot;
    return mapSnapshot(payload);
  }

  async sendMessage(payload: MessagePayload): Promise<RouterSnapshot> {
    const response = await fetch(`${this.options.routerBaseUrl}/sessions/${payload.sessionId}/messages`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ content: payload.content, cust_id: payload.custId ?? "cust_demo_001" })
    });
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    const result = await response.json();
    return mapSnapshot(result.snapshot as BackendSnapshot);
  }

  async sendMessageStream(payload: MessagePayload, handlers: MessageStreamHandlers = {}): Promise<void> {
    await this.streamPost(
      `${this.options.routerBaseUrl}/sessions/${payload.sessionId}/messages/stream`,
      { content: payload.content, cust_id: payload.custId ?? "cust_demo_001" },
      handlers
    );
  }

  private async streamPost(url: string, body: Record<string, unknown>, handlers: MessageStreamHandlers): Promise<void> {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream"
      },
      body: JSON.stringify(body)
    });
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    if (!response.body) {
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let currentEvent = "message";
    let dataLines: string[] = [];

    const flushEvent = () => {
      if (dataLines.length === 0) {
        currentEvent = "message";
        return;
      }
      const payloadText = dataLines.join("\n");
      try {
        const data = mapTaskEvent(JSON.parse(payloadText) as BackendTaskEvent);
        handlers.onEvent?.({ event: currentEvent, data, at: data.createdAt });
      } catch {
        // Drop malformed SSE frames instead of breaking the full stream.
      }
      currentEvent = "message";
      dataLines = [];
    };

    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });

      let newlineIndex = buffer.indexOf("\n");
      while (newlineIndex >= 0) {
        const rawLine = buffer.slice(0, newlineIndex);
        buffer = buffer.slice(newlineIndex + 1);
        const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;

        if (!line) {
          flushEvent();
        } else if (line.startsWith("event:")) {
          currentEvent = line.slice(6).trim() || "message";
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trimStart());
        }
        newlineIndex = buffer.indexOf("\n");
      }

      if (done) {
        if (buffer.trim()) {
          dataLines.push(buffer.trim());
          buffer = "";
        }
        flushEvent();
        break;
      }
    }
  }

  subscribeSession(sessionId: string, onEvent: (event: RouterSseEvent) => void): () => void {
    const source = new EventSource(`${this.options.routerBaseUrl}/sessions/${sessionId}/events`);
    const forward = (eventName: string, message: MessageEvent<string>) => {
      const data = mapTaskEvent(JSON.parse(message.data) as BackendTaskEvent);
      onEvent({ event: eventName, data, at: data.createdAt });
    };
    [
      "recognition.started",
      "recognition.delta",
      "recognition.completed",
      "task.created",
      "task.dispatching",
      "task.running",
      "task.message",
      "task.resuming",
      "task.waiting_user_input",
      "task.waiting_confirmation",
      "task.completed",
      "task.failed",
      "task.cancelled",
      "session.recognized",
      "session.idle",
      "session.waiting_user_input",
      "session.waiting_confirmation",
      "session.plan.proposed",
      "session.plan.waiting_confirmation",
      "session.plan.confirmed",
      "session.plan.updated",
      "session.plan.completed",
      "session.plan.cancelled"
    ].forEach((eventName) => {
      source.addEventListener(eventName, (message) => {
        forward(eventName, message as MessageEvent<string>);
      });
    });
    return () => source.close();
  }

  async sendSessionAction(input: SessionActionInput): Promise<RouterSnapshot> {
    const response = await fetch(`${this.options.routerBaseUrl}/sessions/${input.sessionId}/actions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        task_id: input.taskId,
        cust_id: input.custId,
        source: input.source,
        action_code: input.actionCode,
        confirm_token: input.confirmToken,
        payload: input.payload ?? {}
      })
    });
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    const result = await response.json();
    return mapSnapshot(result.snapshot as BackendSnapshot);
  }

  async sendSessionActionStream(input: SessionActionInput, handlers: MessageStreamHandlers = {}): Promise<void> {
    await this.streamPost(
      `${this.options.routerBaseUrl}/sessions/${input.sessionId}/actions/stream`,
      {
        task_id: input.taskId,
        cust_id: input.custId,
        source: input.source,
        action_code: input.actionCode,
        confirm_token: input.confirmToken,
        payload: input.payload ?? {}
      },
      handlers
    );
  }

  async listIntents(): Promise<IntentDefinition[]> {
    const response = await fetch(`${this.options.adminBaseUrl}/intents`);
    const payload = await response.json();
    return (payload.items as BackendIntent[]).map(mapIntent);
  }

  async createIntent(intent: IntentInput): Promise<IntentDefinition> {
    const response = await fetch(`${this.options.adminBaseUrl}/intents`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify(toIntentPayload(intent))
    });
    if (!response.ok) {
      throw new Error(await response.text());
    }
    return mapIntent((await response.json()) as BackendIntent);
  }

  async updateIntent(intentCode: string, intent: IntentInput): Promise<IntentDefinition> {
    const response = await fetch(`${this.options.adminBaseUrl}/intents/${encodeURIComponent(intentCode)}`, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify(toIntentPayload(intent))
    });
    if (!response.ok) {
      throw new Error(await response.text());
    }
    return mapIntent((await response.json()) as BackendIntent);
  }

  async activateIntent(intentCode: string): Promise<IntentDefinition> {
    const response = await fetch(
      `${this.options.adminBaseUrl}/intents/${encodeURIComponent(intentCode)}/activate`,
      {
        method: "POST"
      }
    );
    if (!response.ok) {
      throw new Error(await response.text());
    }
    return mapIntent((await response.json()) as BackendIntent);
  }

  async deactivateIntent(intentCode: string): Promise<IntentDefinition> {
    const response = await fetch(
      `${this.options.adminBaseUrl}/intents/${encodeURIComponent(intentCode)}/deactivate`,
      {
        method: "POST"
      }
    );
    if (!response.ok) {
      throw new Error(await response.text());
    }
    return mapIntent((await response.json()) as BackendIntent);
  }

  async listPerfTestCases(): Promise<PerfTestCase[]> {
    const response = await fetch(`${this.options.adminBaseUrl}/perf-tests/cases`);
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    const payload = await response.json();
    return getArrayPayload<BackendPerfCase>(payload, ["items", "cases"]).map(mapPerfCase);
  }

  async listPerfTestRuns(): Promise<PerfTestRunSummary[]> {
    const response = await fetch(`${this.options.adminBaseUrl}/perf-tests/runs`);
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    const payload = await response.json();
    return getArrayPayload<BackendPerfRun>(payload, ["items", "runs"]).map(mapPerfRunSummary);
  }

  async createPerfTestRun(input: PerfTestRunCreateInput): Promise<PerfTestRunSummary> {
    const response = await fetch(`${this.options.adminBaseUrl}/perf-tests/runs`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        case_id: input.caseId,
        ladder_steps: input.ladderSteps.map((step) => ({
          name: step.name,
          concurrency: step.concurrency,
          duration_sec: step.durationSec,
          warmup_sec: step.warmupSec,
          request_limit: step.requestLimit,
          cooldown_sec: step.cooldownSec,
          timeout_ms: step.timeoutMs
        }))
      })
    });
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    const payload = await response.json();
    const run = getObjectPayload<BackendPerfRun>(payload, ["item", "run"]);
    if (!run) {
      throw new Error("压测任务创建成功，但返回体缺少 run 信息");
    }
    return mapPerfRunSummary(run);
  }

  async getPerfTestRun(runId: string): Promise<PerfTestRunDetail> {
    const response = await fetch(`${this.options.adminBaseUrl}/perf-tests/runs/${encodeURIComponent(runId)}`);
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    const payload = await response.json();
    const run = getObjectPayload<BackendPerfRun>(payload, ["item", "run"]);
    if (!run) {
      throw new Error("压测任务详情返回格式不正确");
    }
    return mapPerfRunDetail(run);
  }
}
