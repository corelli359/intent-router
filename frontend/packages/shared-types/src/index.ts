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
  taskId: string;
  source: InteractionSource;
  actionCode: string;
  confirmToken?: string;
  payload?: Record<string, unknown>;
}
