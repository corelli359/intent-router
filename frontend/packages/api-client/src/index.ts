import type {
  ChatMessage,
  InteractionCard,
  IntentDefinition,
  IntentInput,
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
}
