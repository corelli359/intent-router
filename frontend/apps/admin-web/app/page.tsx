"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { IntentRouterApiClient } from "@intent-router/api-client";
import type { IntentDefinition, IntentInput } from "@intent-router/shared-types";

const api = new IntentRouterApiClient();

type RegistryFilter = "all" | "active" | "inactive" | "grayscale";
type WorkspaceMode = "overview" | "registry" | "compose" | "release";

const filterLabelMap: Record<RegistryFilter, string> = {
  all: "全部",
  active: "已生效",
  inactive: "未生效",
  grayscale: "灰度"
};

const statusLabelMap: Record<IntentDefinition["status"], string> = {
  active: "已生效",
  inactive: "未生效",
  grayscale: "灰度"
};

const workspaceCopyMap: Record<
  WorkspaceMode,
  { kicker: string; title: string; description: string }
> = {
  overview: {
    kicker: "运行概览",
    title: "意图库状态",
    description: "这里只看待处理和最近变更。"
  },
  registry: {
    kicker: "意图目录",
    title: "搜索和选择意图",
    description: "目录页只负责查找、筛选和选中。"
  },
  compose: {
    kicker: "注册编辑",
    title: "维护识别信息和请求契约",
    description: "这里只做配置，保存后再进入发布控制。"
  },
  release: {
    kicker: "发布控制",
    title: "检查后再切换状态",
    description: "这里只做发布检查和状态切换。"
  }
};

const initialForm: IntentInput = {
  intentCode: "",
  name: "",
  description: "",
  examples: [],
  agentUrl: "",
  status: "inactive",
  isFallback: false,
  dispatchPriority: 100,
  requestSchema: {
    type: "object",
    required: ["sessionId", "taskId", "input"]
  },
  fieldMapping: {
    sessionId: "$session.id",
    taskId: "$task.id",
    input: "$message.current"
  },
  resumePolicy: "resume_same_task"
};

function parseJsonObject(input: string, fallback: Record<string, unknown>): Record<string, unknown> {
  if (!input.trim()) return fallback;
  return JSON.parse(input) as Record<string, unknown>;
}

function safeParseJsonObject(input: string): Record<string, unknown> | null {
  if (!input.trim()) return {};
  try {
    const value = JSON.parse(input);
    if (value && typeof value === "object" && !Array.isArray(value)) {
      return value as Record<string, unknown>;
    }
    return null;
  } catch {
    return null;
  }
}

function buildFormFromIntent(intent: IntentDefinition): IntentInput {
  return {
    intentCode: intent.intentCode,
    name: intent.name,
    description: intent.description,
    examples: intent.examples,
    agentUrl: intent.agentUrl,
    status: intent.status,
    isFallback: intent.isFallback,
    dispatchPriority: intent.dispatchPriority,
    requestSchema: intent.requestSchema,
    fieldMapping: intent.fieldMapping,
    resumePolicy: intent.resumePolicy
  };
}

function formatTimestamp(value?: string | null) {
  if (!value) return "暂无";
  return new Date(value).toLocaleString("zh-CN");
}

function countRequiredFields(requestSchema: Record<string, unknown>) {
  const required = requestSchema.required;
  return Array.isArray(required) ? required.length : 0;
}

function countMappings(fieldMapping: Record<string, string>) {
  return Object.keys(fieldMapping).length;
}

function getIntentIssues(intent: {
  description: string;
  agentUrl: string;
  requestSchema: Record<string, unknown>;
  fieldMapping: Record<string, string>;
}) {
  const issues: string[] = [];

  if (!intent.description.trim()) issues.push("缺少意图描述");
  if (!intent.agentUrl.trim()) issues.push("缺少 Agent 接口地址");
  if (countRequiredFields(intent.requestSchema) === 0) issues.push("Schema 未声明必填字段");
  if (countMappings(intent.fieldMapping) === 0) issues.push("字段映射为空");

  return issues;
}

function hasDraftContent(form: IntentInput) {
  return Boolean(
    form.intentCode.trim() ||
      form.name.trim() ||
      form.description.trim() ||
      form.examples.length ||
      form.agentUrl.trim()
  );
}

export default function AdminPage() {
  const [intents, setIntents] = useState<IntentDefinition[]>([]);
  const [form, setForm] = useState(initialForm);
  const [editingIntentCode, setEditingIntentCode] = useState<string | null>(null);
  const [focusedIntentCode, setFocusedIntentCode] = useState<string | null>(null);
  const [workspaceMode, setWorkspaceMode] = useState<WorkspaceMode>("overview");
  const [registryFilter, setRegistryFilter] = useState<RegistryFilter>("all");
  const [searchText, setSearchText] = useState("");
  const [schemaText, setSchemaText] = useState(JSON.stringify(initialForm.requestSchema, null, 2));
  const [mappingText, setMappingText] = useState(JSON.stringify(initialForm.fieldMapping, null, 2));
  const [statusText, setStatusText] = useState("管理台正在加载");
  const [errorText, setErrorText] = useState<string | null>(null);
  const [lastSyncedAt, setLastSyncedAt] = useState<string | null>(null);

  async function refresh(options: {
    source?: "init" | "manual";
    syncIntentCode?: string | null;
    syncFocusedCode?: string | null;
  } = {}) {
    try {
      const next = await api.listIntents();
      const activeEditingCode = options.syncIntentCode ?? editingIntentCode;
      const activeFocusedCode = options.syncFocusedCode ?? focusedIntentCode;

      setIntents(next);
      setLastSyncedAt(new Date().toISOString());
      setErrorText(null);

      if (activeFocusedCode && next.some((intent) => intent.intentCode === activeFocusedCode)) {
        setFocusedIntentCode(activeFocusedCode);
      } else {
        setFocusedIntentCode(next[0]?.intentCode ?? null);
      }

      if (activeEditingCode) {
        const current = next.find((intent) => intent.intentCode === activeEditingCode);
        if (current) {
          setEditingIntentCode(current.intentCode);
          setForm(buildFormFromIntent(current));
          setSchemaText(JSON.stringify(current.requestSchema, null, 2));
          setMappingText(JSON.stringify(current.fieldMapping, null, 2));
        }
      }

      if (options.source === "manual") {
        setStatusText("已刷新意图清单");
      } else if (options.source === "init") {
        setStatusText("管理台已就绪");
      }

      return next;
    } catch (error) {
      const message = error instanceof Error ? error.message : "未知错误";
      setErrorText(message);
      setStatusText("加载意图清单失败");
      return [];
    }
  }

  useEffect(() => {
    void refresh({ source: "init" });
  }, []);

  function resetForm(nextStatusText = "已切换到新建草稿") {
    setEditingIntentCode(null);
    setForm(initialForm);
    setSchemaText(JSON.stringify(initialForm.requestSchema, null, 2));
    setMappingText(JSON.stringify(initialForm.fieldMapping, null, 2));
    setWorkspaceMode("compose");
    setStatusText(nextStatusText);
    setErrorText(null);
  }

  function beginCreateIntent() {
    resetForm("已创建新草稿");
  }

  function focusIntent(intent: IntentDefinition) {
    setFocusedIntentCode(intent.intentCode);
    setErrorText(null);
  }

  function loadIntent(intent: IntentDefinition) {
    setFocusedIntentCode(intent.intentCode);
    setEditingIntentCode(intent.intentCode);
    setWorkspaceMode("compose");
    setForm(buildFormFromIntent(intent));
    setSchemaText(JSON.stringify(intent.requestSchema, null, 2));
    setMappingText(JSON.stringify(intent.fieldMapping, null, 2));
    setStatusText(`正在编辑 ${intent.intentCode}`);
    setErrorText(null);
  }

  async function changeStatus(intent: IntentDefinition, nextAction: "activate" | "deactivate") {
    try {
      setFocusedIntentCode(intent.intentCode);
      setWorkspaceMode("release");
      setErrorText(null);
      setStatusText(nextAction === "activate" ? `正在生效 ${intent.intentCode}...` : `正在停用 ${intent.intentCode}...`);

      if (nextAction === "activate") {
        await api.activateIntent(intent.intentCode);
      } else {
        await api.deactivateIntent(intent.intentCode);
      }

      await refresh({
        syncIntentCode: editingIntentCode,
        syncFocusedCode: intent.intentCode
      });

      setStatusText(nextAction === "activate" ? `${intent.intentCode} 已生效，等待 router 同步` : `${intent.intentCode} 已停用`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "未知错误";
      setErrorText(message);
      setStatusText("状态变更失败");
    }
  }

  async function onSubmit() {
    try {
      setErrorText(null);

      const payload = {
        ...form,
        intentCode: form.intentCode.trim(),
        name: form.name.trim(),
        description: form.description.trim(),
        agentUrl: form.agentUrl.trim(),
        examples: form.examples.filter(Boolean),
        requestSchema: parseJsonObject(schemaText, {}),
        fieldMapping: parseJsonObject(mappingText, {}) as Record<string, string>
      };

      const targetCode = payload.intentCode;

      setStatusText(editingIntentCode ? `正在保存 ${editingIntentCode}...` : "正在注册新意图...");

      if (editingIntentCode) {
        await api.updateIntent(editingIntentCode, payload);
      } else {
        await api.createIntent(payload);
      }

      setEditingIntentCode(targetCode);
      setFocusedIntentCode(targetCode);

      await refresh({
        syncIntentCode: targetCode,
        syncFocusedCode: targetCode
      });

      if (editingIntentCode) {
        setWorkspaceMode("compose");
        setStatusText(`已保存 ${targetCode}`);
      } else {
        setWorkspaceMode("release");
        setStatusText(`已注册 ${targetCode}，进入发布控制`);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "未知错误";
      setErrorText(message);
      setStatusText(editingIntentCode ? "保存失败" : "注册失败");
    }
  }

  const parsedSchema = safeParseJsonObject(schemaText);
  const parsedMapping = safeParseJsonObject(mappingText);
  const effectiveSchema = parsedSchema ?? form.requestSchema;
  const effectiveMapping =
    parsedMapping === null
      ? form.fieldMapping
      : Object.fromEntries(Object.entries(parsedMapping).map(([key, value]) => [key, String(value)]));

  const draftIssues = [
    form.intentCode.trim() ? null : "缺少意图编码",
    form.name.trim() ? null : "缺少展示名称",
    parsedSchema === null ? "请求 Schema 不是合法 JSON 对象" : null,
    parsedMapping === null ? "字段映射不是合法 JSON 对象" : null,
    ...getIntentIssues({
      description: form.description,
      agentUrl: form.agentUrl,
      requestSchema: effectiveSchema,
      fieldMapping: effectiveMapping
    })
  ].filter((issue): issue is string => Boolean(issue));

  const focusedIntent = focusedIntentCode ? intents.find((intent) => intent.intentCode === focusedIntentCode) ?? null : intents[0] ?? null;
  const inspectedIntent =
    workspaceMode === "registry"
      ? focusedIntent
      : editingIntentCode
        ? intents.find((intent) => intent.intentCode === editingIntentCode) ?? focusedIntent
        : focusedIntent;

  const searchNeedle = searchText.trim().toLowerCase();
  const filteredIntents = intents
    .filter((intent) => registryFilter === "all" || intent.status === registryFilter)
    .filter((intent) => {
      if (!searchNeedle) return true;

      const haystack = [
        intent.intentCode,
        intent.name,
        intent.description,
        intent.agentUrl,
        ...intent.examples
      ]
        .join(" ")
        .toLowerCase();

      return haystack.includes(searchNeedle);
    })
    .sort((left, right) => {
      const leftRank = left.status === "active" ? 0 : left.status === "inactive" ? 1 : 2;
      const rightRank = right.status === "active" ? 0 : right.status === "inactive" ? 1 : 2;

      if (leftRank !== rightRank) return leftRank - rightRank;

      return (right.updatedAt ?? "").localeCompare(left.updatedAt ?? "");
    });

  const activeCount = intents.filter((intent) => intent.status === "active").length;
  const activeCoverage = intents.length ? Math.round((activeCount / intents.length) * 100) : 0;
  const attentionCount = intents.filter((intent) => intent.status !== "active" || getIntentIssues(intent).length > 0).length;
  const overviewQueue = intents
    .map((intent) => {
      const issues = getIntentIssues(intent);

      if (issues.length > 0) {
        return {
          intent,
          note: issues[0],
          action: "edit" as const
        };
      }

      if (intent.status !== "active") {
        return {
          intent,
          note: "契约完整，可进入发布控制",
          action: "release" as const
        };
      }

      return null;
    })
    .filter((item): item is { intent: IntentDefinition; note: string; action: "edit" | "release" } => Boolean(item))
    .sort((left, right) => {
      if (left.action !== right.action) return left.action === "edit" ? -1 : 1;
      return (right.intent.updatedAt ?? "").localeCompare(left.intent.updatedAt ?? "");
    })
    .slice(0, 6);
  const recentUpdates = [...intents]
    .sort((left, right) => (right.updatedAt ?? "").localeCompare(left.updatedAt ?? ""))
    .slice(0, 6);
  const draftHasContent = hasDraftContent(form);
  const formReadyForActivation = draftIssues.length === 0;
  const releaseTarget = editingIntentCode
    ? {
        ...form,
        status: inspectedIntent?.status ?? "inactive",
        requestSchema: effectiveSchema,
        fieldMapping: effectiveMapping
      }
    : inspectedIntent;
  const releaseIssues =
    editingIntentCode || draftHasContent
      ? draftIssues
      : inspectedIntent
        ? getIntentIssues(inspectedIntent)
        : [];
  const releaseStatus = releaseTarget?.status ?? "inactive";
  const releaseRequiredCount = releaseTarget ? countRequiredFields(releaseTarget.requestSchema) : 0;
  const releaseMappingCount = releaseTarget ? countMappings(releaseTarget.fieldMapping) : 0;
  const releaseCanActivate = Boolean(
    releaseTarget &&
      ("createdAt" in releaseTarget ? true : editingIntentCode !== null) &&
      releaseIssues.length === 0 &&
      releaseTarget.intentCode.trim()
  );

  const navigationItems: Array<{
    id: WorkspaceMode;
    label: string;
    counter: string;
  }> = [
    {
      id: "overview",
      label: "总览",
      counter: `${activeCoverage}%`
    },
    {
      id: "registry",
      label: "意图目录",
      counter: `${intents.length}`
    },
    {
      id: "compose",
      label: "注册编辑",
      counter: editingIntentCode ? "编辑中" : draftHasContent ? "草稿" : "新建"
    },
    {
      id: "release",
      label: "发布控制",
      counter: inspectedIntent ? statusLabelMap[inspectedIntent.status] : "待选"
    }
  ];

  const workspaceCopy = workspaceCopyMap[workspaceMode];
  const showInspector = workspaceMode === "registry";

  return (
    <div className="console-app">
      <aside className="console-sidebar">
        <div className="brand-lockup shell-enter shell-enter-1">
          <div className="brand-mark">IR</div>
          <div>
            <strong>Intent Router</strong>
            <span>Admin Control</span>
          </div>
        </div>

        <nav className="sidebar-nav shell-enter shell-enter-2">
          {navigationItems.map((item) => (
            <button
              key={item.id}
              className={`nav-item ${workspaceMode === item.id ? "active" : ""}`}
              onClick={() => setWorkspaceMode(item.id)}
              type="button"
            >
              <div className="nav-item-head">
                <strong>{item.label}</strong>
                <span>{item.counter}</span>
              </div>
            </button>
          ))}
        </nav>

        <div className="sidebar-actions shell-enter shell-enter-3">
          <button className="primary-button" onClick={beginCreateIntent} type="button">
            新建意图
          </button>
          <Link className="secondary-button" href="/perf-tests">
            性能压测
          </Link>
          <button className="ghost-button" onClick={() => void refresh({ source: "manual" })} type="button">
            刷新清单
          </button>
        </div>
      </aside>

      <div className="console-main">
        <header className="console-topbar shell-enter shell-enter-2">
          <div className="topbar-copy">
            <p className="eyebrow">{workspaceCopy.kicker}</p>
            <h1>{workspaceCopy.title}</h1>
            <p>{workspaceCopy.description}</p>
          </div>

          <div className="topbar-side">
            <div className={`runtime-badge ${errorText ? "warning" : "ok"}`}>
              <span>运行状态</span>
              <strong>{statusText}</strong>
            </div>

            <div className="runtime-meta">
              <div>
                <span>上次同步</span>
                <strong>{formatTimestamp(lastSyncedAt)}</strong>
              </div>
            </div>
          </div>
        </header>

        {errorText ? <div className="alert-strip shell-enter shell-enter-3">{errorText}</div> : null}

        <div className={`workspace-grid ${showInspector ? "" : "single"}`}>
          <main className="workspace-stage shell-enter shell-enter-3">
            {workspaceMode === "overview" ? (
              <section className="stage-section">
                <div className="section-head">
                  <div>
                    <p className="eyebrow">系统视图</p>
                    <h2>当前只看需要动作的项</h2>
                  </div>
                  <small>先补齐缺口，再生效完整项。</small>
                </div>

                <div className="metric-strip">
                  <div className="metric-cell">
                    <span>已注册意图</span>
                    <strong>{intents.length}</strong>
                    <small>管理端总量</small>
                  </div>
                  <div className="metric-cell">
                    <span>已生效</span>
                    <strong>{activeCount}</strong>
                    <small>{activeCoverage}% 已进入路由</small>
                  </div>
                  <div className="metric-cell">
                    <span>待处理</span>
                    <strong>{attentionCount}</strong>
                    <small>未生效或存在缺口</small>
                  </div>
                </div>

                <div className="overview-columns">
                  <section className="workspace-block">
                    <div className="block-head">
                      <h3>待处理队列</h3>
                      <small>把需要编辑和待生效的项放在同一处</small>
                    </div>

                    {overviewQueue.length === 0 ? (
                      <div className="empty-state">
                        <strong>当前没有待处理项。</strong>
                        <p>目录已经比较干净，下一步只需要关注新增意图。</p>
                      </div>
                    ) : (
                      <div className="list-stack">
                        {overviewQueue.map(({ intent, note, action }) => (
                          <button
                            key={intent.intentCode}
                            className="list-row"
                            onClick={() => {
                              if (action === "edit") {
                                loadIntent(intent);
                                return;
                              }

                              focusIntent(intent);
                              setWorkspaceMode("release");
                            }}
                            type="button"
                          >
                            <div>
                              <strong>{intent.name}</strong>
                              <small>{intent.intentCode}</small>
                            </div>
                            <div className="list-row-tail">
                              <span className={`status-pill ${intent.status}`}>{statusLabelMap[intent.status]}</span>
                              <small>{note}</small>
                            </div>
                          </button>
                        ))}
                      </div>
                    )}
                  </section>

                  <section className="workspace-block">
                    <div className="block-head">
                      <h3>最近更新</h3>
                      <small>按最近编辑时间排序</small>
                    </div>

                    {recentUpdates.length === 0 ? (
                      <div className="empty-state">
                        <strong>还没有意图记录。</strong>
                        <p>先在注册编辑页创建一个新意图。</p>
                      </div>
                    ) : (
                      <div className="activity-stack">
                        {recentUpdates.map((intent) => (
                          <article key={intent.intentCode} className="activity-row">
                            <div>
                              <strong>{intent.name}</strong>
                              <small>{intent.intentCode}</small>
                            </div>
                            <div className="activity-meta">
                              <span className={`status-pill ${intent.status}`}>{statusLabelMap[intent.status]}</span>
                              <small>{formatTimestamp(intent.updatedAt)}</small>
                            </div>
                          </article>
                        ))}
                      </div>
                    )}
                  </section>
                </div>
              </section>
            ) : null}

            {workspaceMode === "registry" ? (
              <section className="stage-section">
                <div className="section-head">
                  <div>
                    <p className="eyebrow">检索视图</p>
                    <h2>意图目录</h2>
                  </div>
                  <small>{filteredIntents.length} 条命中记录</small>
                </div>

                <div className="toolbar-band">
                  <input
                    className="search-input"
                    placeholder="按代码、名称、描述、示例或 URL 搜索"
                    value={searchText}
                    onChange={(event) => setSearchText(event.target.value)}
                  />

                  <div className="filter-row">
                    {(["all", "active", "inactive", "grayscale"] as RegistryFilter[]).map((filter) => (
                      <button
                        key={filter}
                        className={`filter-chip ${registryFilter === filter ? "selected" : ""}`}
                        onClick={() => setRegistryFilter(filter)}
                        type="button"
                      >
                        {filterLabelMap[filter]}
                      </button>
                    ))}
                  </div>
                </div>

                <div className="registry-table">
                  <div className="registry-head">
                    <span>意图</span>
                    <span>契约与状态</span>
                    <span>更新时间</span>
                  </div>

                  {filteredIntents.length === 0 ? (
                    <div className="empty-state">
                      <strong>当前筛选没有命中意图。</strong>
                      <p>你可以调整筛选条件，或者新建一个意图草稿。</p>
                    </div>
                  ) : (
                    filteredIntents.map((intent) => {
                      const issues = getIntentIssues(intent);

                      return (
                        <article
                          key={intent.intentCode}
                          className={`registry-row ${focusedIntentCode === intent.intentCode ? "selected" : ""}`}
                          onClick={() => focusIntent(intent)}
                        >
                          <div className="registry-primary">
                            <div className="registry-title">
                              <strong>{intent.name}</strong>
                              <code>{intent.intentCode}</code>
                            </div>
                            <p>{intent.description}</p>
                            <small className="registry-summary">
                              {intent.examples.length} 条示例 · {countRequiredFields(intent.requestSchema)} 个必填字段
                            </small>
                          </div>

                          <div className="registry-contract">
                            <span className={`status-pill ${intent.status}`}>{statusLabelMap[intent.status]}</span>
                            <small>
                              {issues.length === 0
                                ? intent.status === "active"
                                  ? "已在路由清单中"
                                  : "契约完整，可进入发布控制"
                                : `${issues.length} 个缺口：${issues[0]}`}
                            </small>
                          </div>

                          <div className="registry-side">
                            <small>{formatTimestamp(intent.updatedAt)}</small>
                            <div className="row-actions">
                              <button
                                className="secondary-button"
                                onClick={(event) => {
                                  event.stopPropagation();
                                  loadIntent(intent);
                                }}
                                type="button"
                              >
                                编辑
                              </button>
                              {intent.status === "active" ? (
                                <button
                                  className="ghost-button"
                                  onClick={(event) => {
                                    event.stopPropagation();
                                    void changeStatus(intent, "deactivate");
                                  }}
                                  type="button"
                                >
                                  停用
                                </button>
                              ) : (
                                <button
                                  className="primary-button"
                                  onClick={(event) => {
                                    event.stopPropagation();
                                    void changeStatus(intent, "activate");
                                  }}
                                  type="button"
                                >
                                  生效
                                </button>
                              )}
                            </div>
                          </div>
                        </article>
                      );
                    })
                  )}
                </div>
              </section>
            ) : null}

            {workspaceMode === "compose" ? (
              <section className="stage-section">
                <div className="section-head">
                  <div>
                    <p className="eyebrow">配置工作区</p>
                    <h2>{editingIntentCode ? `编辑 ${editingIntentCode}` : "新建意图草稿"}</h2>
                  </div>
                  <small>{editingIntentCode ? "保存后再进入发布控制" : "新建意图默认未生效"}</small>
                </div>

                <section className="workspace-block">
                  <div className="block-head">
                    <h3>识别信息</h3>
                    <small>这部分进入路由识别上下文</small>
                  </div>

                  <div className="form-grid">
                    <label>
                      <span>意图编码</span>
                      <input
                        placeholder="query_order_status"
                        value={form.intentCode}
                        onChange={(event) => setForm({ ...form, intentCode: event.target.value })}
                      />
                    </label>

                    <label>
                      <span>展示名称</span>
                      <input
                        placeholder="查询订单状态"
                        value={form.name}
                        onChange={(event) => setForm({ ...form, name: event.target.value })}
                      />
                    </label>

                    <label className="span-2">
                      <span>意图描述</span>
                      <textarea
                        placeholder="明确这个意图解决什么问题，避免与相邻意图混淆。"
                        value={form.description}
                        onChange={(event) => setForm({ ...form, description: event.target.value })}
                      />
                    </label>

                    <label className="span-2">
                      <span>示例表达</span>
                      <input
                        placeholder="帮我查下订单状态, 订单123到哪了"
                        value={form.examples.join(", ")}
                        onChange={(event) =>
                          setForm({
                            ...form,
                            examples: event.target.value
                              .split(",")
                              .map((item) => item.trim())
                              .filter(Boolean)
                          })
                        }
                      />
                    </label>
                  </div>
                </section>

                <section className="workspace-block">
                  <div className="block-head">
                    <h3>分发策略</h3>
                    <small>router 只做 HTTP 分发；兜底意图不参与识别，只在未命中时触发</small>
                  </div>

                  <div className="form-grid">
                    <label className="span-2">
                      <span>Agent 接口地址</span>
                      <input
                        placeholder="http://intent-your-agent.intent.svc.cluster.local:8000/api/agent/run"
                        value={form.agentUrl}
                        onChange={(event) => setForm({ ...form, agentUrl: event.target.value })}
                      />
                    </label>

                    <label className="span-2">
                      <span>路由角色</span>
                      <div className="token-list">
                        <label className="toggle-chip">
                          <input
                            checked={form.isFallback}
                            onChange={(event) => setForm({ ...form, isFallback: event.target.checked })}
                            type="checkbox"
                          />
                          <span>作为兜底意图</span>
                        </label>
                      </div>
                      <small>{form.isFallback ? "不会进入识别清单，只在未命中时被分发。" : "会进入识别清单，供路由提示词读取。"}</small>
                    </label>

                    <label>
                      <span>调度优先级</span>
                      <input
                        type="number"
                        value={form.dispatchPriority}
                        onChange={(event) => setForm({ ...form, dispatchPriority: Number(event.target.value) })}
                      />
                    </label>

                    <label>
                      <span>恢复策略</span>
                      <input
                        value={form.resumePolicy}
                        onChange={(event) => setForm({ ...form, resumePolicy: event.target.value })}
                      />
                    </label>
                  </div>
                </section>

                <section className="workspace-block">
                  <div className="block-head">
                    <h3>请求契约</h3>
                    <small>Schema 约束字段，映射负责装配发送给 agent 的请求体</small>
                  </div>

                  <div className="contract-grid">
                    <label>
                      <span>请求 Schema</span>
                      <textarea className="code-area" value={schemaText} onChange={(event) => setSchemaText(event.target.value)} />
                    </label>

                    <label>
                      <span>字段映射</span>
                      <textarea className="code-area" value={mappingText} onChange={(event) => setMappingText(event.target.value)} />
                    </label>
                  </div>
                </section>

                <section className="workspace-block action-block">
                  <div className="readiness-strip">
                    <div>
                      <span>发布准备度</span>
                      <strong>{formReadyForActivation ? "可以进入发布控制" : "还存在待补字段"}</strong>
                    </div>
                    <div>
                      <span>Schema 必填数</span>
                      <strong>{countRequiredFields(effectiveSchema)}</strong>
                    </div>
                    <div>
                      <span>映射数量</span>
                      <strong>{countMappings(effectiveMapping)}</strong>
                    </div>
                  </div>

                  {draftIssues.length > 0 ? (
                    <div className="issue-list">
                      {draftIssues.map((issue) => (
                        <span key={issue}>{issue}</span>
                      ))}
                    </div>
                  ) : (
                    <div className="success-note">当前草稿字段完整，保存后即可进入发布控制。</div>
                  )}

                  <div className="action-row">
                    <button className="primary-button" onClick={onSubmit} type="button">
                      {editingIntentCode ? "保存修改" : "注册意图"}
                    </button>

                    <button
                      className="secondary-button"
                      onClick={() => {
                        if (focusedIntent) {
                          loadIntent(focusedIntent);
                        }
                      }}
                      type="button"
                    >
                      载入当前焦点
                    </button>

                    <button className="ghost-button" onClick={() => resetForm()} type="button">
                      {editingIntentCode ? "取消编辑" : "清空草稿"}
                    </button>
                  </div>
                </section>
              </section>
            ) : null}

            {workspaceMode === "release" ? (
              <section className="stage-section">
                <div className="section-head">
                  <div>
                    <p className="eyebrow">发布检查</p>
                    <h2>{releaseTarget?.name ?? "未选择意图"}</h2>
                  </div>
                  <small>{releaseTarget?.intentCode ?? "先在目录中选择或在编辑页保存一个意图"}</small>
                </div>

                <div className="release-layout">
                  <section className="workspace-block emphasis-block">
                    <div className="block-head">
                      <h3>状态切换</h3>
                      <small>确认契约完整后再执行生效</small>
                    </div>

                    <div className="release-summary">
                      <span className={`status-pill ${releaseStatus}`}>{statusLabelMap[releaseStatus]}</span>
                      <strong>{releaseTarget?.intentCode ?? "未选择意图"}</strong>
                      <p>
                        {releaseStatus === "active"
                          ? "该意图已在生效清单中，router 会在刷新周期内继续同步。"
                          : "该意图尚未进入生效清单，切换状态后 router 才会读取。"}
                      </p>
                    </div>

                    <div className="action-row">
                      {inspectedIntent ? (
                        inspectedIntent.status === "active" ? (
                          <button className="ghost-button" onClick={() => void changeStatus(inspectedIntent, "deactivate")} type="button">
                            停用当前意图
                          </button>
                        ) : (
                          <button
                            className="primary-button"
                            disabled={!releaseCanActivate}
                            onClick={() => void changeStatus(inspectedIntent, "activate")}
                            type="button"
                          >
                            生效当前意图
                          </button>
                        )
                      ) : (
                        <button className="primary-button" disabled type="button">
                          先保存或选择意图
                        </button>
                      )}

                      <button className="secondary-button" onClick={() => setWorkspaceMode("compose")} type="button">
                        返回编辑
                      </button>
                    </div>
                  </section>

                  <section className="workspace-block">
                    <div className="block-head">
                      <h3>发布检查项</h3>
                      <small>只保留会影响识别或分发的关键项</small>
                    </div>

                    <div className="check-grid">
                      <article>
                        <span>意图描述</span>
                        <strong>{releaseTarget?.description.trim() ? "已配置" : "缺失"}</strong>
                      </article>
                      <article>
                        <span>Agent 地址</span>
                        <strong>{releaseTarget?.agentUrl.trim() ? "已配置" : "缺失"}</strong>
                      </article>
                      <article>
                        <span>路由角色</span>
                        <strong>{releaseTarget?.isFallback ? "兜底意图" : "普通意图"}</strong>
                      </article>
                      <article>
                        <span>必填字段</span>
                        <strong>{releaseRequiredCount}</strong>
                      </article>
                      <article>
                        <span>字段映射</span>
                        <strong>{releaseMappingCount}</strong>
                      </article>
                    </div>

                    {releaseIssues.length > 0 ? (
                      <div className="issue-list">
                        {releaseIssues.map((issue) => (
                          <span key={issue}>{issue}</span>
                        ))}
                      </div>
                    ) : (
                      <div className="success-note">当前配置已具备发布条件，切换为“已生效”后等待 router 刷新即可。</div>
                    )}
                  </section>
                </div>
              </section>
            ) : null}
          </main>

          {showInspector ? (
            <aside className="workspace-inspector shell-enter shell-enter-4">
              <div className="inspector-head">
                <p className="eyebrow">当前检查器</p>
                <h2>{inspectedIntent?.name ?? "未选择意图"}</h2>
                <p>{inspectedIntent?.description || "先从目录中选一个意图，再查看契约和操作。"}</p>
              </div>

                <div className="inspector-metrics">
                  <div>
                    <span>状态</span>
                    <strong>{inspectedIntent ? statusLabelMap[inspectedIntent.status] : "未选择"}</strong>
                  </div>
                  <div>
                    <span>路由角色</span>
                    <strong>{inspectedIntent?.isFallback ? "兜底" : "普通"}</strong>
                  </div>
                  <div>
                    <span>必填字段</span>
                    <strong>{inspectedIntent ? countRequiredFields(inspectedIntent.requestSchema) : 0}</strong>
                  </div>
                  <div>
                  <span>字段映射</span>
                  <strong>{inspectedIntent ? countMappings(inspectedIntent.fieldMapping) : 0}</strong>
                </div>
              </div>

              <section className="inspector-section">
                <div className="block-head">
                  <h3>Agent 目标</h3>
                  <small>目录里不展开 URL，这里单独看</small>
                </div>

                {inspectedIntent?.agentUrl ? (
                  <code className="inspector-code">{inspectedIntent.agentUrl}</code>
                ) : (
                  <div className="empty-state compact">
                    <strong>当前没有可查看的目标地址。</strong>
                  </div>
                )}
              </section>

              <section className="inspector-section">
                <div className="block-head">
                  <h3>当前判断</h3>
                  <small>这里只显示是否有明显缺口</small>
                </div>

                {!inspectedIntent ? (
                  <div className="empty-state compact">
                    <strong>先选择一个意图。</strong>
                  </div>
                ) : getIntentIssues(inspectedIntent).length > 0 ? (
                  <div className="issue-list">
                    {getIntentIssues(inspectedIntent).map((issue) => (
                      <span key={issue}>{issue}</span>
                    ))}
                  </div>
                ) : (
                  <div className="success-note">当前契约完整，可以进入发布控制切换状态。</div>
                )}
              </section>

              {inspectedIntent?.isFallback ? (
                <section className="inspector-section">
                  <div className="block-head">
                    <h3>兜底行为</h3>
                    <small>不会参与识别，只作为未命中时的默认分发目标</small>
                  </div>

                  <div className="plain-copy">建议让兜底 Agent 负责澄清用户诉求，而不是在路由器里处理业务。</div>
                </section>
              ) : null}

              {(inspectedIntent?.examples.length ?? 0) > 0 ? (
                <section className="inspector-section">
                  <div className="block-head">
                    <h3>示例表达</h3>
                    <small>只保留帮助判断边界的内容</small>
                  </div>

                  <div className="token-list">
                    {inspectedIntent?.examples.map((example) => (
                      <span key={example}>{example}</span>
                    ))}
                  </div>
                </section>
              ) : null}

              <section className="inspector-section">
                <div className="block-head">
                  <h3>快速操作</h3>
                  <small>从目录直接进入下一步</small>
                </div>

                <div className="stack-actions">
                  {inspectedIntent ? (
                    <>
                      <button className="secondary-button" onClick={() => loadIntent(inspectedIntent)} type="button">
                        编辑当前意图
                      </button>
                      {inspectedIntent.status === "active" ? (
                        <button className="ghost-button" onClick={() => void changeStatus(inspectedIntent, "deactivate")} type="button">
                          停用当前意图
                        </button>
                      ) : (
                        <button className="primary-button" onClick={() => void changeStatus(inspectedIntent, "activate")} type="button">
                          生效当前意图
                        </button>
                      )}
                    </>
                  ) : (
                    <button className="secondary-button" onClick={beginCreateIntent} type="button">
                      开始新建草稿
                    </button>
                  )}
                </div>
              </section>
            </aside>
          ) : null}
        </div>
      </div>
    </div>
  );
}
