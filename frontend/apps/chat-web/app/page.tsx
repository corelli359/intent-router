"use client";

import { startTransition, useEffect, useState } from "react";
import { IntentRouterApiClient } from "@intent-router/api-client";
import type {
  CandidateIntent,
  ChatMessage,
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
  "task.completed": "任务已完成",
  "task.failed": "任务失败",
  "session.recognized": "已完成意图识别",
  "session.idle": "会话空闲",
  "session.waiting_user_input": "会话等待输入"
};

function toneForStatus(status?: TaskSummary["status"]): "default" | "warning" | "success" | "emphasis" {
  if (status === "completed") return "success";
  if (status === "failed" || status === "waiting_user_input") return "warning";
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
  const canSend = composer.trim().length > 0 && !isSending && sessionId !== null;
  const hasDiagnostics = queuedTasks.length > 0 || candidates.length > 0 || sseEvents.length > 0;
  const activeTaskStatusLabel = activeTask ? STATUS_LABELS[activeTask.status] : "空闲";

  function handleStreamEvent(event: RouterSseEvent) {
    setSseEvents((previous) => [event, ...previous].slice(0, 10));

    if (event.event.startsWith("task.")) {
      setTasks((previous) => upsertTaskSummary(previous, event));
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

    if (["task.waiting_user_input", "task.completed", "task.failed"].includes(event.event)) {
      setMessages((previous) => finalizeAssistantMessage(previous, event));
      if (event.event === "task.waiting_user_input") {
        setActiveTaskId(event.data.taskId);
      }
    }

    if (event.event === "session.recognized" || event.event === "session.waiting_user_input" || event.event === "session.idle") {
      const nextActiveTaskId = parsePayloadString(event.data.payload, "active_task_id");
      const queuedTaskIds = parsePayloadStringArray(event.data.payload, "queued_task_ids");
      const nextCandidates = parseCandidateIntents(event.data.payload, "candidate_intents");

      setActiveTaskId(nextActiveTaskId);
      setTasks((previous) => markQueuedTasks(previous, queuedTaskIds, event.data.createdAt));
      if (nextCandidates.length > 0 || Array.isArray(event.data.payload?.candidate_intents)) {
        setCandidates(nextCandidates);
      }
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
              placeholder="例如：帮我查下订单，再帮我取消明天的预约"
              value={composer}
              onChange={(event) => setComposer(event.target.value)}
            />
            <div className="composer-foot">
              <small className="hint-text">支持一次输入多个诉求，例如“查订单后，再取消明天的预约”。</small>
              <button disabled={!canSend} onClick={onSendMessage} type="button">
                {isSending ? "发送中..." : "发送消息"}
              </button>
            </div>
          </div>
        </section>

        <aside className="context-rail">
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

          <section className="rail-section">
            <div className="section-head">
              <p className="section-label">会话信息</p>
              <Badge label={sessionId ? "已建立" : "初始化中"} tone={sessionId ? "success" : "emphasis"} />
            </div>
            <h3>{custId}</h3>
            <p className="status-copy">后续消息会沿用当前会话上下文。详细 trace 放在下方的诊断区，不挤占主界面。</p>
            {showDiagnostics && sessionId ? <small className="mono">{sessionId}</small> : null}
          </section>
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
