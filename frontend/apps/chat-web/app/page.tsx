"use client";

import { startTransition, useEffect, useState } from "react";
import { IntentRouterApiClient } from "@intent-router/api-client";
import type {
  CandidateIntent,
  ChatMessage,
  RouterSnapshot,
  RouterSseEvent,
  TaskSummary
} from "@intent-router/shared-types";
import { Badge, Divider, Panel, SectionHeading } from "@intent-router/ui";

const api = new IntentRouterApiClient();

function toneForStatus(status?: TaskSummary["status"]): "default" | "warning" | "success" | "emphasis" {
  if (status === "completed") return "success";
  if (status === "failed" || status === "waiting_user_input") return "warning";
  if (status === "running" || status === "dispatching" || status === "resuming") return "emphasis";
  return "default";
}

function buildAssistantMessage(event: RouterSseEvent): ChatMessage | null {
  if (!event.data.message) return null;
  if (!["task.waiting_user_input", "task.completed", "task.failed"].includes(event.event)) return null;
  return {
    id: `${event.data.taskId}-${event.data.createdAt}`,
    role: "assistant",
    content: event.data.message,
    createdAt: event.data.createdAt
  };
}

export default function ChatPage() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [custId, setCustId] = useState("cust_demo_001");
  const [composer, setComposer] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: "assistant-boot",
      role: "assistant",
      content: "Intent Router online. Try a multi-intent message such as “帮我查下订单，再帮我取消明天的预约”。",
      createdAt: new Date().toISOString()
    }
  ]);
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [candidates, setCandidates] = useState<CandidateIntent[]>([]);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [sseEvents, setSseEvents] = useState<RouterSseEvent[]>([]);
  const [streamState, setStreamState] = useState("booting");

  async function refreshSnapshot(targetSessionId: string) {
    const snapshot = await api.getSession(targetSessionId);
    hydrateSnapshot(snapshot);
  }

  function hydrateSnapshot(snapshot: RouterSnapshot) {
    setTasks(snapshot.tasks);
    setCandidates(snapshot.candidateIntents);
    setActiveTaskId(snapshot.activeTaskId ?? null);
    setMessages((previous) => {
      const initialAssistant = previous.filter((message) => message.id === "assistant-boot");
      const serverMessages = snapshot.messages;
      const clientAssistants = previous.filter(
        (message) =>
          message.role === "assistant" &&
          message.id !== "assistant-boot" &&
          !serverMessages.some(
            (serverMessage) => serverMessage.content === message.content && serverMessage.createdAt === message.createdAt
          )
      );
      return [...initialAssistant, ...serverMessages, ...clientAssistants];
    });
  }

  useEffect(() => {
    let unsubscribe: (() => void) | undefined;
    let cancelled = false;

    async function boot() {
      const session = await api.createSession({ custId });
      if (cancelled) return;
      setSessionId(session.sessionId);
      setCustId(session.custId);
      setStreamState("connecting");
      await refreshSnapshot(session.sessionId);
      unsubscribe = api.subscribeSession(session.sessionId, (event) => {
        setStreamState("connected");
        setSseEvents((previous) => [event, ...previous].slice(0, 10));
        const assistantMessage = buildAssistantMessage(event);
        if (assistantMessage) {
          setMessages((previous) => {
            if (previous.some((message) => message.id === assistantMessage.id)) {
              return previous;
            }
            return [...previous, assistantMessage];
          });
        }
        void refreshSnapshot(session.sessionId);
      });
    }

    void boot();

    return () => {
      cancelled = true;
      if (unsubscribe) unsubscribe();
      setStreamState("disconnected");
    };
  }, [custId]);

  const activeTask =
    tasks.find((task) => task.taskId === activeTaskId) ??
    tasks.find((task) => !["completed", "failed", "cancelled"].includes(task.status)) ??
    null;
  const queuedTasks = tasks.filter((task) => task.status === "queued");
  const canSend = composer.trim().length > 0 && !isSending && sessionId !== null;

  async function onSendMessage() {
    if (!canSend || sessionId === null) return;
    const content = composer.trim();
    setComposer("");
    setIsSending(true);

    startTransition(() => {
      setMessages((previous) => [
        ...previous,
        {
          id: crypto.randomUUID(),
          role: "user",
          content,
          createdAt: new Date().toISOString()
        }
      ]);
    });

    try {
      const snapshot = await api.sendMessage({ sessionId, content, custId });
      hydrateSnapshot(snapshot);
    } finally {
      setIsSending(false);
    }
  }

  return (
    <div className="shell">
      <header className="topbar">
        <div>
          <h1>Intent Router Chat Cockpit</h1>
          <p>Serial multi-intent routing, task handover, and SSE state visibility.</p>
        </div>
        <div className="topbar-meta">
          <Badge label={`stream ${streamState}`} tone={streamState === "connected" ? "success" : "warning"} />
          <small className="mono">{custId}</small>
          {sessionId ? <small className="mono">{sessionId}</small> : null}
        </div>
      </header>

      <main className="main-grid">
        <Panel tone="emphasis" title="Conversation">
          <SectionHeading
            title="Message Stream"
            subtitle="Short-term memory is session scoped for 30 minutes; long-term memory is customer scoped."
          />
          <Divider />
          <div className="message-list">
            {messages.map((message) => (
              <article key={message.id} className={`message ${message.role === "user" ? "user" : ""}`}>
                <div className="meta">
                  {message.role} · {new Date(message.createdAt).toLocaleTimeString()}
                </div>
                <p>{message.content}</p>
              </article>
            ))}
          </div>

          <Divider />
          <div className="composer">
            <textarea
              placeholder="例如：帮我查下订单，再帮我取消明天的预约"
              value={composer}
              onChange={(event) => setComposer(event.target.value)}
            />
            <button disabled={!canSend} onClick={onSendMessage} type="button">
              {isSending ? "Dispatching..." : "Send Message"}
            </button>
          </div>
        </Panel>

        <div className="stack">
          <Panel tone={toneForStatus(activeTask?.status)} title="Active Task">
            {activeTask ? (
              <>
                <div className="line-item">
                  <strong>{activeTask.intentCode}</strong>
                  <Badge label={activeTask.status} tone={toneForStatus(activeTask.status)} />
                </div>
                <small className="mono">{activeTask.taskId}</small>
                <small>Confidence: {activeTask.confidence.toFixed(2)}</small>
              </>
            ) : (
              <small>No active task. Start with a user message.</small>
            )}
          </Panel>

          <Panel title="Queued Tasks">
            <div className="stack">
              {queuedTasks.length === 0 ? (
                <small>No queued tasks.</small>
              ) : (
                queuedTasks.map((task) => (
                  <div key={task.taskId} className="stack-tight">
                    <div className="line-item">
                      <strong>{task.intentCode}</strong>
                      <Badge label={task.status} />
                    </div>
                    <small className="mono">{task.taskId}</small>
                  </div>
                ))
              )}
            </div>
          </Panel>

          <Panel title="Candidate Intents">
            <div className="stack">
              {candidates.length === 0 ? (
                <small>No candidates above threshold.</small>
              ) : (
                candidates.map((candidate) => (
                  <div key={candidate.intentCode} className="stack-tight">
                    <div className="line-item">
                      <strong>{candidate.intentCode}</strong>
                      <small>{candidate.confidence.toFixed(2)}</small>
                    </div>
                    <small>{candidate.reason}</small>
                  </div>
                ))
              )}
            </div>
          </Panel>

          <Panel title="SSE Feed">
            <div className="stack">
              {sseEvents.length === 0 ? (
                <small>Waiting for router events.</small>
              ) : (
                sseEvents.map((event, index) => (
                  <div key={`${event.at}-${index}`} className="stack-tight">
                    <div className="line-item">
                      <strong>{event.event}</strong>
                      <small>{new Date(event.at).toLocaleTimeString()}</small>
                    </div>
                    <small>
                      {event.data.intentCode} · {event.data.status}
                    </small>
                    {event.data.message ? <small>{event.data.message}</small> : null}
                  </div>
                ))
              )}
            </div>
          </Panel>
        </div>
      </main>
    </div>
  );
}
