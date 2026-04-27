const CLUSTER_DEMO_PREFIX = "/v4-demo";
const LOCAL_ASSISTANT_API_BASE = "http://127.0.0.1:8040/api/assistant";

function observerPrefix() {
  const { pathname } = window.location;
  if (pathname === CLUSTER_DEMO_PREFIX || pathname.startsWith(`${CLUSTER_DEMO_PREFIX}/`)) {
    return CLUSTER_DEMO_PREFIX;
  }
  return "";
}

function isLocalObserver() {
  return window.location.hostname === "127.0.0.1" || window.location.hostname === "localhost";
}

function resolveApiBase(localBase, clusterSuffix) {
  if (isLocalObserver() && !OBSERVER_PREFIX) {
    return localBase;
  }
  return `${window.location.origin}${OBSERVER_PREFIX}${clusterSuffix}`;
}

const OBSERVER_PREFIX = observerPrefix();
const ASSISTANT_API_BASE = resolveApiBase(LOCAL_ASSISTANT_API_BASE, "/api/assistant");

const serviceLabels = {
  "assistant-service": "掌银助手",
  "router-v4-service": "意图识别服务",
  "execution-agent-service": "执行智能体",
  "transfer-agent": "转账执行 Agent"
};

const phaseLabels = {
  load: "加载",
  request: "请求",
  dispatch: "派发",
  callback: "回调"
};

const stageLabels = {
  turn_start: "本轮开始",
  intent_react_start: "Intent ReAct 开始",
  intent_react_done: "Intent ReAct 完成",
  after_intent_react: "Intent ReAct 后",
  after_intent_mapped: "意图映射后",
  after_scene_selected: "场景选定后",
  before_dispatch: "派发前",
  after_state: "状态读取后"
};

const eventLabels = {
  "assistant.turn_received": "助手服务端接收消息",
  "assistant.router_request": "助手调用意图识别服务",
  "assistant.router_response": "助手收到 Router 返回",
  "assistant.agent_request": "助手调用执行 Agent",
  "assistant.agent_response": "助手收到 Agent 返回",
  "assistant.visible_result_generated": "助手生成用户可见结果",
  assistant_request: "助手请求进入意图服务",
  context_progressive_built: "构建渐进式上下文",
  context_block_loaded: "加载上下文块",
  intent_selected: "LLM 意图识别完成",
  intent_react_failed: "Intent ReAct 失败",
  agent_dispatched: "派发执行任务",
  skill_loaded: "加载 Skill",
  skill_react_decision: "Skill ReAct 决策",
  scene_unrecognized: "未识别到场景",
  agent_dispatch_failed: "执行任务派发失败",
  assistant_receives_structured_state: "助手收到结构化状态"
};

const state = {
  sessionId: null,
  turnIndex: 0,
  activeTaskId: null,
  flowHistory: [],
  run: null,
  events: [],
  selectedSeq: null,
  selectedFlowId: null,
  activeTab: "mechanism"
};

const elements = {
  messageInput: document.querySelector("#messageInput"),
  sendButton: document.querySelector("#sendButton"),
  refreshButton: document.querySelector("#refreshButton"),
  runStatus: document.querySelector("#runStatus"),
  chatLog: document.querySelector("#chatLog"),
  runId: document.querySelector("#runId"),
  sessionId: document.querySelector("#sessionId"),
  eventCount: document.querySelector("#eventCount"),
  assistantLane: document.querySelector("#assistantLane"),
  routerLane: document.querySelector("#routerLane"),
  agentLane: document.querySelector("#agentLane"),
  mechanismView: document.querySelector("#mechanismView"),
  loadsList: document.querySelector("#loadsList"),
  resultView: document.querySelector("#resultView")
};

function serviceName(event) {
  return serviceLabels[event.service] || event.service || "服务";
}

function phaseName(event) {
  return phaseLabels[event.phase] || event.phase || "阶段";
}

function eventTitle(event) {
  return event.title || eventLabels[event.event] || event.event || "链路事件";
}

function formatJson(value) {
  if (value === null || value === undefined) return "无";
  return JSON.stringify(value, null, 2);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

function getRecord(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function getInputMessage(event) {
  const input = getRecord(event.input);
  return typeof input?.message === "string" ? input.message : "";
}

function getSceneId(event) {
  const artifact = getRecord(event.artifact);
  return artifact?.scene_id || artifact?.skill_id || "";
}

function getFileName(path) {
  return typeof path === "string" ? path.split("/").pop() : "";
}

function assistantText(output) {
  if (!output) return "意图服务未返回。";
  if (output.status === "dispatched") {
    if (output.scene_id === "transfer") {
      return "已进入转账办理流程，转账信息会由执行 Agent 按 Skill 继续处理。";
    }
    if (output.scene_id === "balance_query") {
      return "已进入余额查询流程，正在获取账户结果。";
    }
    if (output.scene_id === "fund_query") {
      return "已进入基金查询流程，正在查询产品信息。";
    }
    return "好的，我正在为你处理。";
  }
  if (output.status === "planned") {
    return "好的，我会分开处理这几个事项。";
  }
  if (output.status === "forwarded") {
    return "好的，继续为你处理。";
  }
  if (output.status === "task_updated") {
    return "已收到处理结果。";
  }
  if (output.status === "no_action") {
    return "好的，这次先不处理。";
  }
  if (output.status === "failed") {
    return "这次没有处理成功，请稍后再试。";
  }
  if (output.status === "clarification_required") {
    return output.response || "请再补充一点信息。";
  }
  return "好的，我来处理。";
}

function routerStatusText(output) {
  if (!output) return "意图服务未返回。";
  return output.response || `status=${output.status}`;
}

function stageName(stage) {
  return stageLabels[stage] || stage || "未知阶段";
}

function compactJson(value, limit = 900) {
  const raw = formatJson(value);
  if (raw.length <= limit) return raw;
  return `${raw.slice(0, limit).trimEnd()}\n...`;
}

function syncActiveTab() {
  document.querySelectorAll(".tab").forEach((item) => {
    item.classList.toggle("active", item.dataset.tab === state.activeTab);
  });
  document.querySelectorAll(".panel").forEach((item) => {
    item.classList.toggle("active", item.id === `${state.activeTab}Panel`);
  });
}

function requestJson(path, options, baseUrl = ASSISTANT_API_BASE) {
  return fetch(`${baseUrl}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options && options.headers ? options.headers : {})
    }
  }).then(async (response) => {
    if (!response.ok) {
      const text = await response.text();
      throw new Error(`${response.status} ${response.statusText}${text ? `：${text}` : ""}`);
    }
    return response.json();
  });
}

async function requestSse(path, options, handlers = {}, baseUrl = ASSISTANT_API_BASE) {
  const response = await fetch(`${baseUrl}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      ...(options && options.headers ? options.headers : {})
    }
  });
  if (!response.ok || !response.body) {
    const text = await response.text();
    throw new Error(`${response.status} ${response.statusText}${text ? `：${text}` : ""}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let frameEnd = buffer.indexOf("\n\n");
    while (frameEnd >= 0) {
      const frame = buffer.slice(0, frameEnd);
      buffer = buffer.slice(frameEnd + 2);
      const parsed = parseSseFrame(frame);
      if (parsed) handlers.onEvent?.(parsed.event, parsed.data);
      frameEnd = buffer.indexOf("\n\n");
    }
  }
  const tail = buffer.trim();
  if (tail) {
    const parsed = parseSseFrame(tail);
    if (parsed) handlers.onEvent?.(parsed.event, parsed.data);
  }
}

function parseSseFrame(frame) {
  let event = "message";
  const dataLines = [];
  frame.split(/\r?\n/).forEach((line) => {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  });
  if (!dataLines.length) return null;
  const raw = dataLines.join("\n");
  try {
    return { event, data: JSON.parse(raw) };
  } catch (_error) {
    return { event, data: raw };
  }
}

function setBusy(isBusy) {
  elements.sendButton.disabled = isBusy;
  elements.runStatus.textContent = isBusy ? "处理中" : state.run?.status === "failed" ? "失败" : state.run ? "已处理" : "待发送";
}

function appendBubble(kind, label, text) {
  const node = document.createElement("article");
  node.className = `bubble ${kind}`;
  node.innerHTML = `<span></span><p></p>`;
  node.querySelector("span").textContent = label;
  node.querySelector("p").textContent = text;
  elements.chatLog.appendChild(node);
  elements.chatLog.scrollTop = elements.chatLog.scrollHeight;
  return node;
}

function resetChat(message) {
  elements.chatLog.innerHTML = "";
  appendBubble("user", "用户", message);
  appendBubble("assistant", "助手", "收到，我先帮你看一下。");
}

function updateMeta() {
  elements.runId.textContent = state.run?.run_id || "未开始";
  elements.sessionId.textContent = state.run?.session_id || "未创建";
  elements.eventCount.textContent = String(state.events.length);
  elements.refreshButton.disabled = true;
  elements.runStatus.textContent = state.run?.status === "failed" ? "失败" : state.run ? "已处理" : "待发送";

  elements.assistantLane.classList.toggle("active", state.events.some((event) => event.layer === "assistant"));
  elements.routerLane.classList.toggle("active", state.events.some((event) => event.layer === "router"));
  elements.agentLane.classList.toggle("active", state.events.some((event) => event.layer === "agent"));
}

function currentOutput() {
  return state.run?.output || null;
}

function traceItems(output = currentOutput()) {
  return Array.isArray(output?.prompt_report?.load_trace) ? output.prompt_report.load_trace : [];
}

function findTrace(block, output = currentOutput()) {
  return traceItems(output).find((item) => item.block === block) || null;
}

function tracesFor(blocks, output = currentOutput()) {
  const wanted = new Set(blocks);
  return traceItems(output).filter((item) => wanted.has(item.block));
}

function firstFileName(trace) {
  const files = Array.isArray(trace?.files) ? trace.files : [];
  return files.map((file) => getFileName(file.path)).filter(Boolean).join("、");
}

function traceSnippet(trace, limit = 420) {
  if (!trace) return "";
  const files = Array.isArray(trace.files) ? trace.files : [];
  const fileExcerpt = files.map((file) => file.excerpt).find((value) => typeof value === "string" && value.trim());
  const markdownExcerpt = trace.content?.markdown_excerpt;
  const raw = fileExcerpt || markdownExcerpt || compactJson(trace.content, limit);
  return raw.length > limit ? `${raw.slice(0, limit).trimEnd()}\n...` : raw;
}

function selectedIntentEvent(output) {
  return (output?.events || []).find((event) => event.type === "intent_selected" || event.type === "scene_selected") || null;
}

function dispatchEvent(output) {
  return (output?.events || []).find((event) => event.type === "agent_dispatched" || event.type === "task.dispatched") || null;
}

function sceneSummary(output) {
  const recognizedTrace = findTrace("intent_react_output", output);
  const recognized = Array.isArray(recognizedTrace?.content) ? recognizedTrace.content[0] : null;
  const selected = selectedIntentEvent(output);
  return {
    intentId: selected?.intent_id || recognized?.intent_id || "未命中",
    sceneId: output?.scene_id || recognized?.scene_id || "未命中",
    score: selected?.score || recognized?.score || null,
    reasons: selected?.reasons || recognized?.reasons || [],
    hints: output?.routing_hints || recognized?.routing_hints || {}
  };
}

function renderTracePills(traces) {
  const items = traces.filter(Boolean);
  if (!items.length) return "";
  return `
    <div class="evidence-strip">
      ${items.map((trace) => `
        <span>
          <b>${escapeHtml(stageName(trace.stage))}</b>
          ${escapeHtml(trace.block || "上下文块")}
          ${firstFileName(trace) ? `<em>${escapeHtml(firstFileName(trace))}</em>` : ""}
        </span>
      `).join("")}
    </div>
  `;
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function firstPresent(...values) {
  return values.find((value) => value !== null && value !== undefined && value !== "");
}

function compactValue(value, fallback = "无") {
  if (value === null || value === undefined || value === "") return fallback;
  if (Array.isArray(value)) return value.length ? value.join("、") : fallback;
  if (typeof value === "object") return Object.keys(value).length ? formatJson(value) : fallback;
  return String(value);
}

function flowSource(output) {
  return output?.agent_flow || output?.flow || output?.observer_flow || output?.main_flow || null;
}

function normalizeReturnedFlow(output) {
  const source = flowSource(output);
  const nodes = Array.isArray(source) ? source : asArray(source?.nodes);
  if (!nodes.length) return [];
  return nodes.map((node, index) => {
    const type = node.type || node.kind || node.role || node.lane || "router";
    const id = String(node.id || node.node_id || `${type}-${index + 1}`);
    return {
      id,
      type,
      title: node.title || node.name || flowTypeName(type),
      summary: node.summary || node.description || node.message || "后端返回的演示链路节点。",
      status: node.status || node.state || "已完成",
      owner: node.owner || node.service || node.agent || "",
      details: Array.isArray(node.details) ? node.details : [],
      evidence: Array.isArray(node.evidence) ? node.evidence : [],
      payload: firstPresent(node.payload, node.data, node.raw, node)
    };
  });
}

function flowTypeName(type) {
  const names = {
    user: "用户输入",
    assistant: "助手承接",
    router: "意图路由",
    spec: "Spec 识别",
    "router/spec": "Spec 识别",
    skill: "Skill 约束",
    agent: "Agent 执行",
    result: "业务结果"
  };
  return names[type] || names[String(type).toLowerCase()] || "链路节点";
}

function flowTypeClass(type) {
  const normalized = String(type || "").toLowerCase().replace("/", "-");
  if (normalized.includes("router") || normalized.includes("spec")) return "router-spec";
  if (normalized.includes("skill")) return "skill";
  if (normalized.includes("agent")) return "agent";
  if (normalized.includes("result")) return "result";
  if (normalized.includes("assistant")) return "assistant";
  return "user";
}

function selectedFlowNode(nodes) {
  return nodes.find((node) => node.id === state.selectedFlowId) || nodes[0] || null;
}

function buildDerivedFlow(output) {
  if (!output) return [];
  const scene = sceneSummary(output);
  const dispatch = dispatchEvent(output);
  const intentTrace = findTrace("intent_catalog", output);
  const skillReferenceTrace = findTrace("skill_reference", output);
  const referenceTrace = findTrace("retrieved_references", output);
  const recognizedTrace = findTrace("intent_react_output", output);
  const stateTrace = findTrace("routing_state", output);
  const skillRef = dispatch?.task_payload?.skill_ref || {};
  const tasks = asArray(output.tasks);
  const routerEvents = asArray(output.events);
  const agentCallbacks = routerEvents.filter((event) => {
    const type = String(event.type || event.event || "").toLowerCase();
    return type.includes("agent") || type.includes("task_updated") || type.includes("result");
  });
  const intentEvent = selectedIntentEvent(output);
  const agentOutput = output.agent_output;
  const hints = output.routing_hints || scene.hints || {};
  const reasonText = asArray(scene.reasons).length ? scene.reasons.join("；") : "未返回可读识别理由";

  const nodes = [
    {
      id: "user-input",
      type: "user",
      title: "用户表达进入助手",
      summary: output.observer_message || getInputMessage(state.events[0] || {}) || "本轮用户输入已进入助手对话。",
      status: "已接收",
      owner: "掌银助手",
      details: [
        "场景入口：对话输入",
        "助手只负责收集用户表达和页面上下文，再交给 Router 识别。"
      ],
      evidence: [],
      payload: state.events[0]?.input || null
    },
    {
      id: "assistant-request",
      type: "assistant",
      title: "助手发起意图路由",
      summary: "助手把用户表达、来源和页面上下文提交给意图识别服务。",
      status: "已提交",
      owner: "掌银助手",
      details: [
        "业务展示中左侧只保留用户和助手话术，底层请求细节放在节点详情里。",
        "来源：用户主动输入"
      ],
      evidence: [],
      payload: buildAssistantRequest({
        sessionId: state.run?.session_id || "observer-session",
        message: output.observer_message || getInputMessage(state.events[0] || {}) || elements.messageInput.value
      })
    },
    {
      id: "router-spec",
      type: "router/spec",
      title: "独立 Intent Spec 识别",
      summary: `命中意图 ${scene.intentId}。Router 识别阶段只加载一个 intent.md，不加载 Skill 正文。`,
      status: output.status === "failed" ? "异常" : "已识别",
      owner: "意图识别服务",
      details: [
        `识别理由：${reasonText}`,
        `置信分：${scene.score ?? "未返回"}`,
        `映射场景：${scene.sceneId}`,
        `业务提槽：由执行 Agent 按 Skill 处理${Object.keys(hints).length ? `；兼容 hints=${Object.keys(hints).join("、")}` : ""}`
      ],
      evidence: [stateTrace, intentTrace, recognizedTrace, skillReferenceTrace].filter(Boolean),
      payload: {
        intent_id: scene.intentId,
        scene_id: scene.sceneId,
        score: scene.score,
        reasons: scene.reasons,
        field_owner: "execution-agent/skill",
        routing_hints: hints,
        intent_event: intentEvent
      }
    }
  ];

  if (skillRef.skill_id) {
    nodes.push({
      id: "skill-ref",
      type: "skill",
      title: "Skill 引用交给 Agent",
      summary: `${skillRef.skill_id} 是派发引用；Skill md 的实际加载发生在执行 Agent 内。`,
      status: "已引用",
      owner: skillRef.owner || output.target_agent || "执行 Agent",
      details: [
        `Skill：${skillRef.skill_id}`,
        `职责说明：${skillRef.description || "按场景 Skill 完成业务动作"}`,
        "Router 不读取 skill md；Agent 会在自己的生命周期中加载 skill md。"
      ],
      evidence: [skillReferenceTrace, dispatch, referenceTrace].filter(Boolean),
      payload: skillRef
    });
  }

  if (tasks.length) {
    tasks.forEach((task, index) => {
      nodes.push({
        id: `agent-task-${index + 1}`,
        type: "agent",
        title: `执行 Agent 任务 ${index + 1}`,
        summary: `${task.scene_id || scene.sceneId} 已交给 ${task.target_agent || output.target_agent || "执行 Agent"}。`,
        status: task.status || "已派发",
        owner: task.target_agent || output.target_agent || "执行 Agent",
        details: [
          `任务编号：${task.task_id || "未返回"}`,
          `业务场景：${task.scene_id || scene.sceneId}`,
          task.stream_url ? `进度流：${task.stream_url}` : "当前未返回进度流地址",
          "补充信息、业务校验、风控确认和 API 调用由执行 Agent 负责。"
        ],
        evidence: [findTrace("dispatch_contract", output), referenceTrace].filter(Boolean),
        payload: task
      });
    });
  } else if (dispatch || output.target_agent) {
    nodes.push({
      id: "agent-task",
      type: "agent",
      title: "派发给执行 Agent",
      summary: `已交给 ${output.target_agent || "执行 Agent"} 继续办理。`,
      status: "已派发",
      owner: output.target_agent || "执行 Agent",
      details: [
        `任务数：${dispatch ? 1 : 0}`,
        `目标 Agent：${output.target_agent || "未返回"}`,
        "补充信息、业务校验、风控确认和 API 调用由执行 Agent 负责。"
      ],
      evidence: [findTrace("dispatch_contract", output), referenceTrace].filter(Boolean),
      payload: dispatch
    });
  }

  agentCallbacks.forEach((callback, index) => {
    const type = callback.type || callback.event || "agent_callback";
    if (type === "agent_dispatched") return;
    nodes.push({
      id: `agent-callback-${index + 1}`,
      type: "result",
      title: "Agent 回调更新",
      summary: callback.summary || summarizeRouterEvent(callback, output),
      status: callback.status || output.status || "已更新",
      owner: callback.target_agent || output.target_agent || "执行 Agent",
      details: [
        `回调类型：${type}`,
        `关联任务：${callback.task_id || output.task_id || "未返回"}`,
        "该节点来自后端事件流，用于展示多轮或 Agent 回调带来的链路增量。"
      ],
      evidence: [callback],
      payload: callback
    });
  });

  nodes.push({
    id: "business-result",
    type: "result",
    title: "结构化结果回到助手",
    summary: resultConclusion(output),
    status: agentOutput ? "已返回业务结果" : output.status || "已返回状态",
    owner: "掌银助手",
    details: [
      `助手可见话术：${assistantText(output)}`,
      agentOutput ? "执行 Agent 已回传结构化结果。" : "当前展示 Router 返回状态；等待执行 Agent 回调后可补充业务结果节点。"
    ],
    evidence: routerEvents.slice(-3),
    payload: agentOutput || output
  });

  return nodes;
}

function flowNodes(output) {
  if (state.flowHistory.length) return state.flowHistory;
  const returnedNodes = normalizeReturnedFlow(output);
  return returnedNodes.length ? returnedNodes : buildDerivedFlow(output);
}

function renderNodeEvidence(items) {
  const evidence = items.filter(Boolean).slice(0, 6);
  if (!evidence.length) return `<div class="empty inline">这个节点没有额外证据。</div>`;
  return `
    <div class="node-evidence">
      ${evidence.map((item) => {
        const title = item.block || item.type || item.event || item.title || "证据";
        const summary = item.summary || item.description || item.stage || item.status || "";
        const label = item.stage ? stageName(item.stage) : flowTypeName(item.type || item.event);
        return `
          <span>
            <b>${escapeHtml(label)}</b>
            ${escapeHtml(title)}
            ${summary ? `<em>${escapeHtml(summary)}</em>` : ""}
          </span>
        `;
      }).join("")}
    </div>
  `;
}

function renderFlowNode(node, index, isActive) {
  return `
    <button class="flow-node ${flowTypeClass(node.type)}${isActive ? " active" : ""}" data-flow-id="${escapeHtml(node.id)}" type="button">
      <span class="node-order">${index + 1}</span>
      <span class="node-copy">
        <b>${escapeHtml(node.title)}</b>
        <small>${escapeHtml(node.summary)}</small>
      </span>
      <span class="node-meta">
        <i>${escapeHtml(flowTypeName(node.type))}</i>
        <em>${escapeHtml(node.status || "处理中")}</em>
      </span>
    </button>
  `;
}

function renderFlowDetails(node) {
  if (!node) return `<aside class="flow-detail"><div class="empty">点击链路节点查看详情。</div></aside>`;
  const detailRows = asArray(node.details).filter(Boolean);
  return `
    <aside class="flow-detail">
      <div class="detail-head">
        <span>${escapeHtml(flowTypeName(node.type))}</span>
        <strong>${escapeHtml(node.title)}</strong>
        <small>${escapeHtml(node.owner || "业务链路")}</small>
      </div>
      <p>${escapeHtml(node.summary)}</p>
      ${detailRows.length ? `
        <ul class="detail-list">
          ${detailRows.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}
        </ul>
      ` : ""}
      <section>
        <h3>本节点依据</h3>
        ${renderNodeEvidence(asArray(node.evidence))}
      </section>
      <section>
        <h3>结构化数据</h3>
        <pre class="drive-snippet large">${escapeHtml(compactJson(node.payload, 1800))}</pre>
      </section>
    </aside>
  `;
}

function renderMechanism() {
  const output = currentOutput();
  if (!output) {
    elements.mechanismView.innerHTML = `<div class="empty">发送一次对话后，这里展示用户、助手、Router/Spec、Skill、Agent 和结果节点。</div>`;
    return;
  }

  const scene = sceneSummary(output);
  const promptReport = output.prompt_report || {};
  const dropped = Array.isArray(promptReport.dropped_blocks) ? promptReport.dropped_blocks : [];
  const nodes = flowNodes(output);
  if (!nodes.some((node) => node.id === state.selectedFlowId)) {
    state.selectedFlowId = nodes.find((node) => flowTypeClass(node.type) === "router-spec")?.id || nodes[0]?.id || null;
  }
  const selected = selectedFlowNode(nodes);
  const skillRef = dispatchEvent(output)?.task_payload?.skill_ref || {};
  const hintNames = Object.keys(scene.hints || {});

  elements.mechanismView.innerHTML = `
    <section class="drive-summary">
      <div>
        <span>命中意图</span>
        <strong>${escapeHtml(scene.intentId)}</strong>
        <small>${scene.score ? `置信分：${escapeHtml(scene.score)}` : "由 spec/LLM 识别结果决定"}</small>
      </div>
      <div>
        <span>映射场景</span>
        <strong>${escapeHtml(scene.sceneId)}</strong>
        <small>${escapeHtml(output.target_agent || "等待目标 Agent")}</small>
      </div>
      <div>
        <span>Skill 归属</span>
        <strong>${escapeHtml(skillRef.skill_id || "Agent 内加载")}</strong>
        <small>${hintNames.length ? `兼容 hints：${escapeHtml(hintNames.join("、"))}` : "Router 只传 skill_ref"}</small>
      </div>
      <div>
        <span>当前结果</span>
        <strong>${escapeHtml(output.status || "未知")}</strong>
        <small>${escapeHtml(assistantText(output))}</small>
      </div>
    </section>

    <section class="flow-panel">
      <div class="flow-title">
        <div>
          <h2>本轮业务链路 Flow</h2>
          <p>默认展示 intent spec、场景契约、Agent Skill 加载和业务结果。底层报文不铺满页面，点击节点查看依据。</p>
        </div>
        <span>${escapeHtml(nodes.length)} 个节点</span>
      </div>
      <div class="flow-workspace">
        <div class="flow-graph" aria-label="业务链路节点图">
          ${nodes.map((node, index) => renderFlowNode(node, index, node.id === selected?.id)).join("")}
        </div>
        ${renderFlowDetails(selected)}
      </div>
    </section>

    <section class="drive-panel">
      <h2>和 OpenClaw / Hermes 调研的落点</h2>
      <div class="alignment-grid">
        <article>
          <strong>状态持久化</strong>
          <p>对应 <code>routing_state</code>、<code>recent_transcript</code>。保留当前任务和路由态，多轮不用从头开始。</p>
        </article>
        <article>
          <strong>渐进加载</strong>
          <p>识别前只读一个 <code>intent_catalog</code>，命中后读取同一目录里的 <code>skill_reference</code> 和 <code>dispatch_contract</code>；Skill md 由 Agent 加载。</p>
        </article>
        <article>
          <strong>压缩 / 裁剪</strong>
          <p>本轮预算：${escapeHtml(promptReport.max_chars || "未配置")}；裁剪块：${escapeHtml(dropped.length ? dropped.join("、") : "无")}。</p>
        </article>
        <article>
          <strong>按需检索</strong>
          <p>对应 <code>retrieved_references</code>。目前只用于 intent 参考材料，后续可替换成真实检索服务。</p>
        </article>
      </div>
    </section>
  `;
}

function renderLoads() {
  const loadEvents = state.events.filter((event) => event.phase === "load");
  if (!loadEvents.length) {
    elements.loadsList.innerHTML = `<div class="empty">发送一次对话后，这里展示每个阶段读取了哪些 spec、md、reference，以及内容摘录。</div>`;
    return;
  }

  elements.loadsList.innerHTML = loadEvents
    .map((event) => {
      const artifact = getRecord(event.artifact);
      const files = Array.isArray(artifact?.files) ? artifact.files : [];
      const fileNames = files.map((file) => getFileName(file.path)).filter(Boolean).join("、");
      const mdFiles = files.filter((file) => String(file.path || "").endsWith(".md"));
      const active = event.seq === state.selectedSeq ? " active" : "";
      return `
        <article class="load-card${active}" data-seq="${event.seq || ""}" tabindex="0">
          <div class="load-card-head">
            <span>${escapeHtml(stageName(artifact?.stage))}</span>
            <strong>${escapeHtml(artifact?.block || eventTitle(event))}</strong>
            <i>${artifact?.included ? "已加载" : "被裁剪"}</i>
          </div>
          <p>${escapeHtml(artifact?.summary || event.summary || "")}</p>
          <div class="load-facts">
            ${fileNames ? `<b>文件：${escapeHtml(fileNames)}</b>` : `<b>文件：无</b>`}
            ${artifact?.spec_path ? `<b>Spec path：${escapeHtml(artifact.spec_path)}</b>` : ""}
            ${mdFiles.length ? `<b>MD：${escapeHtml(mdFiles.map((file) => getFileName(file.path)).join("、"))}</b>` : ""}
          </div>
          ${files.length ? renderFiles(files) : ""}
          <pre class="load-content">${escapeHtml(compactJson(artifact?.content))}</pre>
        </article>
      `;
    })
    .join("");
}

function renderFiles(files) {
  return `
    <div class="file-list">
      ${files.map((file) => `
        <section class="file-item">
          <strong>${escapeHtml(getFileName(file.path) || file.logical_path || "文件")}</strong>
          <small>${escapeHtml(file.kind || "file")} · ${file.exists ? "存在" : "未找到"}</small>
          <code>${escapeHtml(file.path || file.logical_path || "")}</code>
          ${file.excerpt ? `<pre>${escapeHtml(file.excerpt)}</pre>` : ""}
        </section>
      `).join("")}
    </div>
  `;
}

function selectedEvent() {
  return state.events.find((event) => event.seq === state.selectedSeq) || state.events[state.events.length - 1] || null;
}

function resultConclusion(output) {
  if (!output) return "";
  if (output.status === "dispatched") {
    return `已经完成 Router 层闭环：命中意图并映射到 ${output.scene_id || "目标"} 场景，创建任务并交给 ${output.target_agent || "执行 Agent"}。业务补槽、确认、风控和 API 调用由执行 Agent 继续完成。`;
  }
  if (output.status === "planned") {
    return "已经完成多意图拆流：Router 创建任务图并按任务拆分 SSE/执行流，助手侧可以分流消费进度。";
  }
  if (output.status === "task_updated") {
    return "执行 Agent 已返回结构化业务结果，助手可以基于该结果生成最终用户话术。";
  }
  if (output.status === "clarification_required") {
    return output.response || "Router 需要更多信息才能确定业务场景。";
  }
  if (output.status === "no_action") {
    return "用户表达没有承接本次主动推送意图，Router 不派发任务。";
  }
  if (output.status === "failed") {
    return output.response || "本轮处理失败。";
  }
  return output.response || "本轮已经返回结构化状态。";
}

function renderTaskCards(tasks) {
  if (!Array.isArray(tasks) || !tasks.length) {
    return `<div class="empty inline">本轮没有创建 Agent task。</div>`;
  }
  return `
    <div class="task-grid">
      ${tasks.map((task) => `
        <article>
          <span>${escapeHtml(task.scene_id || "scene")}</span>
          <strong>${escapeHtml(task.target_agent || "agent")}</strong>
          <p>${escapeHtml(task.status || "unknown")} · ${escapeHtml(task.task_id || "")}</p>
          ${task.stream_url ? `<small>SSE：${escapeHtml(task.stream_url)}</small>` : ""}
        </article>
      `).join("")}
    </div>
  `;
}

function renderResult() {
  const output = currentOutput();
  if (!output) {
    elements.resultView.innerHTML = `<div class="empty">发送一次对话后，这里展示本轮业务结论、Agent 接管状态和助手最终可见结果。</div>`;
    return;
  }

  const scene = sceneSummary(output);
  const dispatch = dispatchEvent(output);
  const skillRef = dispatch?.task_payload?.skill_ref || {};
  const agentOutput = output.agent_output;
  elements.resultView.innerHTML = `
    <section class="result-hero">
      <span>本轮结论</span>
      <h2>${escapeHtml(resultConclusion(output))}</h2>
      <p>左侧对话只展示用户和助手；这里展示助手拿到的结构化业务状态，以及执行 Agent 接下来负责的内容。</p>
    </section>

    <section class="result-grid">
      <article>
        <span>助手可见表达</span>
        <strong>${escapeHtml(assistantText(output))}</strong>
        <p>最终用户话术由助手生成，不由 Router 或 Agent 拼接。</p>
      </article>
      <article>
        <span>Router 完成</span>
        <strong>${escapeHtml(scene.intentId)} -> ${escapeHtml(scene.sceneId)}</strong>
        <p>Intent 识别、场景映射、Agent 派发和任务追踪；业务提槽在 Agent/Skill 内完成。</p>
      </article>
      <article>
        <span>执行 Agent 接管</span>
        <strong>${escapeHtml(output.target_agent || "等待派发")}</strong>
        <p>${escapeHtml(skillRef.description || "按场景 Skill 完成业务补槽、校验、确认和 API 调用。")}</p>
      </article>
      <article>
        <span>业务结果</span>
        <strong>${agentOutput ? "已返回" : "等待执行 Agent"}</strong>
        <p>${agentOutput ? "助手可基于结构化结果生成最终答复。" : "当前默认 Agent 是派发态；真实业务结果需要执行 Agent 回传后出现。"}</p>
      </article>
    </section>

    <section class="drive-panel">
      <h2>Agent 任务</h2>
      ${renderTaskCards(output.tasks)}
    </section>

    ${agentOutput ? `
      <section class="drive-panel">
        <h2>结构化业务结果</h2>
        <pre class="drive-snippet large">${escapeHtml(compactJson(agentOutput, 1600))}</pre>
      </section>
    ` : ""}
  `;
}

function renderAll() {
  syncActiveTab();
  updateMeta();
  renderMechanism();
  renderLoads();
  renderResult();
}

function ensureSession() {
  if (!state.sessionId) {
    state.sessionId = `observer_${Date.now().toString(36)}_${Math.random().toString(16).slice(2, 8)}`;
    state.turnIndex = 0;
    state.activeTaskId = null;
    state.flowHistory = [];
    state.events = [];
    elements.chatLog.innerHTML = "";
  }
  return state.sessionId;
}

function appendEvents(events) {
  const offset = state.events.length;
  state.events = [
    ...state.events,
    ...events.map((event, index) => ({
      ...event,
      seq: offset + index + 1
    }))
  ];
}

function rememberTask(output) {
  const taskId = output?.task_id || output?.tasks?.[0]?.task_id || output?.tasks?.[0]?.agent_task_id;
  if (taskId) state.activeTaskId = taskId;
}

function prefixFlowNodes(nodes, turnIndex, prefix) {
  return nodes.map((node, index) => ({
    ...node,
    id: `${prefix}-${turnIndex}-${node.id || node.node_id || index + 1}`,
    node_id: `${prefix}-${turnIndex}-${node.node_id || node.id || index + 1}`,
    details: [
      `第 ${turnIndex} 轮`,
      ...(Array.isArray(node.details) ? node.details : [])
    ]
  }));
}

function addFlowForOutput(output, turnIndex, source) {
  const nodes = prefixFlowNodes(buildDerivedFlow(output), turnIndex, source);
  state.flowHistory = [...state.flowHistory, ...nodes];
}

function addAgentFlow(agentOutput, turnIndex) {
  const rawNodes = asArray(agentOutput?.flow_nodes);
  if (!rawNodes.length) return;
  const nodes = prefixFlowNodes(normalizeReturnedFlow({ agent_flow: rawNodes }), turnIndex, "agent");
  state.flowHistory = [...state.flowHistory, ...nodes];
}

async function startRun() {
  const message = elements.messageInput.value.trim();
  if (!message) {
    elements.messageInput.focus();
    return;
  }
  const sessionId = ensureSession();
  state.turnIndex += 1;
  setBusy(true);
  appendBubble("user", "用户", message);
  const assistantBubble = appendBubble("assistant streaming", "助手", "");
  const assistantTextNode = assistantBubble.querySelector("p");
  elements.messageInput.value = "";

  try {
    let assistantTurn = null;
    let streamedText = "";
    await requestSse("/turn/stream", {
      method: "POST",
      body: JSON.stringify(buildAssistantRequest({ sessionId, message }))
    }, {
      onEvent(event, payload) {
        if (event === "assistant.status") {
          elements.runStatus.textContent = payload?.message || "处理中";
          return;
        }
        if (event === "assistant.message_start") {
          assistantTextNode.textContent = "";
          streamedText = "";
          return;
        }
        if (event === "assistant.message_delta") {
          streamedText += payload?.delta || "";
          assistantTextNode.textContent = streamedText;
          elements.chatLog.scrollTop = elements.chatLog.scrollHeight;
          return;
        }
        if (event === "assistant.message_end") {
          assistantTextNode.textContent = payload?.assistant_message || streamedText;
          return;
        }
        if (event === "assistant.final") {
          assistantTurn = payload;
        }
        if (event === "assistant.error") {
          throw new Error(payload?.error || payload?.message || "流式请求失败");
        }
      }
    });
    if (!assistantTurn) throw new Error("流式接口没有返回最终结果");
    let displayOutput = assistantTurn.output || assistantTurn.router_output || {};
    rememberTask(displayOutput);
    let assistantMessage = assistantTurn.assistant_message || assistantText(displayOutput);
    if (!assistantTextNode.textContent) assistantTextNode.textContent = assistantMessage;
    assistantBubble.classList.remove("streaming");
    displayOutput = { ...displayOutput, observer_message: message };
    addFlowForOutput(displayOutput, state.turnIndex, "router");
    if (assistantTurn.agent_output) addAgentFlow(assistantTurn.agent_output, state.turnIndex);
    displayOutput = { ...displayOutput, agent_flow: state.flowHistory };
    state.run = {
      run_id: `real_${Date.now().toString(36)}`,
      session_id: sessionId,
      status: displayOutput.status === "failed" ? "failed" : "returned",
      summary: assistantMessage,
      output: displayOutput
    };
    appendEvents(buildEventsFromRouterOutput({ sessionId, message, output: displayOutput }));
    const firstLoad = state.events.find((event) => event.phase === "load");
    state.selectedSeq = firstLoad?.seq || state.events.at(-1)?.seq || null;
    state.selectedFlowId = state.flowHistory.at(-1)?.id || null;
    state.activeTab = "mechanism";
    renderAll();
    if (displayOutput.status === "failed") {
      assistantBubble.classList.add("error");
      assistantTextNode.textContent = assistantMessage;
    }
  } catch (error) {
    assistantBubble.classList.remove("streaming");
    assistantBubble.classList.add("error");
    assistantTextNode.textContent = "请求失败了，请稍后再试。";
    console.error(error);
    state.run = { status: "failed", summary: error instanceof Error ? error.message : "请求失败" };
  } finally {
    setBusy(false);
  }
}

function buildAssistantRequest({ sessionId, message }) {
  return {
    session_id: sessionId,
    message,
    source: "user",
    user_profile: { user_id: "observer-user" },
    page_context: { current_page: "observer-ui" }
  };
}

function buildEventsFromRouterOutput({ sessionId, message, output }) {
  const now = new Date().toISOString();
  const events = [
    {
      seq: 1,
      service: "assistant-service",
      layer: "assistant",
      phase: "request",
      event: "assistant_frontend_request",
      title: "前端请求助手服务端",
      summary: "对话窗口只和助手服务端通信，Router 和 Agent 都在服务端后置调用。",
      input: { message },
      output: null,
      timestamp: now
    }
  ];
  if (output.prompt_report?.load_trace?.length) {
    output.prompt_report.load_trace.forEach((traceItem) => {
      events.push({
        seq: events.length + 1,
        service: "router-v4-service",
        layer: "router",
        phase: "load",
        event: "context_block_loaded",
        title: `加载 ${traceItem.block || "上下文块"}`,
        summary: `${traceItem.stage || "unknown"}：${traceItem.summary || ""}`,
        artifact: traceItem,
        input: {
          included_blocks: output.prompt_report.included_blocks,
          dropped_blocks: output.prompt_report.dropped_blocks,
          max_chars: output.prompt_report.max_chars
        },
        output: traceItem.content,
        timestamp: now
      });
    });
  } else if (output.prompt_report) {
    events.push({
      seq: events.length + 1,
      service: "router-v4-service",
      layer: "router",
      phase: "load",
      event: "context_progressive_built",
      title: "构建渐进式上下文",
      summary: "意图识别服务按预算加载独立 intent spec、识别结果、场景契约、近期对话和检索引用。",
      artifact: {
        included_blocks: output.prompt_report.included_blocks,
        dropped_blocks: output.prompt_report.dropped_blocks,
        recognized_intent_ids: output.prompt_report.recognized_intent_ids,
        selected_scene_id: output.prompt_report.selected_scene_id,
        retrieved_references: output.prompt_report.retrieved_references,
        lifecycle: output.prompt_report.lifecycle
      },
      output: output.prompt_report,
      timestamp: now
    });
  }
  (output.events || []).forEach((routerEvent) => {
    events.push(routerEventToObservedEvent(routerEvent, output, events.length + 1, now));
  });
  events.push({
    seq: events.length + 1,
    service: "assistant-service",
    layer: "assistant",
    phase: "callback",
    event: "assistant_receives_structured_state",
    title: "助手收到结构化状态",
    summary: routerStatusText(output),
    input: { router_output_status: output.status },
    output,
    timestamp: now
  });
  return events.map((event) => ({ session_id: sessionId, ...event }));
}

function routerEventToObservedEvent(routerEvent, output, seq, timestamp) {
  if (routerEvent.service === "assistant-service") {
    return {
      seq,
      service: "assistant-service",
      layer: "assistant",
      phase: routerEvent.phase || "request",
      event: routerEvent.type || "assistant_event",
      title: routerEvent.title || eventLabels[routerEvent.type] || "助手服务端事件",
      summary: routerEvent.summary || "助手服务端编排事件。",
      artifact: routerEvent.artifact || null,
      input: routerEvent.input || null,
      output: routerEvent.output || routerEvent,
      timestamp
    };
  }
  if (routerEvent.service === "transfer-agent") {
    return {
      seq,
      service: "transfer-agent",
      layer: "agent",
      phase: routerEvent.phase || "execute",
      event: routerEvent.type || "agent_event",
      title: routerEvent.title || "执行 Agent 事件",
      summary: routerEvent.summary || "转账 Agent 返回的生命周期事件。",
      artifact: routerEvent.artifact || null,
      input: routerEvent.input || null,
      output: routerEvent.output || routerEvent,
      timestamp
    };
  }
  const type = routerEvent.type || "router_event";
  const isLlm = Array.isArray(routerEvent.reasons) && routerEvent.reasons.includes("llm");
  const title = isLlm ? "LLM 意图识别完成" : eventLabels[type] || "意图服务事件";
  const phase = type.includes("dispatch") || type === "agent_dispatched" ? "dispatch" : "callback";
  return {
    seq,
    service: "router-v4-service",
    layer: "router",
    phase,
    event: type,
    title,
    summary: summarizeRouterEvent(routerEvent, output),
    artifact: {
      intent_id: routerEvent.intent_id,
      scene_id: routerEvent.scene_id || output.scene_id,
      target_agent: routerEvent.target_agent || output.target_agent,
      task_id: routerEvent.task_id || output.task_id,
      task_payload: routerEvent.task_payload,
      reasons: routerEvent.reasons,
      score: routerEvent.score
    },
    input: null,
    output: routerEvent,
    state_diff: {
      status: output.status,
      scene_id: output.scene_id,
      routing_hints: output.routing_hints,
      action_required: output.action_required
    },
    timestamp
  };
}

function summarizeRouterEvent(routerEvent, output) {
  const type = routerEvent.type || "";
  if (type === "intent_selected" || type === "scene_selected") {
    const via = Array.isArray(routerEvent.reasons) && routerEvent.reasons.includes("llm") ? "由 LLM" : "由意图识别服务";
    return `${via}选中意图 ${routerEvent.intent_id || "未知"}，映射场景 ${routerEvent.scene_id || output.scene_id}，置信分 ${routerEvent.score ?? "未知"}。`;
  }
  if (type === "agent_dispatched") {
    const skill = routerEvent.task_payload?.skill_ref?.skill_id ? `，Skill 引用：${routerEvent.task_payload.skill_ref.skill_id}` : "";
    return `Router 已创建 Agent task 并派发给 ${routerEvent.target_agent || output.target_agent}${skill}。`;
  }
  if (type === "llm_recognition_failed") {
    return routerEvent.error || "LLM 意图识别失败。";
  }
  return routerStatusText(output);
}

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    state.activeTab = button.dataset.tab || "mechanism";
    syncActiveTab();
  });
});

document.addEventListener("click", (event) => {
  const flowNode = event.target.closest("[data-flow-id]");
  if (flowNode) {
    state.selectedFlowId = flowNode.dataset.flowId || null;
    renderAll();
    return;
  }

  const item = event.target.closest("[data-seq]");
  if (!item) return;
  const seq = Number(item.dataset.seq);
  if (!Number.isFinite(seq)) return;
  state.selectedSeq = seq;
  renderAll();
});

elements.sendButton.addEventListener("click", () => {
  void startRun();
});

elements.refreshButton.addEventListener("click", () => {
  renderAll();
});
