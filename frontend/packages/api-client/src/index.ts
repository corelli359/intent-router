import type {
  ChatMessage,
  IntentDefinition,
  IntentInput,
  RouterSnapshot,
  RouterSseEvent,
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

interface BackendSnapshot {
  session_id: string;
  cust_id: string;
  messages: BackendMessage[];
  tasks: BackendTask[];
  candidate_intents: BackendCandidate[];
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
  dispatch_priority: number;
  request_schema: Record<string, unknown>;
  field_mapping: Record<string, string>;
  resume_policy: string;
  created_at?: string;
  updated_at?: string;
}

const defaultOptions: Required<ApiClientOptions> = {
  routerBaseUrl: process.env.NEXT_PUBLIC_ROUTER_BASE_URL ?? "/api/router",
  adminBaseUrl: process.env.NEXT_PUBLIC_ADMIN_BASE_URL ?? "/api/admin"
};

function mapSnapshot(snapshot: BackendSnapshot): RouterSnapshot {
  return {
    sessionId: snapshot.session_id,
    custId: snapshot.cust_id,
    messages: snapshot.messages.map((message, index) => ({
      id: `${message.created_at}-${index}`,
      role: message.role,
      content: message.content,
      createdAt: message.created_at
    })),
    tasks: snapshot.tasks.map((task) => ({
      taskId: task.task_id,
      intentCode: task.intent_code,
      status: task.status,
      confidence: task.confidence,
      updatedAt: task.updated_at
    })),
    candidateIntents: snapshot.candidate_intents.map((candidate) => ({
      intentCode: candidate.intent_code,
      confidence: candidate.confidence,
      reason: candidate.reason
    })),
    activeTaskId: snapshot.active_task_id,
    expiresAt: snapshot.expires_at
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
    const payload = await response.json();
    return {
      sessionId: payload.session_id,
      custId: payload.cust_id
    };
  }

  async getSession(sessionId: string): Promise<RouterSnapshot> {
    const response = await fetch(`${this.options.routerBaseUrl}/sessions/${sessionId}`);
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
    const result = await response.json();
    return mapSnapshot(result.snapshot as BackendSnapshot);
  }

  subscribeSession(sessionId: string, onEvent: (event: RouterSseEvent) => void): () => void {
    const source = new EventSource(`${this.options.routerBaseUrl}/sessions/${sessionId}/events`);
    const forward = (eventName: string, message: MessageEvent<string>) => {
      const data = JSON.parse(message.data) as RouterTaskEvent;
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
      "task.completed",
      "task.failed",
      "session.recognized",
      "session.idle",
      "session.waiting_user_input"
    ].forEach((eventName) => {
      source.addEventListener(eventName, (message) => {
        forward(eventName, message as MessageEvent<string>);
      });
    });
    return () => source.close();
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
}
