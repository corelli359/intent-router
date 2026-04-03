export type MessageRole = "user" | "assistant" | "system";

export type TaskStatus =
  | "created"
  | "queued"
  | "dispatching"
  | "running"
  | "waiting_user_input"
  | "resuming"
  | "completed"
  | "failed"
  | "cancelled";

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
  dispatchPriority: number;
  requestSchema: Record<string, unknown>;
  fieldMapping: Record<string, string>;
  resumePolicy: string;
}

export interface SessionCreateInput {
  custId: string;
  sessionId?: string;
}
