"use client";

import { startTransition, useEffect, useRef, useState, type FormEvent } from "react";
import { Badge } from "@intent-router/ui";

type MessageRole = "user" | "assistant" | "system";
type StreamState = "booting" | "connecting" | "connected" | "disconnected";

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
  slot_memory?: Record<string, unknown>;
  output_payload?: Record<string, unknown>;
  updated_at?: string;
};

type BackendGraphEdge = {
  edge_id: string;
  source_node_id: string;
  target_node_id: string;
  relation_type: string;
  label?: string | null;
  condition?: {
    left_key?: string | null;
    operator?: string | null;
    right_value?: string | number | boolean | null;
  } | null;
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

type GuidedFieldTemplate = {
  key: string;
  label: string;
  placeholder: string;
  inputMode?: "text" | "numeric" | "decimal";
  transform?: "uppercase";
};

type GuidedIntentTemplate = {
  intentCode: string;
  title: string;
  description: string;
  sourceFragment: string;
  fields: GuidedFieldTemplate[];
};

type GuidedIntentDraft = GuidedIntentTemplate & {
  selected: boolean;
  slotMemory: Record<string, string>;
};

const API_BASE = "/api/router/v2";
const DEFAULT_CUST_ID = "cust_demo";
const BOOT_MESSAGE: BackendMessage = {
  role: "assistant",
  content: "V2 已就绪。你可以一次输入多个事项、条件依赖，或在执行中补充、修改、取消当前诉求。",
  created_at: "",
};

const GUIDED_INTENT_TEMPLATES: GuidedIntentTemplate[] = [
  {
    intentCode: "query_account_balance",
    title: "查账户余额",
    description: "已知卡号与手机号后4位时，可直接发起余额查询。",
    sourceFragment: "查询账户余额",
    fields: [
      { key: "card_number", label: "卡号", placeholder: "例如 6222020100049999999", inputMode: "numeric" },
      { key: "phone_last_four", label: "手机号后4位", placeholder: "例如 1234", inputMode: "numeric" },
    ],
  },
  {
    intentCode: "transfer_money",
    title: "转账",
    description: "已知收款信息时可直接执行；不全时仍会进入多轮补充。",
    sourceFragment: "转账",
    fields: [
      { key: "recipient_name", label: "收款人", placeholder: "例如 小明 / 弟弟" },
      { key: "amount", label: "金额", placeholder: "例如 1000", inputMode: "decimal" },
    ],
  },
  {
    intentCode: "query_credit_card_repayment",
    title: "查信用卡还款",
    description: "用于查询应还金额、最低还款额和到期日。",
    sourceFragment: "查询信用卡还款信息",
    fields: [
      { key: "card_number", label: "信用卡卡号", placeholder: "例如 4333333333333333", inputMode: "numeric" },
      { key: "phone_last_four", label: "手机号后4位", placeholder: "例如 5678", inputMode: "numeric" },
    ],
  },
  {
    intentCode: "pay_gas_bill",
    title: "缴纳天然气费",
    description: "适合已知燃气户号与缴费金额的快速执行场景。",
    sourceFragment: "缴纳天然气费",
    fields: [
      { key: "gas_account_number", label: "燃气户号", placeholder: "例如 88001234", inputMode: "numeric" },
      { key: "amount", label: "缴费金额", placeholder: "例如 88", inputMode: "decimal" },
    ],
  },
];

const STREAM_STATE_LABELS: Record<StreamState, string> = {
  booting: "启动中",
  connecting: "连接中",
  connected: "已连接",
  disconnected: "已断开",
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
  "graph.created": "执行图已创建",
  "graph.updated": "执行图更新",
  "graph.partially_completed": "执行图部分完成",
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

function eventLabel(event: string): string {
  return EVENT_LABELS[event] ?? event;
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

function appendBootMessage(messages: BackendMessage[] | undefined): BackendMessage[] {
  if (!messages || messages.length === 0) {
    return [BOOT_MESSAGE];
  }
  return messages;
}

function toneForStreamState(state: StreamState): "default" | "warning" | "success" | "emphasis" {
  if (state === "connected") return "success";
  if (state === "disconnected") return "warning";
  if (state === "booting" || state === "connecting") return "emphasis";
  return "default";
}

function toneForGraphStatus(status?: string): "default" | "warning" | "success" | "emphasis" {
  if (status === "completed") return "success";
  if (status === "failed" || status === "cancelled") return "warning";
  if (status === "running" || status === "waiting_user_input" || status === "waiting_confirmation") return "emphasis";
  return "default";
}

function toneForNodeStatus(status?: string): "default" | "warning" | "success" | "emphasis" {
  if (status === "completed") return "success";
  if (status === "failed" || status === "cancelled" || status === "skipped") return "warning";
  if (status === "running" || status === "waiting_user_input" || status === "waiting_confirmation") return "emphasis";
  return "default";
}

function relationTypeLabel(type: string): string {
  if (type === "conditional") return "条件依赖";
  if (type === "parallel") return "并行";
  if (type === "sequential") return "顺序";
  return type;
}

function edgeDescription(edge: BackendGraphEdge): string {
  if (edge.condition?.left_key && edge.condition.operator) {
    return `${edge.condition.left_key} ${edge.condition.operator} ${String(edge.condition.right_value ?? "")}`.trim();
  }
  return edge.label ?? relationTypeLabel(edge.relation_type);
}

function formatSlotValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value);
}

function createGuidedIntentDrafts(): GuidedIntentDraft[] {
  return GUIDED_INTENT_TEMPLATES.map((template) => ({
    ...template,
    selected: false,
    slotMemory: Object.fromEntries(template.fields.map((field) => [field.key, ""])),
  }));
}

function filterGuidedSlotMemory(slotMemory: Record<string, string>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(slotMemory)
      .map(([key, value]) => [key, value.trim()])
      .filter((entry): entry is [string, string] => Boolean(entry[1])),
  );
}

function guidedSelectionDisplayContent(items: Array<{ title: string; intentCode: string }>): string {
  const labels = items.map((item) => item.title || item.intentCode);
  return `已选择推荐事项：${labels.join("、")}`;
}

function normalizeGuidedFieldValue(field: GuidedFieldTemplate, value: string): string {
  return field.transform === "uppercase" ? value.toUpperCase() : value;
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
  if (payload.node && ["node.running", "node.waiting_user_input", "node.waiting_confirmation"].includes(eventName)) {
    next.active_node_id = payload.node.node_id;
  }

  if (eventName === "graph.proposed" || eventName === "graph.waiting_confirmation") {
    next.pending_graph = payload.graph ?? payload.pending_graph ?? next.pending_graph;
    return next;
  }

  if (eventName === "graph.confirmed") {
    next.pending_graph = null;
    if (payload.graph) {
      next.current_graph = payload.graph;
    }
    return next;
  }

  if (eventName === "graph.cancelled") {
    if (previous.pending_graph && previous.pending_graph.graph_id === event.task_id) {
      next.pending_graph = null;
    }
    if (payload.graph) {
      next.current_graph = payload.graph;
    }
    return next;
  }

  if (payload.pending_graph) {
    next.pending_graph = payload.pending_graph;
  }
  if (payload.graph) {
    next.current_graph = payload.graph;
  }

  return next;
}

export default function ChatV2PageClient() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [custId, setCustId] = useState<string>(DEFAULT_CUST_ID);
  const [snapshot, setSnapshot] = useState<BackendSnapshot | null>(null);
  const [messages, setMessages] = useState<BackendMessage[]>([BOOT_MESSAGE]);
  const [timeline, setTimeline] = useState<TimelineEntry[]>([]);
  const [composer, setComposer] = useState("");
  const [guidedIntents, setGuidedIntents] = useState<GuidedIntentDraft[]>(() => createGuidedIntentDrafts());
  const [isSending, setIsSending] = useState(false);
  const [isSubmittingGuidedSelection, setIsSubmittingGuidedSelection] = useState(false);
  const [isSubmittingGraphAction, setIsSubmittingGraphAction] = useState(false);
  const [isCancellingNode, setIsCancellingNode] = useState(false);
  const [showDiagnostics, setShowDiagnostics] = useState(false);
  const [streamState, setStreamState] = useState<StreamState>("booting");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const eventSourceRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const shouldAutoScrollRef = useRef(true);

  function clearReconnectTimer() {
    if (typeof reconnectTimerRef.current === "number") {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }

  function applySnapshot(body: BackendSnapshot) {
    setSnapshot(body);
    setMessages(appendBootMessage(body.messages));
  }

  async function refreshSnapshot(nextSessionId: string) {
    const response = await fetch(`${API_BASE}/sessions/${nextSessionId}`);
    if (!response.ok) {
      throw new Error(await response.text());
    }
    applySnapshot((await response.json()) as BackendSnapshot);
  }

  function scrollMessagesToLatest(behavior: ScrollBehavior) {
    const node = messageListRef.current;
    if (!node) {
      return;
    }
    node.scrollTo({ top: node.scrollHeight, behavior });
  }

  function handleMessageListScroll() {
    const node = messageListRef.current;
    if (!node) {
      return;
    }
    const distanceToBottom = node.scrollHeight - node.scrollTop - node.clientHeight;
    shouldAutoScrollRef.current = distanceToBottom < 96;
  }

  function subscribe(nextSessionId: string) {
    eventSourceRef.current?.close();
    clearReconnectTimer();
    setStreamState("connecting");

    const source = new EventSource(`${API_BASE}/sessions/${nextSessionId}/events`);
    eventSourceRef.current = source;

    source.onopen = () => {
      clearReconnectTimer();
      setStreamState("connected");
    };
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) {
        clearReconnectTimer();
        setStreamState("disconnected");
        return;
      }
      setStreamState((previous) => (previous === "booting" ? "connecting" : previous));
      if (reconnectTimerRef.current === null) {
        reconnectTimerRef.current = window.setTimeout(() => {
          reconnectTimerRef.current = null;
          setStreamState((previous) => (previous === "connected" ? previous : "disconnected"));
        }, 5000);
      }
    };

    const forward = (eventName: string, rawEvent: MessageEvent<string>) => {
      try {
        clearReconnectTimer();
        setStreamState("connected");
        const data = JSON.parse(rawEvent.data) as BackendTaskEvent;
        setTimeline((previous) => [
          {
            id: `${eventName}-${data.created_at ?? new Date().toISOString()}-${previous.length}`,
            event: eventName,
            message: data.message ?? eventLabel(eventName),
            created_at: data.created_at ?? new Date().toISOString(),
          },
          ...previous,
        ].slice(0, 20));
        setSnapshot((previous) => mergeSnapshotFromEvent(previous, eventName, data));
      } catch {
        // Ignore malformed SSE frames instead of breaking the live view.
      }
    };

    Object.keys(EVENT_LABELS).forEach((eventName) => {
      source.addEventListener(eventName, (event) => forward(eventName, event as MessageEvent<string>));
    });
  }

  useEffect(() => {
    let cancelled = false;

    async function boot() {
      try {
        setErrorMessage(null);
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
        setStreamState("disconnected");
        setErrorMessage(error instanceof Error ? error.message : "初始化 V2 会话失败");
      }
    }

    void boot();
    return () => {
      cancelled = true;
      clearReconnectTimer();
      eventSourceRef.current?.close();
    };
  }, []);

  useEffect(() => {
    const node = messageListRef.current;
    if (!node || !shouldAutoScrollRef.current) {
      return;
    }
    const frameId = window.requestAnimationFrame(() => {
      scrollMessagesToLatest(
        isSending || isSubmittingGuidedSelection || isSubmittingGraphAction || isCancellingNode ? "auto" : "smooth",
      );
    });
    return () => window.cancelAnimationFrame(frameId);
  }, [messages, snapshot, isSending, isSubmittingGuidedSelection, isSubmittingGraphAction, isCancellingNode]);

  async function onSendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!sessionId || !composer.trim()) {
      return;
    }

    const content = composer.trim();
    setComposer("");
    setIsSending(true);
    setErrorMessage(null);
    shouldAutoScrollRef.current = true;

    const localMessage: BackendMessage = {
      role: "user",
      content,
      created_at: new Date().toISOString(),
    };

    startTransition(() => {
      setMessages((previous) => [...previous, localMessage]);
    });

    try {
      const response = await fetch(`${API_BASE}/sessions/${sessionId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content, cust_id: custId }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const payload = (await response.json()) as { snapshot: BackendSnapshot };
      applySnapshot(payload.snapshot);
    } catch (error: unknown) {
      setErrorMessage(error instanceof Error ? error.message : "发送消息失败");
      setMessages((previous) => previous.filter((message) => message !== localMessage));
    } finally {
      setIsSending(false);
    }
  }

  function toggleGuidedIntent(intentCode: string) {
    setGuidedIntents((previous) =>
      previous.map((item) => (item.intentCode === intentCode ? { ...item, selected: !item.selected } : item)),
    );
  }

  function updateGuidedSlot(intentCode: string, field: GuidedFieldTemplate, value: string) {
    setGuidedIntents((previous) =>
      previous.map((item) =>
        item.intentCode === intentCode
          ? {
              ...item,
              selected: true,
              slotMemory: {
                ...item.slotMemory,
                [field.key]: normalizeGuidedFieldValue(field, value),
              },
            }
          : item,
      ),
    );
  }

  function resetGuidedSelection() {
    setGuidedIntents(createGuidedIntentDrafts());
  }

  async function submitGuidedSelection() {
    if (!sessionId) {
      return;
    }

    const selectedIntents = guidedIntents
      .filter((item) => item.selected)
      .map((item) => ({
        intentCode: item.intentCode,
        title: item.title,
        sourceFragment: item.sourceFragment,
        slotMemory: filterGuidedSlotMemory(item.slotMemory),
      }));

    if (selectedIntents.length === 0) {
      return;
    }

    const content = composer.trim();
    const displayContent = content || guidedSelectionDisplayContent(selectedIntents);
    const localMessage: BackendMessage = {
      role: "user",
      content: displayContent,
      created_at: new Date().toISOString(),
    };

    setIsSubmittingGuidedSelection(true);
    setErrorMessage(null);
    shouldAutoScrollRef.current = true;
    if (content) {
      setComposer("");
    }

    startTransition(() => {
      setMessages((previous) => [...previous, localMessage]);
    });

    try {
      const response = await fetch(`${API_BASE}/sessions/${sessionId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content,
          cust_id: custId,
          guidedSelection: { selectedIntents },
        }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const payload = (await response.json()) as { snapshot: BackendSnapshot };
      applySnapshot(payload.snapshot);
      resetGuidedSelection();
    } catch (error: unknown) {
      setErrorMessage(error instanceof Error ? error.message : "提交引导式选择失败");
      if (content) {
        setComposer(content);
      }
      setMessages((previous) => previous.filter((message) => message !== localMessage));
    } finally {
      setIsSubmittingGuidedSelection(false);
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
      applySnapshot(payload.snapshot);
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
      applySnapshot(payload.snapshot);
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
    currentGraph?.nodes.find((node) => node.node_id === snapshot?.active_node_id) ??
    displayedGraph?.nodes.find((node) => node.node_id === snapshot?.active_node_id) ??
    null;
  const candidateIntents = snapshot?.candidate_intents ?? [];
  const nodeTitleById = new Map(displayedGraph?.nodes.map((node) => [node.node_id, node.title]) ?? []);
  const selectedGuidedIntents = guidedIntents.filter((item) => item.selected);
  const canSubmitGuidedSelection = Boolean(sessionId && selectedGuidedIntents.length > 0 && !isSending && !isSubmittingGuidedSelection);
  const readyGuidedSelectionCount = selectedGuidedIntents.filter((item) =>
    item.fields.every((field) => Boolean(item.slotMemory[field.key]?.trim())),
  ).length;
  const canSend = Boolean(sessionId && composer.trim() && !isSending && !isSubmittingGuidedSelection);

  return (
    <div className="shell">
      <header className="masthead">
        <div className="brand-copy">
          <div className="brand-headline">
            <p className="eyebrow">Intent Router / Chat V2</p>
            <h1>动态图编排会话台</h1>
          </div>
          <p className="masthead-copy">默认展示会话与执行图，诊断信息折叠。条件依赖、多意图补充、取消与修改都在同一界面完成。</p>
        </div>
        <div className="masthead-actions">
          <div className="status-row">
            <Badge label={STREAM_STATE_LABELS[streamState]} tone={toneForStreamState(streamState)} />
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
              <p className="section-label">会话区</p>
              <h2>多轮对话</h2>
            </div>
            <div className="glance-strip" aria-label="会话速览">
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

          <div
            ref={messageListRef}
            className="message-list"
            aria-live="polite"
            aria-relevant="additions text"
            onScroll={handleMessageListScroll}
          >
            {messages.map((message, index) => (
              <article key={`${message.role}-${message.created_at}-${index}`} className={`message ${message.role === "user" ? "user" : ""}`.trim()}>
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
              placeholder="例如：帮我查一下余额，如果超过 5000，就给我媳妇儿转 1000。"
              value={composer}
              onChange={(event) => setComposer(event.target.value)}
            />
            <div className="composer-foot">
              <small className="hint-text">
                支持直接补充、修改或取消当前事项。如果右侧选了推荐事项并点击“按所选事项发起”，这里的文字会作为补充说明和 guided selection 一起发送。
              </small>
              <button type="submit" disabled={!canSend || isSubmittingGraphAction || isCancellingNode}>
                {isSending ? "发送中..." : "发送消息"}
              </button>
            </div>
          </form>
        </section>

        <aside className="context-rail">
          <section className="rail-section guided-section">
            <div className="section-head">
              <p className="section-label">可选引导式选择</p>
              <Badge
                label={selectedGuidedIntents.length > 0 ? `已选 ${selectedGuidedIntents.length}` : "未选择"}
                tone={selectedGuidedIntents.length > 0 ? "emphasis" : "default"}
              />
            </div>
            <h3>推荐事项</h3>
            <p className="status-copy guided-copy">
              这只是 V2 的一条可选入口。自由对话仍然保留；当你已经明确事项和关键要素时，可以直接把结构化意图送进执行图。会话区输入框仍然是主入口，若里面有补充说明，会和本面板选择一起提交。
            </p>
            <div className="guided-grid">
              {guidedIntents.map((item) => {
                const filledFields = item.fields.filter((field) => Boolean(item.slotMemory[field.key]?.trim())).length;
                const isReady = item.fields.length > 0 && filledFields === item.fields.length;
                return (
                  <article
                    key={item.intentCode}
                    className={`guided-card ${item.selected ? "is-selected" : ""}`.trim()}
                  >
                    <div className="guided-card-head">
                      <div>
                        <strong>{item.title}</strong>
                        <small>{item.intentCode}</small>
                      </div>
                      <button className="toggle-button" onClick={() => toggleGuidedIntent(item.intentCode)} type="button">
                        {item.selected ? "取消" : "选择"}
                      </button>
                    </div>
                    <p className="status-copy">{item.description}</p>
                    <div className="guided-selection-meta">
                      <Badge label={isReady ? "要素已齐" : `已填 ${filledFields}/${item.fields.length}`} tone={isReady ? "success" : "default"} />
                    </div>
                    <div className="guided-field-grid">
                      {item.fields.map((field) => (
                        <label key={`${item.intentCode}-${field.key}`} className="guided-field">
                          <span>{field.label}</span>
                          <input
                            placeholder={field.placeholder}
                            type="text"
                            inputMode={field.inputMode}
                            value={item.slotMemory[field.key] ?? ""}
                            onChange={(event) => updateGuidedSlot(item.intentCode, field, event.target.value)}
                          />
                        </label>
                      ))}
                    </div>
                  </article>
                );
              })}
            </div>
            <div className="composer-foot">
              <small className="hint-text">
                当前已选 {selectedGuidedIntents.length} 项，其中 {readyGuidedSelectionCount} 项关键槽位已齐。主输入框留空时，接口会只发送
                <span className="mono"> guidedSelection </span>
                以及空的
                <span className="mono"> content </span>
                ；若主输入框有文字，则两者一起发送。
              </small>
              <div className="guided-actions">
                <button className="toggle-button" onClick={resetGuidedSelection} type="button">
                  重置推荐项
                </button>
                <button
                  type="button"
                  disabled={!canSubmitGuidedSelection || isSubmittingGraphAction || isCancellingNode}
                  onClick={() => void submitGuidedSelection()}
                >
                  {isSubmittingGuidedSelection ? "提交中..." : "按所选事项发起"}
                </button>
              </div>
            </div>
          </section>

          {pendingGraph ? (
            <section className="rail-section plan-section">
              <div className="section-head">
                <p className="section-label">待确认执行图</p>
                <Badge label={statusLabel(pendingGraph.status, GRAPH_STATUS_LABELS)} tone={toneForGraphStatus(pendingGraph.status)} />
              </div>
              <h3>请确认后开始执行</h3>
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

          <section className={`rail-section ${displayedGraph ? `tone-${toneForGraphStatus(displayedGraph.status)}` : ""}`.trim()}>
            <div className="section-head">
              <p className="section-label">图状态</p>
              <Badge label={statusLabel(displayedGraph?.status, GRAPH_STATUS_LABELS)} tone={toneForGraphStatus(displayedGraph?.status)} />
            </div>
            <h3>{displayedGraph ? "当前执行视图" : "尚未生成执行图"}</h3>
            <p className="status-copy">
              {displayedGraph?.summary ?? "发送消息后，V2 会先做多意图识别，再生成包含顺序、条件或并行关系的执行图。"}
            </p>
            {activeNode ? (
              <>
                <small className="mono">{activeNode.intent_code}</small>
                <p className="status-copy">当前停留在 {activeNode.title} 节点，你可以直接补充信息，也可以取消当前节点。</p>
              </>
            ) : sessionId ? (
              <small className="mono">{sessionId}</small>
            ) : null}
            {snapshot?.active_node_id && currentGraph ? (
              <button className="toggle-button" disabled={isCancellingNode} onClick={() => void cancelCurrentNode()} type="button">
                {isCancellingNode ? "取消中..." : "取消当前节点"}
              </button>
            ) : null}
          </section>

          {displayedGraph ? (
            <section className="rail-section">
              <div className="section-head">
                <p className="section-label">节点视图</p>
              </div>
              <div className="graph-flow">
                {displayedGraph.nodes
                  .slice()
                  .sort((left, right) => left.position - right.position)
                  .map((node, index) => (
                    <article
                      key={node.node_id}
                      className={`graph-node-card ${snapshot?.active_node_id === node.node_id ? "is-active" : ""}`.trim()}
                    >
                      <div className="graph-node-head">
                        <div className="graph-node-meta">
                          <span className="graph-node-index">{String(index + 1).padStart(2, "0")}</span>
                          <div>
                            <strong>{node.title}</strong>
                            <small>{node.intent_code}</small>
                          </div>
                        </div>
                        <Badge label={statusLabel(node.status, NODE_STATUS_LABELS)} tone={toneForNodeStatus(node.status)} />
                      </div>
                      {node.relation_reason ? <p className="status-copy">{node.relation_reason}</p> : null}
                      {node.blocking_reason ? <p className="status-copy">{node.blocking_reason}</p> : null}
                      {node.slot_memory && Object.keys(node.slot_memory).length > 0 ? (
                        <div className="graph-slot-list">
                          {Object.entries(node.slot_memory).map(([key, value]) => (
                            <span key={`${node.node_id}-${key}`} className="graph-slot-pill">
                              {key}: {formatSlotValue(value)}
                            </span>
                          ))}
                        </div>
                      ) : (
                        <p className="graph-ghost-note">当前还没有可复用槽位。</p>
                      )}
                    </article>
                  ))}
              </div>
            </section>
          ) : null}

          {displayedGraph?.edges.length ? (
            <section className="rail-section">
              <div className="section-head">
                <p className="section-label">执行关系</p>
              </div>
              <div className="graph-edge-list">
                {displayedGraph.edges.map((edge) => (
                  <article key={edge.edge_id} className="graph-edge-card">
                    <div className="line-item">
                      <strong>{relationTypeLabel(edge.relation_type)}</strong>
                      <small>{edgeDescription(edge)}</small>
                    </div>
                    <small>
                      {nodeTitleById.get(edge.source_node_id) ?? edge.source_node_id} →{" "}
                      {nodeTitleById.get(edge.target_node_id) ?? edge.target_node_id}
                    </small>
                  </article>
                ))}
              </div>
            </section>
          ) : null}

          {showDiagnostics ? (
            <>
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
                  <p className="status-copy">SSE 已就绪，运行中的识别与图事件会滚动显示在这里。</p>
                )}
              </section>
            </>
          ) : null}
        </aside>
      </main>
    </div>
  );
}
