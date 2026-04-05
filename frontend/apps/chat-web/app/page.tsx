"use client";

import { startTransition, useEffect, useState } from "react";
import { IntentRouterApiClient } from "@intent-router/api-client";
import type {
  CandidateIntent,
  ChatMessage,
  InteractionCard,
  RouterSnapshot,
  RouterSseEvent,
  TaskSummary
} from "@intent-router/shared-types";
import { Badge } from "@intent-router/ui";

const api = new IntentRouterApiClient();
const DEFAULT_CUST_ID = "cust_demo_001";
const BOOT_MESSAGE: ChatMessage = {
  id: "assistant-boot",
  role: "assistant",
  content: "路由服务已就绪。你可以直接输入多个诉求，例如“先查订单，再取消明天的预约”。",
  createdAt: ""
};

const STATUS_LABELS: Record<TaskSummary["status"], string> = {
  created: "已创建",
  queued: "排队中",
  dispatching: "分发中",
  running: "执行中",
  waiting_user_input: "等待补充",
  waiting_confirmation: "等待确认",
  resuming: "恢复中",
  completed: "已完成",
  failed: "执行失败",
  cancelled: "已取消"
};

const STREAM_STATE_LABELS: Record<string, string> = {
  booting: "启动中",
  connecting: "连接中",
  connected: "已连接",
  disconnected: "已断开"
};

const EVENT_LABELS: Record<string, string> = {
  "recognition.started": "开始意图识别",
  "recognition.delta": "识别流式输出",
  "recognition.completed": "意图识别完成",
  "task.created": "任务已创建",
  "task.dispatching": "任务分发中",
  "task.running": "任务执行中",
  "task.message": "任务流式输出",
  "task.resuming": "恢复原任务",
  "task.waiting_user_input": "等待补充信息",
  "task.waiting_confirmation": "等待确认",
  "task.completed": "任务已完成",
  "task.failed": "任务失败",
  "task.cancelled": "任务取消",
  "session.recognized": "已完成意图识别",
  "session.idle": "会话空闲",
  "session.waiting_user_input": "会话等待输入",
  "session.waiting_confirmation": "会话等待确认",
  "session.plan.proposed": "计划已生成",
  "session.plan.waiting_confirmation": "等待确认计划",
  "session.plan.confirmed": "计划已确认",
  "session.plan.updated": "计划更新",
  "session.plan.completed": "计划完成",
  "session.plan.cancelled": "计划取消"
};

function toneForStatus(status?: TaskSummary["status"]): "default" | "warning" | "success" | "emphasis" {
  if (status === "completed") return "success";
  if (status === "failed" || status === "waiting_user_input" || status === "waiting_confirmation") return "warning";
  if (status === "running" || status === "dispatching" || status === "resuming") return "emphasis";
  return "default";
}

function toneForStreamState(state: string): "default" | "warning" | "success" | "emphasis" {
  if (state === "connected") return "success";
  if (state === "disconnected") return "warning";
  if (state === "booting" || state === "connecting") return "emphasis";
  return "default";
}

function formatTime(value: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit"
  });
}

function roleLabel(role: ChatMessage["role"]): string {
  if (role === "user") return "你";
  if (role === "assistant") return "助手";
  return "系统";
}

function eventLabel(eventName: string): string {
  return EVENT_LABELS[eventName] ?? eventName;
}

function planStatusLabel(status?: string): string {
  if (status === "running" || status === "partially_completed") return "执行中";
  if (status === "completed") return "已完成";
  if (status === "cancelled") return "已取消";
  return "待确认";
}

function planItemStatusLabel(status?: string): string {
  if (status === "running") return "执行中";
  if (status === "waiting_user_input") return "待补充";
  if (status === "waiting_confirmation") return "待确认";
  if (status === "completed") return "完成";
  if (status === "failed") return "失败";
  if (status === "cancelled") return "取消";
  if (status === "skipped") return "跳过";
  return "待执行";
}

function createLocalMessageId(): string {
  if (typeof globalThis !== "undefined" && typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `local-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function parsePayloadString(payload: Record<string, unknown> | undefined, key: string): string | null {
  const value = payload?.[key];
  return typeof value === "string" && value.length > 0 ? value : null;
}

function parsePayloadStringArray(payload: Record<string, unknown> | undefined, key: string): string[] {
  const value = payload?.[key];
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is string => typeof item === "string" && item.length > 0);
}

function parsePayloadNumber(payload: Record<string, unknown> | undefined, key: string): number | null {
  const value = payload?.[key];
  return typeof value === "number" ? value : null;
}

function parseCandidateIntent(value: unknown): CandidateIntent | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const record = value as Record<string, unknown>;
  const intentCode =
    (typeof record.intentCode === "string" && record.intentCode) ||
    (typeof record.intent_code === "string" && record.intent_code) ||
    null;
  const confidence = typeof record.confidence === "number" ? record.confidence : null;
  if (!intentCode || confidence === null) {
    return null;
  }
  return {
    intentCode,
    confidence,
    reason: typeof record.reason === "string" ? record.reason : ""
  };
}

function parseCandidateIntents(payload: Record<string, unknown> | undefined, key: string): CandidateIntent[] {
  const value = payload?.[key];
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map(parseCandidateIntent).filter((candidate): candidate is CandidateIntent => candidate !== null);
}

function upsertTaskSummary(previous: TaskSummary[], event: RouterSseEvent): TaskSummary[] {
  const nextTask: TaskSummary = {
    taskId: event.data.taskId,
    intentCode: event.data.intentCode,
    status: event.data.status,
    confidence: parsePayloadNumber(event.data.payload, "confidence") ?? 0,
    message: event.data.message ?? undefined,
    updatedAt: event.data.createdAt
  };

  const existingIndex = previous.findIndex((task) => task.taskId === nextTask.taskId);
  if (existingIndex < 0) {
    return [...previous, nextTask];
  }

  const existing = previous[existingIndex];
  const merged: TaskSummary = {
    ...existing,
    ...nextTask,
    confidence: nextTask.confidence > 0 ? nextTask.confidence : existing.confidence,
    message: event.data.message ?? existing.message
  };
  return previous.map((task, index) => (index === existingIndex ? merged : task));
}

function markQueuedTasks(previous: TaskSummary[], taskIds: string[], updatedAt: string): TaskSummary[] {
  if (taskIds.length === 0) {
    return previous;
  }
  const queuedSet = new Set(taskIds);
  return previous.map((task) => {
    if (!queuedSet.has(task.taskId) || !["created", "queued"].includes(task.status)) {
      return task;
    }
    return {
      ...task,
      status: "queued",
      updatedAt
    };
  });
}

function draftAssistantMessageId(taskId: string): string {
  return `assistant-draft-${taskId}`;
}

function applyAssistantDelta(previous: ChatMessage[], event: RouterSseEvent): ChatMessage[] {
  if (!event.data.message) {
    return previous;
  }
  const draftId = draftAssistantMessageId(event.data.taskId);
  const existingIndex = previous.findIndex((message) => message.id === draftId);
  if (existingIndex < 0) {
    return [
      ...previous,
      {
        id: draftId,
        role: "assistant",
        content: event.data.message,
        createdAt: event.data.createdAt
      }
    ];
  }

  return previous.map((message, index) =>
    index === existingIndex
      ? {
          ...message,
          content: `${message.content}${event.data.message}`,
          createdAt: event.data.createdAt
        }
      : message
  );
}

function finalizeAssistantMessage(previous: ChatMessage[], event: RouterSseEvent): ChatMessage[] {
  const draftId = draftAssistantMessageId(event.data.taskId);
  const draft = previous.find((message) => message.id === draftId) ?? null;
  const baseMessages = previous.filter((message) => message.id !== draftId);
  const content = event.data.message || draft?.content || "";
  if (!content) {
    return baseMessages;
  }

  const messageId = `${event.data.taskId}-${event.data.createdAt}`;
  if (baseMessages.some((message) => message.id === messageId)) {
    return baseMessages;
  }

  return [
    ...baseMessages,
    {
      id: messageId,
      role: "assistant",
      content,
      createdAt: event.data.createdAt
    }
  ];
}

function isRouterPlanEvent(eventName: string): boolean {
  return eventName.startsWith("session.plan.");
}

function parsePlanItems(payload: Record<string, unknown> | undefined): InteractionCard["items"] {
  const items = payload?.items;
  if (!Array.isArray(items)) {
    return undefined;
  }
  const parsed = items
    .filter((item) => item && typeof item === "object")
    .map((item) => {
      const record = item as Record<string, unknown>;
      const intentCode =
        (typeof record.intentCode === "string" && record.intentCode) ||
        (typeof record.intent_code === "string" && record.intent_code) ||
        null;
      if (!intentCode) {
        return null;
      }
      return {
        taskId:
          (typeof record.taskId === "string" && record.taskId) ||
          (typeof record.task_id === "string" && record.task_id) ||
          undefined,
        intentCode,
        title:
          (typeof record.title === "string" && record.title) ||
          intentCode,
        status: typeof record.status === "string" ? record.status : "pending",
        confidence: typeof record.confidence === "number" ? record.confidence : undefined
      };
    })
    .filter((item): item is NonNullable<typeof item> => item !== null);
  return parsed.length > 0 ? parsed : undefined;
}

function mergeRouterPlanCard(previous: InteractionCard | null, event: RouterSseEvent): InteractionCard | null {
  if (!isRouterPlanEvent(event.event)) {
    return previous;
  }
  const fromInteraction = event.data.interaction;
  if (fromInteraction?.source === "router") {
    return fromInteraction;
  }

  const payload = event.data.payload;
  if (!payload) {
    return previous;
  }
  const nextStatus = parsePayloadString(payload, "plan_status") ?? previous?.status;
  const nextItems = parsePlanItems(payload) ?? previous?.items;
  const hasPlanData = nextStatus !== null || nextItems !== undefined || parsePayloadString(payload, "plan_id") !== null;
  if (!hasPlanData) {
    return previous;
  }

  return {
    source: "router",
    type: previous?.type ?? "plan_card",
    cardType: previous?.cardType ?? "plan_confirm",
    status: nextStatus ?? undefined,
    title: previous?.title ?? "执行计划",
    summary: previous?.summary,
    items: nextItems,
    actions: previous?.actions,
    confirmToken: previous?.confirmToken
  };
}

export default function ChatPage() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [custId, setCustId] = useState(DEFAULT_CUST_ID);
  const [composer, setComposer] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [showDiagnostics, setShowDiagnostics] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([BOOT_MESSAGE]);
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [candidates, setCandidates] = useState<CandidateIntent[]>([]);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [sseEvents, setSseEvents] = useState<RouterSseEvent[]>([]);
  const [streamState, setStreamState] = useState("booting");
  const [routerPlanCard, setRouterPlanCard] = useState<InteractionCard | null>(null);
  const [agentCard, setAgentCard] = useState<InteractionCard | null>(null);
  const [isSubmittingAction, setIsSubmittingAction] = useState(false);

  function applySnapshot(snapshot: RouterSnapshot) {
    setMessages(snapshot.messages.length > 0 ? snapshot.messages : [BOOT_MESSAGE]);
    setTasks(snapshot.tasks);
    setCandidates(snapshot.candidateIntents);
    setActiveTaskId(snapshot.activeTaskId ?? null);
    setRouterPlanCard(snapshot.pendingPlan?.source === "router" ? snapshot.pendingPlan : null);
  }

  useEffect(() => {
    let cancelled = false;

    async function boot() {
      try {
        setErrorMessage(null);
        const session = await api.createSession({ custId: DEFAULT_CUST_ID });
        if (cancelled) return;
        setSessionId(session.sessionId);
        setCustId(session.custId);
        setStreamState("connected");
      } catch (error: unknown) {
        if (cancelled) return;
        setErrorMessage(error instanceof Error ? error.message : "初始化会话失败");
        setStreamState("disconnected");
      }
    }

    void boot();

    return () => {
      cancelled = true;
      setStreamState("disconnected");
    };
  }, []);

  const activeTask =
    tasks.find((task) => task.taskId === activeTaskId) ??
    tasks.find((task) => !["completed", "failed", "cancelled"].includes(task.status)) ??
    null;
  const queuedTasks = tasks.filter((task) => task.status === "queued");
  const hasRouterPlan = routerPlanCard?.source === "router";
  const awaitingPlanConfirmation = hasRouterPlan && routerPlanCard?.status === "waiting_confirmation";
  const canSend = composer.trim().length > 0 && !isSending && sessionId !== null && !awaitingPlanConfirmation;
  const hasDiagnostics = queuedTasks.length > 0 || candidates.length > 0 || sseEvents.length > 0;
  const activeTaskStatusLabel = activeTask ? STATUS_LABELS[activeTask.status] : "空闲";
  const routerPlanItems = routerPlanCard?.items ?? [];
  const routerPlanActions = routerPlanCard?.actions ?? [];

  function handleStreamEvent(event: RouterSseEvent) {
    setSseEvents((previous) => [event, ...previous].slice(0, 10));

    if (event.event.startsWith("task.")) {
      setTasks((previous) => upsertTaskSummary(previous, event));
    }

    if (isRouterPlanEvent(event.event)) {
      setRouterPlanCard((previous) => mergeRouterPlanCard(previous, event));
    }

    if (event.data.interaction?.source === "agent") {
      setAgentCard(event.data.interaction);
    }

    if (event.event === "recognition.completed") {
      const nextCandidates = parseCandidateIntents(event.data.payload, "candidates");
      if (nextCandidates.length > 0 || Array.isArray(event.data.payload?.candidates)) {
        setCandidates(nextCandidates);
      }
    }

    if (event.event === "task.dispatching" || event.event === "task.running" || event.event === "task.resuming") {
      setActiveTaskId(event.data.taskId);
    }

    if (event.event === "task.message") {
      setMessages((previous) => applyAssistantDelta(previous, event));
    }

    if (["task.waiting_user_input", "task.waiting_confirmation", "task.completed", "task.failed"].includes(event.event)) {
      setMessages((previous) => finalizeAssistantMessage(previous, event));
      if (event.event === "task.waiting_user_input") {
        setActiveTaskId(event.data.taskId);
      }
    }

    if (
      event.event === "session.recognized" ||
      event.event === "session.waiting_user_input" ||
      event.event === "session.waiting_confirmation" ||
      event.event === "session.idle"
    ) {
      const nextActiveTaskId = parsePayloadString(event.data.payload, "active_task_id");
      const queuedTaskIds = parsePayloadStringArray(event.data.payload, "queued_task_ids");
      const nextCandidates = parseCandidateIntents(event.data.payload, "candidate_intents");

      setActiveTaskId(nextActiveTaskId);
      setTasks((previous) => markQueuedTasks(previous, queuedTaskIds, event.data.createdAt));
      if (nextCandidates.length > 0 || Array.isArray(event.data.payload?.candidate_intents)) {
        setCandidates(nextCandidates);
      }
    }

    if (event.event === "task.completed" || event.event === "task.failed" || event.event === "task.cancelled") {
      setAgentCard(null);
    }
  }

  async function onPlanAction(actionCode: string) {
    if (!sessionId || !hasRouterPlan || !routerPlanCard) return;
    setIsSubmittingAction(true);
    setErrorMessage(null);
    try {
      const snapshot = await api.sendSessionAction({
        sessionId,
        custId,
        taskId: "session",
        source: "router",
        actionCode,
        confirmToken: routerPlanCard.confirmToken,
        payload: { decision: actionCode }
      });
      applySnapshot(snapshot);
    } catch (error: unknown) {
      setErrorMessage(error instanceof Error ? error.message : "计划操作失败");
    } finally {
      setIsSubmittingAction(false);
    }
  }

  async function onSendMessage() {
    if (!canSend || sessionId === null) return;
    const content = composer.trim();
    setComposer("");
    setIsSending(true);
    setErrorMessage(null);

    startTransition(() => {
      setMessages((previous) => [
        ...previous,
        {
          id: createLocalMessageId(),
          role: "user",
          content,
          createdAt: new Date().toISOString()
        }
      ]);
    });

    let streamError: string | null = null;
    try {
      await api.sendMessageStream({ sessionId, content, custId }, { onEvent: handleStreamEvent });
      setStreamState("connected");
    } catch (error: unknown) {
      streamError = error instanceof Error ? error.message : "发送消息失败";
      setStreamState("disconnected");
    } finally {
      if (streamError) {
        setErrorMessage(streamError);
      }
      setIsSending(false);
    }
  }

  return (
    <div className="shell">
      <header className="masthead">
        <div className="brand-copy">
          <p className="eyebrow">Intent Router</p>
          <h1>中文对话，清楚路由。</h1>
          <p className="masthead-copy">主界面只保留会话、当前任务和发送动作，诊断信息按需展开。</p>
        </div>
        <div className="masthead-actions">
          <div className="status-row">
            <Badge label={STREAM_STATE_LABELS[streamState] ?? streamState} tone={toneForStreamState(streamState)} />
            <Badge label={sessionId ? "会话已就绪" : "创建会话中"} tone={sessionId ? "success" : "emphasis"} />
          </div>
          <button className="toggle-button" onClick={() => setShowDiagnostics((value) => !value)} type="button">
            {showDiagnostics ? "收起诊断" : "查看诊断"}
          </button>
        </div>
      </header>

      <main className="workspace">
        <section className="conversation-stage">
          <header className="stage-topline">
            <div>
              <p className="section-label">当前会话</p>
              <h2>对话窗口</h2>
            </div>
            <div className="glance-strip" aria-label="会话速览">
              <div className="glance-item">
                <span>当前任务</span>
                <strong>{activeTask ? activeTask.intentCode : "等待识别"}</strong>
              </div>
              <div className="glance-item">
                <span>排队任务</span>
                <strong>{queuedTasks.length}</strong>
              </div>
            </div>
          </header>

          <div className="message-list" aria-live="polite">
            {messages.map((message) => (
              <article key={message.id} className={`message ${message.role === "user" ? "user" : ""}`}>
                <div className="meta">
                  {roleLabel(message.role)}
                  {formatTime(message.createdAt) ? ` · ${formatTime(message.createdAt)}` : ""}
                </div>
                <p>{message.content}</p>
              </article>
            ))}
          </div>

          {errorMessage ? <p className="hint-text" role="alert">{errorMessage}</p> : null}

          <div className="composer">
            <label className="composer-label" htmlFor="chat-composer">
              输入消息
            </label>
            <textarea
              id="chat-composer"
              placeholder={awaitingPlanConfirmation ? "请先确认或取消当前执行计划" : "例如：帮我查下订单，再帮我取消明天的预约"}
              disabled={awaitingPlanConfirmation}
              value={composer}
              onChange={(event) => setComposer(event.target.value)}
            />
            <div className="composer-foot">
              <small className="hint-text">
                {awaitingPlanConfirmation ? "当前存在待确认的执行计划，请先处理 Router 规划卡片。" : "支持一次输入多个诉求，例如“查订单后，再取消明天的预约”。"}
              </small>
              <button disabled={!canSend || isSubmittingAction} onClick={onSendMessage} type="button">
                {isSending ? "发送中..." : "发送消息"}
              </button>
            </div>
          </div>
        </section>

        <aside className="context-rail">
          {hasRouterPlan ? (
            <section className="rail-section plan-section">
              <div className="section-head">
                <p className="section-label">Router 规划</p>
                <Badge
                  label={planStatusLabel(routerPlanCard?.status)}
                  tone={
                    routerPlanCard?.status === "waiting_confirmation"
                      ? "emphasis"
                      : routerPlanCard?.status === "completed"
                        ? "success"
                        : "default"
                  }
                />
              </div>
              <h3>{routerPlanCard?.title ?? "请确认执行计划"}</h3>
              {routerPlanCard?.summary ? <p className="status-copy">{routerPlanCard.summary}</p> : null}
              {routerPlanItems.length > 0 ? (
                <ol className="plan-list">
                  {routerPlanItems.map((item) => (
                    <li key={item.taskId ?? `${item.intentCode}-${item.title}`}>
                      <span>{item.title}</span>
                      <small className={`plan-item-status status-${item.status}`}>{planItemStatusLabel(item.status)}</small>
                    </li>
                  ))}
                </ol>
              ) : null}
              {routerPlanCard?.status === "waiting_confirmation" ? (
                <div className="plan-actions">
                  {routerPlanActions
                    .filter((action) => action.code === "confirm_plan" || action.code === "cancel_plan")
                    .map((action) => (
                      <button
                        key={action.code}
                        className={action.code === "confirm_plan" ? "plan-confirm" : "plan-cancel"}
                        disabled={isSubmittingAction}
                        onClick={() => onPlanAction(action.code)}
                        type="button"
                      >
                        {isSubmittingAction ? "提交中..." : action.label}
                      </button>
                    ))}
                </div>
              ) : null}
            </section>
          ) : null}

          <section className={`rail-section ${activeTask ? `tone-${toneForStatus(activeTask.status)}` : ""}`}>
            <div className="section-head">
              <p className="section-label">路由状态</p>
              <Badge label={activeTaskStatusLabel} tone={activeTask ? toneForStatus(activeTask.status) : "default"} />
            </div>
            {activeTask ? (
              <>
                <h3>{activeTask.intentCode}</h3>
                <p className="status-copy">
                  当前任务已经进入对应 agent。置信度 {activeTask.confidence.toFixed(2)}
                  {queuedTasks.length > 0 ? `，后面还有 ${queuedTasks.length} 个排队任务。` : "。"}
                </p>
                {showDiagnostics ? <small className="mono">{activeTask.taskId}</small> : null}
              </>
            ) : (
              <>
                <h3>等待下一条消息</h3>
                <p className="status-copy">收到新消息后，router 会先识别意图，再把任务分发给对应 agent。</p>
              </>
            )}
          </section>

          {agentCard ? (
            <section className="rail-section">
              <div className="section-head">
                <p className="section-label">业务交互</p>
                <Badge label="Agent Card" tone="warning" />
              </div>
              <h3>{agentCard.title ?? "等待业务确认"}</h3>
              <p className="status-copy">
                当前任务由业务 agent 发起结构化卡片。该区域仅做预留展示，具体表单交互将在后端 action 协议稳定后接入。
              </p>
            </section>
          ) : null}
        </aside>
      </main>

      {showDiagnostics ? (
        <section className="diagnostic-sheet">
          <header className="diagnostic-head">
            <div>
              <p className="eyebrow">诊断</p>
              <h2>路由明细</h2>
            </div>
            {sessionId ? <small className="mono">{sessionId}</small> : null}
          </header>

          {hasDiagnostics ? (
            <div className="diagnostic-grid">
              {queuedTasks.length > 0 ? (
                <section className="diagnostic-section">
                  <h3>排队任务</h3>
                  <div className="stack">
                    {queuedTasks.map((task) => (
                      <div key={task.taskId} className="diagnostic-item">
                        <div className="line-item">
                          <strong>{task.intentCode}</strong>
                          <small>{STATUS_LABELS[task.status]}</small>
                        </div>
                        <small className="mono">{task.taskId}</small>
                      </div>
                    ))}
                  </div>
                </section>
              ) : null}

              {candidates.length > 0 ? (
                <section className="diagnostic-section">
                  <h3>候选意图</h3>
                  <div className="stack">
                    {candidates.map((candidate) => (
                      <div key={candidate.intentCode} className="diagnostic-item">
                        <div className="line-item">
                          <strong>{candidate.intentCode}</strong>
                          <small>{candidate.confidence.toFixed(2)}</small>
                        </div>
                        <small>{candidate.reason}</small>
                      </div>
                    ))}
                  </div>
                </section>
              ) : null}

              {sseEvents.length > 0 ? (
                <section className="diagnostic-section">
                  <h3>最近事件</h3>
                  <div className="stack">
                    {sseEvents.map((event, index) => (
                      <div key={`${event.at}-${index}`} className="diagnostic-item">
                        <div className="line-item">
                          <strong>{eventLabel(event.event)}</strong>
                          <small>{formatTime(event.at)}</small>
                        </div>
                        <small>
                          {event.data.intentCode} · {STATUS_LABELS[event.data.status]}
                        </small>
                        {event.data.message ? <small>{event.data.message}</small> : null}
                      </div>
                    ))}
                  </div>
                </section>
              ) : null}
            </div>
          ) : (
            <div className="diagnostic-empty">
              <strong>目前没有额外诊断信号。</strong>
              <p>当前会话运行正常，没有排队任务、候选冲突或事件堆积需要查看。</p>
            </div>
          )}
        </section>
      ) : null}
    </div>
  );
}
