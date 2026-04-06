"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

type MessageRole = "user" | "assistant" | "system";

type BackendMessage = {
  role: MessageRole;
  content: string;
  created_at: string;
};

type BackendCandidateIntent = {
  intent_code: string;
  confidence: number;
  reason: string;
};

type BackendGraphNode = {
  node_id: string;
  intent_code: string;
  title: string;
  confidence: number;
  position: number;
  status: string;
  task_id?: string | null;
  depends_on?: string[];
  blocking_reason?: string | null;
  relation_reason?: string | null;
  updated_at?: string;
};

type BackendGraphEdge = {
  edge_id: string;
  source_node_id: string;
  target_node_id: string;
  relation_type: string;
  label?: string | null;
};

type BackendGraph = {
  graph_id: string;
  source_message: string;
  summary: string;
  version: number;
  status: string;
  confirm_token?: string | null;
  nodes: BackendGraphNode[];
  edges: BackendGraphEdge[];
  actions?: Array<{ code: string; label: string }>;
};

type BackendSnapshot = {
  session_id: string;
  cust_id: string;
  messages: BackendMessage[];
  candidate_intents: BackendCandidateIntent[];
  current_graph?: BackendGraph | null;
  pending_graph?: BackendGraph | null;
  active_node_id?: string | null;
  expires_at?: string;
};

type BackendTaskEvent = {
  task_id?: string;
  session_id?: string;
  intent_code?: string;
  status?: string;
  message?: string | null;
  payload?: {
    graph?: BackendGraph;
    pending_graph?: BackendGraph;
    node?: BackendGraphNode;
    active_node_id?: string;
    candidate_intents?: BackendCandidateIntent[];
  };
  created_at?: string;
};

type CreateSessionResponse = {
  session_id: string;
  cust_id: string;
};

type TimelineEntry = {
  id: string;
  event: string;
  message: string;
  created_at: string;
};

const API_BASE = "/api/router/v2";
const BOOT_MESSAGE: BackendMessage = {
  role: "assistant",
  content: "V2 已就绪。你可以一次输入多个诉求，系统会先识别多意图，再生成动态执行图。",
  created_at: new Date().toISOString(),
};

const GRAPH_STATUS_LABELS: Record<string, string> = {
  draft: "草稿",
  waiting_confirmation: "待确认",
  running: "执行中",
  waiting_user_input: "待补充",
  waiting_confirmation_node: "待节点确认",
  partially_completed: "部分完成",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const NODE_STATUS_LABELS: Record<string, string> = {
  draft: "草稿",
  blocked: "阻塞",
  ready: "就绪",
  running: "执行中",
  waiting_user_input: "待补充",
  waiting_confirmation: "待确认",
  completed: "完成",
  failed: "失败",
  cancelled: "取消",
  skipped: "跳过",
};

const EVENT_LABELS: Record<string, string> = {
  "recognition.started": "开始识别",
  "recognition.delta": "识别流输出",
  "recognition.completed": "识别完成",
  "graph.proposed": "执行图已生成",
  "graph.waiting_confirmation": "等待确认执行图",
  "graph.confirmed": "执行图已确认",
  "graph.updated": "执行图更新",
  "graph.completed": "执行图完成",
  "graph.failed": "执行图失败",
  "graph.cancelled": "执行图取消",
  "node.created": "节点创建",
  "node.dispatching": "节点分发",
  "node.running": "节点执行",
  "node.message": "节点消息",
  "node.resuming": "节点恢复",
  "node.waiting_user_input": "节点待补充",
  "node.waiting_confirmation": "节点待确认",
  "node.completed": "节点完成",
  "node.failed": "节点失败",
  "node.cancelled": "节点取消",
  "graph.unrecognized": "未识别到明确事项",
  "session.waiting_user_input": "会话待补充",
  "session.waiting_confirmation": "会话待确认",
  "session.idle": "会话空闲",
  heartbeat: "心跳",
};

function statusLabel(status: string | undefined, labels: Record<string, string>): string {
  if (!status) {
    return "未知";
  }
  return labels[status] ?? status;
}

function formatTime(value: string | undefined): string | null {
  if (!value) {
    return null;
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return null;
  }
  return date.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function eventLabel(event: string): string {
  return EVENT_LABELS[event] ?? event;
}

function appendBootMessage(messages: BackendMessage[] | undefined): BackendMessage[] {
  if (!messages || messages.length === 0) {
    return [BOOT_MESSAGE];
  }
  return messages;
}

function mergeSnapshotFromEvent(
  previous: BackendSnapshot | null,
  eventName: string,
  event: BackendTaskEvent,
): BackendSnapshot | null {
  if (previous === null) {
    return previous;
  }
  const payload = event.payload ?? {};
  const next: BackendSnapshot = { ...previous };

  if (payload.candidate_intents) {
    next.candidate_intents = payload.candidate_intents;
  }
  if (payload.active_node_id) {
    next.active_node_id = payload.active_node_id;
  }

  if (eventName === "graph.proposed" || eventName === "graph.waiting_confirmation") {
    if (payload.graph) {
      next.pending_graph = payload.graph;
    }
    return next;
  }

  if (eventName === "graph.confirmed") {
    next.pending_graph = null;
  }

  if (payload.pending_graph) {
    next.pending_graph = payload.pending_graph;
  }
  if (payload.graph) {
    next.current_graph = payload.graph;
  }
  if (payload.node && ["node.running", "node.waiting_user_input", "node.waiting_confirmation"].includes(eventName)) {
    next.active_node_id = payload.node.node_id;
  }

  return next;
}

export default function ChatV2Page() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [custId, setCustId] = useState<string>("cust_demo");
  const [snapshot, setSnapshot] = useState<BackendSnapshot | null>(null);
  const [messages, setMessages] = useState<BackendMessage[]>([BOOT_MESSAGE]);
  const [timeline, setTimeline] = useState<TimelineEntry[]>([]);
  const [composer, setComposer] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [isSubmittingGraphAction, setIsSubmittingGraphAction] = useState(false);
  const [isCancellingNode, setIsCancellingNode] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const eventSourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function boot() {
      try {
        const response = await fetch(`${API_BASE}/sessions`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
        });
        if (!response.ok) {
          throw new Error(await response.text());
        }
        const session = (await response.json()) as CreateSessionResponse;
        if (cancelled) {
          return;
        }
        setSessionId(session.session_id);
        setCustId(session.cust_id);
        await refreshSnapshot(session.session_id);
        subscribe(session.session_id);
      } catch (error: unknown) {
        if (cancelled) {
          return;
        }
        setErrorMessage(error instanceof Error ? error.message : "初始化 V2 会话失败");
      }
    }

    void boot();
    return () => {
      cancelled = true;
      eventSourceRef.current?.close();
    };
  }, []);

  async function refreshSnapshot(nextSessionId: string) {
    const response = await fetch(`${API_BASE}/sessions/${nextSessionId}`);
    if (!response.ok) {
      throw new Error(await response.text());
    }
    const body = (await response.json()) as BackendSnapshot;
    setSnapshot(body);
    setMessages(appendBootMessage(body.messages));
  }

  function subscribe(nextSessionId: string) {
    eventSourceRef.current?.close();
    const source = new EventSource(`${API_BASE}/sessions/${nextSessionId}/events`);
    eventSourceRef.current = source;

    const forward = (eventName: string, rawEvent: MessageEvent<string>) => {
      try {
        const data = JSON.parse(rawEvent.data) as BackendTaskEvent;
        setTimeline((previous) => [
          {
            id: `${eventName}-${data.created_at ?? new Date().toISOString()}-${previous.length}`,
            event: eventName,
            message: data.message ?? eventLabel(eventName),
            created_at: data.created_at ?? new Date().toISOString(),
          },
          ...previous,
        ].slice(0, 40));
        setSnapshot((previous) => mergeSnapshotFromEvent(previous, eventName, data));
      } catch {
        // Ignore malformed SSE frames instead of breaking the live view.
      }
    };

    Object.keys(EVENT_LABELS).forEach((eventName) => {
      source.addEventListener(eventName, (event) => forward(eventName, event as MessageEvent<string>));
    });
  }

  async function onSendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!sessionId || !composer.trim()) {
      return;
    }
    setIsSending(true);
    setErrorMessage(null);
    try {
      const response = await fetch(`${API_BASE}/sessions/${sessionId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: composer, cust_id: custId }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const payload = (await response.json()) as { snapshot: BackendSnapshot };
      setSnapshot(payload.snapshot);
      setMessages(appendBootMessage(payload.snapshot.messages));
      setComposer("");
    } catch (error: unknown) {
      setErrorMessage(error instanceof Error ? error.message : "发送消息失败");
    } finally {
      setIsSending(false);
    }
  }

  async function submitGraphAction(actionCode: "confirm_graph" | "cancel_graph") {
    if (!sessionId || !snapshot?.pending_graph) {
      return;
    }
    setIsSubmittingGraphAction(true);
    setErrorMessage(null);
    try {
      const response = await fetch(`${API_BASE}/sessions/${sessionId}/actions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_id: snapshot.pending_graph.graph_id,
          source: "router",
          action_code: actionCode,
          confirm_token: actionCode === "confirm_graph" ? snapshot.pending_graph.confirm_token : undefined,
          cust_id: custId,
        }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const payload = (await response.json()) as { snapshot: BackendSnapshot };
      setSnapshot(payload.snapshot);
      setMessages(appendBootMessage(payload.snapshot.messages));
    } catch (error: unknown) {
      setErrorMessage(error instanceof Error ? error.message : "执行图操作失败");
    } finally {
      setIsSubmittingGraphAction(false);
    }
  }

  async function cancelCurrentNode() {
    if (!sessionId || !snapshot?.active_node_id) {
      return;
    }
    setIsCancellingNode(true);
    setErrorMessage(null);
    try {
      const response = await fetch(`${API_BASE}/sessions/${sessionId}/actions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_id: snapshot.active_node_id,
          source: "router",
          action_code: "cancel_node",
          payload: { reason: "用户主动取消当前节点" },
          cust_id: custId,
        }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const payload = (await response.json()) as { snapshot: BackendSnapshot };
      setSnapshot(payload.snapshot);
      setMessages(appendBootMessage(payload.snapshot.messages));
    } catch (error: unknown) {
      setErrorMessage(error instanceof Error ? error.message : "取消节点失败");
    } finally {
      setIsCancellingNode(false);
    }
  }

  const currentGraph = snapshot?.current_graph ?? null;
  const pendingGraph = snapshot?.pending_graph ?? null;
  const displayedGraph = currentGraph ?? pendingGraph;
  const activeNode =
    currentGraph?.nodes.find((node) => node.node_id === snapshot?.active_node_id) ?? null;
  const candidateIntents = snapshot?.candidate_intents ?? [];

  return (
    <div className="shell">
      <section className="conversation-stage">
        <header className="stage-topline">
          <div>
            <p className="section-label">V2 Entry</p>
            <h2>Dynamic Intent Graph</h2>
          </div>
          <div className="glance-strip" aria-label="V2 概览">
            <div className="glance-item">
              <span>当前图</span>
              <strong>{statusLabel(currentGraph?.status, GRAPH_STATUS_LABELS)}</strong>
            </div>
            <div className="glance-item">
              <span>待确认图</span>
              <strong>{pendingGraph ? "有" : "无"}</strong>
            </div>
            <div className="glance-item">
              <span>活跃节点</span>
              <strong>{activeNode?.title ?? "无"}</strong>
            </div>
          </div>
        </header>

        <div className="message-list" aria-live="polite">
          {messages.map((message, index) => (
            <article
              key={`${message.role}-${message.created_at}-${index}`}
              className={`message ${message.role === "user" ? "user" : ""}`.trim()}
            >
              <div className="meta">
                {message.role === "user" ? "你" : message.role === "assistant" ? "助手" : "系统"}
                {formatTime(message.created_at) ? ` · ${formatTime(message.created_at)}` : ""}
              </div>
              <p>{message.content}</p>
            </article>
          ))}
        </div>

        {errorMessage ? (
          <p className="hint-text" role="alert">
            {errorMessage}
          </p>
        ) : null}

        <form className="composer" onSubmit={onSendMessage}>
          <label className="composer-label" htmlFor="chat-v2-composer">
            输入消息
          </label>
          <textarea
            id="chat-v2-composer"
            placeholder="例如：先查余额，如果没问题就给张三转账 200 元。"
            value={composer}
            onChange={(event) => setComposer(event.target.value)}
          />
          <div className="composer-foot">
            <small className="hint-text">V2 会先做多意图识别，再生成 graph，并处理等待、取消和切换。</small>
            <button type="submit" disabled={isSending || sessionId === null || !composer.trim()}>
              {isSending ? "发送中..." : "发送消息"}
            </button>
          </div>
        </form>
      </section>

      <aside className="context-rail">
        {pendingGraph ? (
          <section className="rail-section plan-section">
            <div className="section-head">
              <p className="section-label">待确认执行图</p>
            </div>
            <h3>{statusLabel(pendingGraph.status, GRAPH_STATUS_LABELS)}</h3>
            <p className="status-copy">{pendingGraph.summary}</p>
            <ol className="plan-list">
              {pendingGraph.nodes.map((node) => (
                <li key={node.node_id}>
                  <span>{node.title}</span>
                  <small className={`plan-item-status status-${node.status}`}>
                    {statusLabel(node.status, NODE_STATUS_LABELS)}
                  </small>
                </li>
              ))}
            </ol>
            <div className="plan-actions">
              <button
                className="plan-confirm"
                disabled={isSubmittingGraphAction}
                onClick={() => void submitGraphAction("confirm_graph")}
                type="button"
              >
                {isSubmittingGraphAction ? "提交中..." : "确认执行"}
              </button>
              <button
                className="plan-cancel"
                disabled={isSubmittingGraphAction}
                onClick={() => void submitGraphAction("cancel_graph")}
                type="button"
              >
                取消计划
              </button>
            </div>
          </section>
        ) : null}

        <section className="rail-section">
          <div className="section-head">
            <p className="section-label">当前执行图</p>
          </div>
          <h3>{displayedGraph ? statusLabel(displayedGraph.status, GRAPH_STATUS_LABELS) : "尚未生成"}</h3>
          <p className="status-copy">
            {displayedGraph?.summary ?? "发送消息后，V2 会先做多意图识别，再生成 graph 并推进节点执行。"}
          </p>
          {snapshot?.active_node_id && currentGraph ? (
            <button
              className="toggle-button"
              disabled={isCancellingNode}
              onClick={() => void cancelCurrentNode()}
              type="button"
            >
              {isCancellingNode ? "取消中..." : "取消当前节点"}
            </button>
          ) : null}
          {sessionId ? <small className="mono">{sessionId}</small> : null}
        </section>

        <section className="rail-section">
          <div className="section-head">
            <p className="section-label">节点</p>
          </div>
          {displayedGraph ? (
            <div className="stack">
              {displayedGraph.nodes.map((node) => (
                <div key={node.node_id} className="diagnostic-item">
                  <div className="line-item">
                    <strong>{node.title}</strong>
                    <small>{statusLabel(node.status, NODE_STATUS_LABELS)}</small>
                  </div>
                  <small>{node.intent_code}</small>
                  {node.relation_reason ? <small>{node.relation_reason}</small> : null}
                  {node.blocking_reason ? <small>{node.blocking_reason}</small> : null}
                </div>
              ))}
            </div>
          ) : (
            <p className="status-copy">当前还没有节点。</p>
          )}
        </section>

        <section className="rail-section">
          <div className="section-head">
            <p className="section-label">边</p>
          </div>
          {displayedGraph && displayedGraph.edges.length > 0 ? (
            <div className="stack">
              {displayedGraph.edges.map((edge) => (
                <div key={edge.edge_id} className="diagnostic-item">
                  <div className="line-item">
                    <strong>{edge.relation_type}</strong>
                    <small>{edge.label ?? "依赖边"}</small>
                  </div>
                  <small>
                    {edge.source_node_id} → {edge.target_node_id}
                  </small>
                </div>
              ))}
            </div>
          ) : (
            <p className="status-copy">当前图没有显式依赖边，或图尚未创建。</p>
          )}
        </section>

        <section className="rail-section">
          <div className="section-head">
            <p className="section-label">候选意图</p>
          </div>
          {candidateIntents.length > 0 ? (
            <div className="stack">
              {candidateIntents.map((candidate) => (
                <div key={candidate.intent_code} className="diagnostic-item">
                  <div className="line-item">
                    <strong>{candidate.intent_code}</strong>
                    <small>{candidate.confidence.toFixed(2)}</small>
                  </div>
                  <small>{candidate.reason}</small>
                </div>
              ))}
            </div>
          ) : (
            <p className="status-copy">当前没有额外候选意图。</p>
          )}
        </section>

        <section className="rail-section">
          <div className="section-head">
            <p className="section-label">事件时间线</p>
          </div>
          {timeline.length > 0 ? (
            <div className="stack">
              {timeline.map((entry) => (
                <div key={entry.id} className="diagnostic-item">
                  <div className="line-item">
                    <strong>{eventLabel(entry.event)}</strong>
                    <small>{formatTime(entry.created_at)}</small>
                  </div>
                  <small>{entry.message}</small>
                </div>
              ))}
            </div>
          ) : (
            <p className="status-copy">SSE 已就绪，事件会在这里持续滚动展示。</p>
          )}
        </section>
      </aside>
    </div>
  );
}
