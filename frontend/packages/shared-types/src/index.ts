export type MessageRole = "user" | "assistant" | "system";

export type TaskStatus =
  | "created"
  | "queued"
  | "dispatching"
  | "running"
  | "waiting_user_input"
  | "waiting_confirmation"
  | "resuming"
  | "completed"
  | "failed"
  | "cancelled";

export type InteractionSource = "router" | "agent";

export interface InteractionAction {
  code: string;
  label: string;
}

export interface InteractionField {
  key: string;
  label: string;
  value: string;
}

export interface RouterPlanItem {
  taskId?: string | null;
  intentCode: string;
  title: string;
  status: string;
  confidence?: number;
}

export type PlanStatus =
  | "draft"
  | "waiting_confirmation"
  | "running"
  | "partially_completed"
  | "completed"
  | "cancelled";

export interface InteractionCard {
  source: InteractionSource;
  type: string;
  cardType: string;
  status?: PlanStatus | string;
  title?: string;
  summary?: string;
  cardCode?: string;
  version?: number;
  fields?: InteractionField[];
  items?: RouterPlanItem[];
  actions?: InteractionAction[];
  confirmToken?: string;
}

export interface ChatMessage {
  id: string;
  role: MessageRole;
  content: string;
  createdAt: string;
}

export interface TaskSummary {
  taskId: string;
  intentCode: string;
  status: TaskStatus;
  confidence: number;
  message?: string;
  updatedAt?: string;
}

export interface CandidateIntent {
  intentCode: string;
  confidence: number;
  reason: string;
}

export interface RouterSnapshot {
  sessionId: string;
  custId: string;
  messages: ChatMessage[];
  tasks: TaskSummary[];
  candidateIntents: CandidateIntent[];
  pendingPlan?: InteractionCard | null;
  activeTaskId?: string | null;
  expiresAt?: string;
}

export interface RouterTaskEvent {
  taskId: string;
  sessionId: string;
  intentCode: string;
  status: TaskStatus;
  message?: string | null;
  ishandover?: boolean | null;
  payload?: Record<string, unknown>;
  interaction?: InteractionCard | null;
  createdAt: string;
}

export interface RouterSseEvent {
  event: string;
  data: RouterTaskEvent;
  at: string;
}

export interface IntentDefinition {
  intentCode: string;
  name: string;
  description: string;
  examples: string[];
  agentUrl: string;
  status: "active" | "inactive" | "grayscale";
  isFallback: boolean;
  dispatchPriority: number;
  requestSchema: Record<string, unknown>;
  fieldMapping: Record<string, string>;
  resumePolicy: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface IntentInput {
  intentCode: string;
  name: string;
  description: string;
  examples: string[];
  agentUrl: string;
  status: "active" | "inactive" | "grayscale";
  isFallback: boolean;
  dispatchPriority: number;
  requestSchema: Record<string, unknown>;
  fieldMapping: Record<string, string>;
  resumePolicy: string;
}

export interface SessionCreateInput {
  custId: string;
  sessionId?: string;
}

export interface SessionActionInput {
  sessionId: string;
  custId?: string;
  taskId: string;
  source: InteractionSource;
  actionCode: string;
  confirmToken?: string;
  payload?: Record<string, unknown>;
}

export type PerfRunStatus =
  | "queued"
  | "validating"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export type PerfStageStatus = "pending" | "running" | "completed" | "failed" | "cancelled";

export interface PerfMetrics {
  totalRequests?: number | null;
  successCount?: number | null;
  failureCount?: number | null;
  successRate?: number | null;
  rps?: number | null;
  avgMs?: number | null;
  p50Ms?: number | null;
  p95Ms?: number | null;
  p99Ms?: number | null;
  maxMs?: number | null;
  timeoutCount?: number | null;
  statusCodeBreakdown?: Record<string, number>;
  errorTypeBreakdown?: Record<string, number>;
}

export interface PerfTestStepInput {
  name?: string;
  concurrency: number;
  durationSec: number;
  warmupSec?: number;
  requestLimit?: number;
  cooldownSec?: number;
  timeoutMs?: number;
}

export interface PerfFailureSample {
  sampleId: string;
  stageName?: string;
  stepIndex?: number | null;
  statusCode?: number | string | null;
  errorType?: string | null;
  message: string;
  latencyMs?: number | null;
  requestSummary?: string | null;
  occurredAt?: string | null;
}

export interface PerfTestCase {
  caseId: string;
  name: string;
  description?: string;
  category?: string;
  tags?: string[];
  targetRoute?: string;
  defaultSteps: PerfTestStepInput[];
  notes?: string[];
}

export interface PerfTestRunSummary {
  runId: string;
  caseId: string;
  caseName?: string;
  status: PerfRunStatus;
  startedAt?: string | null;
  updatedAt?: string | null;
  finishedAt?: string | null;
  currentStageIndex?: number | null;
  totalStages?: number | null;
  aggregateMetrics?: PerfMetrics;
}

export interface PerfRunProgress {
  completedStages: number;
  totalStages: number;
  currentStageIndex?: number | null;
  currentStageName?: string | null;
  elapsedSec?: number | null;
  lastHeartbeatAt?: string | null;
}

export interface PerfStageResult {
  stageId: string;
  stepIndex: number;
  name: string;
  status: PerfStageStatus;
  concurrency: number;
  durationSec: number;
  warmupSec?: number | null;
  requestLimit?: number | null;
  cooldownSec?: number | null;
  timeoutMs?: number | null;
  startedAt?: string | null;
  finishedAt?: string | null;
  metrics: PerfMetrics;
  failureSamples?: PerfFailureSample[];
}

export interface PerfTestRunDetail extends PerfTestRunSummary {
  targetBaseUrl?: string | null;
  progress: PerfRunProgress;
  ladderSteps: PerfTestStepInput[];
  stepResults: PerfStageResult[];
  errorSamples: PerfFailureSample[];
}

export interface PerfTestRunCreateInput {
  caseId: string;
  ladderSteps: PerfTestStepInput[];
}
