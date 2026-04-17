"use client";

import Link from "next/link";
import { startTransition, useEffect, useState } from "react";
import { IntentRouterApiClient } from "@intent-router/api-client";
import type {
  PerfFailureSample,
  PerfMetrics,
  PerfStageResult,
  PerfTestCase,
  PerfTestRunDetail,
  PerfTestRunSummary,
  PerfTestStepInput
} from "@intent-router/shared-types";
import styles from "./page.module.css";

const api = new IntentRouterApiClient();
const POLL_INTERVAL_MS = 3000;

type AsyncState = "idle" | "loading" | "ready" | "error";

function createStepDraft(index: number): PerfTestStepInput {
  return {
    name: `阶段 ${index + 1}`,
    concurrency: index === 0 ? 10 : 50,
    durationSec: 60,
    warmupSec: 10,
    timeoutMs: 10000
  };
}

function isActiveRunStatus(status?: PerfTestRunSummary["status"] | null) {
  return status === "queued" || status === "validating" || status === "running";
}

function formatTimestamp(value?: string | null) {
  if (!value) return "暂无";
  return new Date(value).toLocaleString("zh-CN");
}

function formatDecimal(value?: number | null, digits = 0) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "--";
  }
  return value.toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  });
}

function formatPercent(value?: number | null) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "--";
  }
  const normalized = value > 1 ? value : value * 100;
  return `${normalized.toFixed(normalized >= 99 || normalized === 0 ? 1 : 2)}%`;
}

function formatLatency(value?: number | null) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "--";
  }
  return `${formatDecimal(value, value >= 100 ? 0 : 1)} ms`;
}

function formatRps(value?: number | null) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "--";
  }
  return `${formatDecimal(value, value >= 100 ? 0 : 1)} rps`;
}

function formatStatusLabel(status: PerfTestRunSummary["status"] | PerfStageResult["status"]) {
  switch (status) {
    case "queued":
      return "排队中";
    case "validating":
      return "校验中";
    case "running":
      return "运行中";
    case "completed":
      return "已完成";
    case "failed":
      return "失败";
    case "cancelled":
      return "已取消";
    case "pending":
      return "待开始";
    default:
      return status;
  }
}

function buildSummaryText(run: PerfTestRunDetail, compareRun?: PerfTestRunDetail | null) {
  const lines = [
    `run_id: ${run.runId}`,
    `case_id: ${run.caseId}`,
    `status: ${run.status}`,
    `started_at: ${run.startedAt ?? "unknown"}`,
    `finished_at: ${run.finishedAt ?? "unknown"}`,
    `rps: ${formatRps(run.aggregateMetrics?.rps)}`,
    `success_rate: ${formatPercent(run.aggregateMetrics?.successRate)}`,
    `p95: ${formatLatency(run.aggregateMetrics?.p95Ms)}`,
    `p99: ${formatLatency(run.aggregateMetrics?.p99Ms)}`,
    `failures: ${formatDecimal(run.aggregateMetrics?.failureCount)}`
  ];

  if (compareRun) {
    lines.push(
      `baseline_run_id: ${compareRun.runId}`,
      `baseline_rps: ${formatRps(compareRun.aggregateMetrics?.rps)}`,
      `baseline_p95: ${formatLatency(compareRun.aggregateMetrics?.p95Ms)}`,
      `baseline_p99: ${formatLatency(compareRun.aggregateMetrics?.p99Ms)}`
    );
  }

  return lines.join("\n");
}

function getDeltaTone(
  currentValue?: number | null,
  baselineValue?: number | null,
  preference: "higher" | "lower" = "higher"
) {
  if (typeof currentValue !== "number" || typeof baselineValue !== "number") {
    return "neutral";
  }

  if (currentValue === baselineValue) {
    return "neutral";
  }

  const isImproved = preference === "higher" ? currentValue > baselineValue : currentValue < baselineValue;
  return isImproved ? "good" : "danger";
}

function formatDelta(
  currentValue: number | null | undefined,
  baselineValue: number | null | undefined,
  kind: "rps" | "percent" | "latency"
) {
  if (typeof currentValue !== "number" || typeof baselineValue !== "number") {
    return "缺少基线";
  }

  const delta = currentValue - baselineValue;
  const prefix = delta > 0 ? "+" : "";

  if (kind === "percent") {
    const normalized = delta > 1 || delta < -1 ? delta : delta * 100;
    return `${prefix}${normalized.toFixed(1)} pt`;
  }

  if (kind === "rps") {
    return `${prefix}${formatDecimal(delta, Math.abs(delta) >= 100 ? 0 : 1)} rps`;
  }

  return `${prefix}${formatDecimal(delta, Math.abs(delta) >= 100 ? 0 : 1)} ms`;
}

function pickCurrentStage(run: PerfTestRunDetail | null) {
  if (!run) return null;
  const runningStage = run.stepResults.find((stage) => stage.status === "running");
  if (runningStage) return runningStage;

  if (typeof run.progress.currentStageIndex === "number") {
    return run.stepResults.find((stage) => stage.stepIndex === run.progress.currentStageIndex) ?? null;
  }

  return run.stepResults[run.stepResults.length - 1] ?? null;
}

function collectFailureSamples(run: PerfTestRunDetail | null) {
  if (!run) return [];
  const stagedSamples = run.stepResults.flatMap((stage) => stage.failureSamples ?? []);
  const merged = [...run.errorSamples, ...stagedSamples];
  const unique = new Map<string, PerfFailureSample>();

  merged.forEach((sample, index) => {
    unique.set(sample.sampleId || `sample-${index + 1}`, sample);
  });

  return [...unique.values()].slice(0, 8);
}

function MetricCell(props: {
  label: string;
  value: string;
  hint?: string;
  tone?: "default" | "good" | "danger";
}) {
  return (
    <div className={`${styles.metricCell} ${props.tone ? styles[`metricTone${capitalize(props.tone)}`] : ""}`}>
      <span>{props.label}</span>
      <strong>{props.value}</strong>
      <small>{props.hint ?? " "}</small>
    </div>
  );
}

function capitalize(value: string) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function StatusPill({ status }: { status: PerfTestRunSummary["status"] | PerfStageResult["status"] }) {
  return <span className={`${styles.statusPill} ${styles[`status${capitalize(status)}`]}`}>{formatStatusLabel(status)}</span>;
}

function BreakdownList({
  title,
  values,
  emptyLabel
}: {
  title: string;
  values?: Record<string, number>;
  emptyLabel: string;
}) {
  const entries = Object.entries(values ?? {}).sort((left, right) => right[1] - left[1]);

  return (
    <section className={styles.breakdownBlock}>
      <div className={styles.sectionHeading}>
        <div>
          <p className={styles.eyebrow}>分布</p>
          <h2>{title}</h2>
        </div>
      </div>
      {entries.length === 0 ? (
        <div className={styles.emptyState}>
          <strong>{emptyLabel}</strong>
          <p>本轮没有足够样本，后续轮询会继续补充。</p>
        </div>
      ) : (
        <div className={styles.breakdownList}>
          {entries.map(([key, count]) => (
            <div key={key} className={styles.breakdownRow}>
              <span>{key}</span>
              <strong>{formatDecimal(count)}</strong>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

export default function PerfTestsPage() {
  const [cases, setCases] = useState<PerfTestCase[]>([]);
  const [runs, setRuns] = useState<PerfTestRunSummary[]>([]);
  const [selectedCaseId, setSelectedCaseId] = useState("");
  const [ladderSteps, setLadderSteps] = useState<PerfTestStepInput[]>([createStepDraft(0), createStepDraft(1)]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<PerfTestRunDetail | null>(null);
  const [compareRunId, setCompareRunId] = useState<string | null>(null);
  const [compareRun, setCompareRun] = useState<PerfTestRunDetail | null>(null);
  const [workspaceStatus, setWorkspaceStatus] = useState("正在准备压测工作台");
  const [errorText, setErrorText] = useState<string | null>(null);
  const [casesState, setCasesState] = useState<AsyncState>("loading");
  const [runsState, setRunsState] = useState<AsyncState>("loading");
  const [detailState, setDetailState] = useState<AsyncState>("idle");
  const [compareState, setCompareState] = useState<AsyncState>("idle");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [copied, setCopied] = useState(false);
  const [lastPolledAt, setLastPolledAt] = useState<string | null>(null);

  const selectedCase = cases.find((item) => item.caseId === selectedCaseId) ?? null;
  const orderedRuns = [...runs].sort((left, right) => (right.updatedAt ?? "").localeCompare(left.updatedAt ?? ""));
  const currentStage = pickCurrentStage(selectedRun);
  const currentMetrics = currentStage?.metrics ?? selectedRun?.aggregateMetrics ?? {};
  const failureSamples = collectFailureSamples(selectedRun);

  const stageRows = Array.from({
    length: Math.max(
      selectedRun?.ladderSteps.length ?? 0,
      selectedRun?.stepResults.length ?? 0,
      ladderSteps.length
    )
  }).map((_, index) => {
    const plannedStep = selectedRun?.ladderSteps[index] ?? ladderSteps[index];
    const stageResult =
      selectedRun?.stepResults.find((item) => item.stepIndex === index) ?? selectedRun?.stepResults[index] ?? null;

    return {
      key: stageResult?.stageId ?? `planned-${index + 1}`,
      stepIndex: index,
      name: stageResult?.name ?? plannedStep?.name ?? `阶段 ${index + 1}`,
      status:
        stageResult?.status ??
        (selectedRun && selectedRun.progress.currentStageIndex === index
          ? "running"
          : selectedRun && selectedRun.progress.completedStages > index
            ? "completed"
            : "pending"),
      concurrency: stageResult?.concurrency ?? plannedStep?.concurrency ?? 0,
      durationSec: stageResult?.durationSec ?? plannedStep?.durationSec ?? 0,
      warmupSec: stageResult?.warmupSec ?? plannedStep?.warmupSec ?? null,
      timeoutMs: stageResult?.timeoutMs ?? plannedStep?.timeoutMs ?? null,
      metrics: stageResult?.metrics ?? {},
      failureCount:
        stageResult?.metrics.failureCount ?? stageResult?.failureSamples?.length ?? null,
      startedAt: stageResult?.startedAt ?? null,
      finishedAt: stageResult?.finishedAt ?? null
    };
  });

  const validationIssues = [
    selectedCaseId ? null : "请选择测试用例",
    ladderSteps.length > 0 ? null : "至少需要一个阶梯步骤",
    ...ladderSteps.flatMap((step, index) => {
      const issues: string[] = [];
      if (!step.name?.trim()) issues.push(`阶段 ${index + 1} 缺少名称`);
      if (step.concurrency <= 0) issues.push(`阶段 ${index + 1} 并发数必须大于 0`);
      if (step.durationSec <= 0) issues.push(`阶段 ${index + 1} 持续时长必须大于 0`);
      if (typeof step.warmupSec === "number" && step.warmupSec < 0) issues.push(`阶段 ${index + 1} 预热时长不能为负数`);
      if (typeof step.timeoutMs === "number" && step.timeoutMs <= 0) issues.push(`阶段 ${index + 1} 超时阈值必须大于 0`);
      return issues;
    })
  ].filter((issue): issue is string => Boolean(issue));

  async function loadCases() {
    try {
      setCasesState("loading");
      const items = await api.listPerfTestCases();
      startTransition(() => {
        setCases(items);
        setCasesState("ready");
      });

      if (!selectedCaseId && items[0]) {
        setSelectedCaseId(items[0].caseId);
        setLadderSteps(
          items[0].defaultSteps.length > 0
            ? items[0].defaultSteps.map((step, index) => ({ ...step, name: step.name ?? `阶段 ${index + 1}` }))
            : [createStepDraft(0), createStepDraft(1)]
        );
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "加载测试用例失败";
      setCasesState("error");
      setErrorText(message);
      setWorkspaceStatus("测试用例加载失败");
    }
  }

  async function loadRuns(options: { silent?: boolean } = {}) {
    try {
      if (!options.silent) {
        setRunsState("loading");
      }
      const items = await api.listPerfTestRuns();
      startTransition(() => {
        setRuns(items);
        setRunsState("ready");
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : "加载压测记录失败";
      setRunsState("error");
      setErrorText(message);
      if (!options.silent) {
        setWorkspaceStatus("压测记录加载失败");
      }
    }
  }

  async function loadRunDetail(runId: string, options: { silent?: boolean } = {}) {
    try {
      if (!options.silent) {
        setDetailState("loading");
      }
      const detail = await api.getPerfTestRun(runId);
      startTransition(() => {
        setSelectedRun(detail);
        setDetailState("ready");
        setLastPolledAt(new Date().toISOString());
      });
      setWorkspaceStatus(
        detail.status === "running"
          ? `压测任务 ${detail.runId} 正在执行`
          : detail.status === "completed"
            ? `压测任务 ${detail.runId} 已完成`
            : detail.status === "failed"
              ? `压测任务 ${detail.runId} 执行失败`
              : `压测任务 ${detail.runId} 状态已更新`
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : "加载压测详情失败";
      setDetailState("error");
      setErrorText(message);
      if (!options.silent) {
        setWorkspaceStatus("压测详情加载失败");
      }
    }
  }

  async function loadCompareRun(runId: string, options: { silent?: boolean } = {}) {
    try {
      if (!options.silent) {
        setCompareState("loading");
      }
      const detail = await api.getPerfTestRun(runId);
      startTransition(() => {
        setCompareRun(detail);
        setCompareState("ready");
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : "加载基线详情失败";
      setCompareState("error");
      setErrorText(message);
    }
  }

  useEffect(() => {
    void Promise.all([loadCases(), loadRuns()]);
  }, []);

  useEffect(() => {
    if (orderedRuns.length === 0) return;
    if (selectedRunId && orderedRuns.some((item) => item.runId === selectedRunId)) return;
    setSelectedRunId(orderedRuns[0].runId);
  }, [orderedRuns, selectedRunId]);

  useEffect(() => {
    if (!selectedRunId) return;
    if (selectedRun?.runId === selectedRunId) return;
    void loadRunDetail(selectedRunId);
  }, [selectedRunId, selectedRun?.runId]);

  useEffect(() => {
    if (!compareRunId) {
      setCompareRun(null);
      setCompareState("idle");
      return;
    }
    if (compareRun?.runId === compareRunId) return;
    void loadCompareRun(compareRunId);
  }, [compareRun?.runId, compareRunId]);

  useEffect(() => {
    if (!selectedRunId || !isActiveRunStatus(selectedRun?.status ?? orderedRuns.find((item) => item.runId === selectedRunId)?.status)) {
      return;
    }

    const refresh = async () => {
      await Promise.all([loadRunDetail(selectedRunId, { silent: true }), loadRuns({ silent: true })]);
    };

    void refresh();
    const timer = window.setInterval(() => {
      void refresh();
    }, POLL_INTERVAL_MS);

    return () => window.clearInterval(timer);
  }, [orderedRuns, selectedRun?.status, selectedRunId]);

  async function handleStartRun() {
    if (validationIssues.length > 0 || !selectedCaseId) {
      setWorkspaceStatus("请先修正启动参数");
      return;
    }

    try {
      setIsSubmitting(true);
      setErrorText(null);
      setWorkspaceStatus("正在提交压测任务");

      const summary = await api.createPerfTestRun({
        caseId: selectedCaseId,
        ladderSteps: ladderSteps.map((step, index) => ({
          ...step,
          name: step.name?.trim() || `阶段 ${index + 1}`
        }))
      });

      setSelectedRunId(summary.runId);
      setCompareRunId(null);
      setCopied(false);
      await Promise.all([loadRuns(), loadRunDetail(summary.runId)]);
    } catch (error) {
      const message = error instanceof Error ? error.message : "提交压测任务失败";
      setErrorText(message);
      setWorkspaceStatus("压测任务启动失败");
    } finally {
      setIsSubmitting(false);
    }
  }

  function updateStep(index: number, patch: Partial<PerfTestStepInput>) {
    setLadderSteps((current) =>
      current.map((step, stepIndex) => (stepIndex === index ? { ...step, ...patch } : step))
    );
  }

  async function copySummary() {
    if (!selectedRun) return;
    await navigator.clipboard.writeText(buildSummaryText(selectedRun, compareRun));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  }

  const baselineMetrics = compareRun?.aggregateMetrics;
  const summaryMetrics = selectedRun?.aggregateMetrics ?? {};

  return (
    <div className={styles.shell}>
      <aside className={styles.leftRail}>
        <div className={styles.brandBlock}>
          <div>
            <p className={styles.eyebrow}>Admin</p>
            <h1>压测工作台</h1>
          </div>
          <p>
            服务端发压，固定走 <code>admin-api -&gt; router-api-test.svc</code>，
            前端仅做配置、监控与结果回放。
          </p>
        </div>

        <div className={styles.navLinks}>
          <Link className={styles.navLink} href="/">
            返回意图控制台
          </Link>
          <span className={styles.navHint}>
            目标服务固定为 <code>router-api-test</code>，页面不暴露环境切换。
          </span>
        </div>

        <section className={styles.panel}>
          <div className={styles.sectionHeading}>
            <div>
              <p className={styles.eyebrow}>配置</p>
              <h2>测试用例与阶梯</h2>
            </div>
            <small>{casesState === "loading" ? "加载中" : `${cases.length} 个用例`}</small>
          </div>

          <label className={styles.field}>
            <span>测试用例</span>
            <select
              value={selectedCaseId}
              onChange={(event) => {
                const nextCaseId = event.target.value;
                setSelectedCaseId(nextCaseId);
                const nextCase = cases.find((item) => item.caseId === nextCaseId);
                setLadderSteps(
                  nextCase?.defaultSteps.length
                    ? nextCase.defaultSteps.map((step, index) => ({ ...step, name: step.name ?? `阶段 ${index + 1}` }))
                    : [createStepDraft(0), createStepDraft(1)]
                );
              }}
            >
              {cases.length === 0 ? <option value="">暂无测试用例</option> : null}
              {cases.map((item) => (
                <option key={item.caseId} value={item.caseId}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>

          <div className={styles.caseSynopsis}>
            <strong>{selectedCase?.name ?? "未选择测试用例"}</strong>
            <p>{selectedCase?.description ?? "从 admin 后端加载结构化压测用例，前端只做选择和覆写。"}</p>
            <div className={styles.inlineMeta}>
              <span>{selectedCase?.category ?? "默认场景"}</span>
              <span>{selectedCase?.targetRoute ?? "router_only"}</span>
            </div>
          </div>

          <div className={styles.stepEditor}>
            {ladderSteps.map((step, index) => (
              <div key={`${step.name}-${index}`} className={styles.stepRow}>
                <div className={styles.stepHeader}>
                  <strong>{step.name || `阶段 ${index + 1}`}</strong>
                  <div className={styles.stepActions}>
                    <button
                      className={styles.inlineButton}
                      onClick={() =>
                        setLadderSteps((current) => {
                          const copy = [...current];
                          copy.splice(index + 1, 0, { ...step, name: `${step.name || `阶段 ${index + 1}`} 副本` });
                          return copy;
                        })
                      }
                      type="button"
                    >
                      复制
                    </button>
                    <button
                      className={styles.inlineButton}
                      disabled={ladderSteps.length <= 1}
                      onClick={() => setLadderSteps((current) => current.filter((_, stepIndex) => stepIndex !== index))}
                      type="button"
                    >
                      删除
                    </button>
                  </div>
                </div>

                <div className={styles.stepGrid}>
                  <label className={styles.field}>
                    <span>名称</span>
                    <input value={step.name ?? ""} onChange={(event) => updateStep(index, { name: event.target.value })} />
                  </label>
                  <label className={styles.field}>
                    <span>并发</span>
                    <input
                      type="number"
                      min={1}
                      value={step.concurrency}
                      onChange={(event) => updateStep(index, { concurrency: Number(event.target.value) })}
                    />
                  </label>
                  <label className={styles.field}>
                    <span>持续时长 / 秒</span>
                    <input
                      type="number"
                      min={1}
                      value={step.durationSec}
                      onChange={(event) => updateStep(index, { durationSec: Number(event.target.value) })}
                    />
                  </label>
                  <label className={styles.field}>
                    <span>预热 / 秒</span>
                    <input
                      type="number"
                      min={0}
                      value={step.warmupSec ?? 0}
                      onChange={(event) => updateStep(index, { warmupSec: Number(event.target.value) })}
                    />
                  </label>
                  <label className={styles.field}>
                    <span>请求上限</span>
                    <input
                      type="number"
                      min={0}
                      value={step.requestLimit ?? 0}
                      onChange={(event) =>
                        updateStep(index, {
                          requestLimit: Number(event.target.value) > 0 ? Number(event.target.value) : undefined
                        })
                      }
                    />
                  </label>
                  <label className={styles.field}>
                    <span>超时阈值 / ms</span>
                    <input
                      type="number"
                      min={1}
                      value={step.timeoutMs ?? 10000}
                      onChange={(event) => updateStep(index, { timeoutMs: Number(event.target.value) })}
                    />
                  </label>
                </div>
              </div>
            ))}
          </div>

          <div className={styles.controlRow}>
            <button className={styles.primaryButton} onClick={handleStartRun} disabled={isSubmitting || validationIssues.length > 0} type="button">
              {isSubmitting ? "提交中..." : "启动压测"}
            </button>
            <button
              className={styles.secondaryButton}
              onClick={() => setLadderSteps([...ladderSteps, createStepDraft(ladderSteps.length)])}
              type="button"
            >
              增加阶段
            </button>
          </div>

          {validationIssues.length > 0 ? (
            <div className={styles.issueList}>
              {validationIssues.map((issue) => (
                <span key={issue}>{issue}</span>
              ))}
            </div>
          ) : (
            <div className={styles.readyNote}>参数校验通过，启动后页面将轮询 `run detail` 并冻结最终结果。</div>
          )}
        </section>
      </aside>

      <main className={styles.workspace}>
        <header className={styles.workspaceHeader}>
          <div>
            <p className={styles.eyebrow}>运行总览</p>
            <h2>{selectedRun ? selectedRun.caseName ?? selectedRun.caseId : "等待启动或选择一条运行记录"}</h2>
            <p className={styles.headerCopy}>
              当前工作区聚焦运行态、阶段结果和失败样本，不展示营销式摘要。
            </p>
          </div>

          <div className={styles.headerAside}>
            <div className={`${styles.runtimeBadge} ${errorText ? styles.runtimeWarning : styles.runtimeOk}`}>
              <span>工作台状态</span>
              <strong>{workspaceStatus}</strong>
            </div>
            <div className={styles.runtimeMeta}>
              <div>
                <span>当前任务</span>
                <strong>{selectedRun?.runId ?? "未选择"}</strong>
              </div>
              <div>
                <span>最后轮询</span>
                <strong>{formatTimestamp(lastPolledAt)}</strong>
              </div>
            </div>
          </div>
        </header>

        {errorText ? <div className={styles.alert}>{errorText}</div> : null}

        <section className={styles.panel}>
          <div className={styles.sectionHeading}>
            <div>
              <p className={styles.eyebrow}>实时态</p>
              <h2>运行进度</h2>
            </div>
            <div className={styles.headingActions}>
              {selectedRun ? <StatusPill status={selectedRun.status} /> : null}
              <button className={styles.inlineButton} disabled={!selectedRun} onClick={() => selectedRun && void copySummary()} type="button">
                {copied ? "已复制" : "复制摘要"}
              </button>
            </div>
          </div>

          <div className={styles.progressStrip}>
            <div>
              <span>当前阶段</span>
              <strong>{selectedRun?.progress.currentStageName ?? currentStage?.name ?? "等待运行"}</strong>
            </div>
            <div>
              <span>完成进度</span>
              <strong>
                {selectedRun
                  ? `${selectedRun.progress.completedStages}/${selectedRun.progress.totalStages}`
                  : "--"}
              </strong>
            </div>
            <div>
              <span>已运行</span>
              <strong>
                {typeof selectedRun?.progress.elapsedSec === "number"
                  ? `${formatDecimal(selectedRun.progress.elapsedSec)} s`
                  : "--"}
              </strong>
            </div>
            <div>
              <span>目标</span>
              <strong>{selectedRun?.targetBaseUrl ?? "router-api-test.svc"}</strong>
            </div>
          </div>

          <div className={styles.metricGrid}>
            <MetricCell label="RPS" value={formatRps(currentMetrics.rps)} hint="当前阶段 / 累计" />
            <MetricCell label="成功率" value={formatPercent(currentMetrics.successRate)} hint="用于快速识别抖动" tone="good" />
            <MetricCell label="P50" value={formatLatency(currentMetrics.p50Ms)} hint="中位延迟" />
            <MetricCell label="P95" value={formatLatency(currentMetrics.p95Ms)} hint="主观体验边界" />
            <MetricCell label="P99" value={formatLatency(currentMetrics.p99Ms)} hint="尾延迟" tone="danger" />
            <MetricCell
              label="失败数"
              value={formatDecimal(currentMetrics.failureCount)}
              hint={`超时 ${formatDecimal(currentMetrics.timeoutCount)}`}
              tone={typeof currentMetrics.failureCount === "number" && currentMetrics.failureCount > 0 ? "danger" : "default"}
            />
          </div>

          {selectedRun && compareRun ? (
            <div className={styles.compareStrip}>
              <div>
                <span>对比基线</span>
                <strong>{compareRun.runId}</strong>
              </div>
              <div className={`${styles.deltaBlock} ${styles[`delta${capitalize(getDeltaTone(summaryMetrics.rps, baselineMetrics?.rps, "higher"))}`]}`}>
                <span>RPS 差异</span>
                <strong>{formatDelta(summaryMetrics.rps, baselineMetrics?.rps, "rps")}</strong>
              </div>
              <div className={`${styles.deltaBlock} ${styles[`delta${capitalize(getDeltaTone(summaryMetrics.successRate, baselineMetrics?.successRate, "higher"))}`]}`}>
                <span>成功率</span>
                <strong>{formatDelta(summaryMetrics.successRate, baselineMetrics?.successRate, "percent")}</strong>
              </div>
              <div className={`${styles.deltaBlock} ${styles[`delta${capitalize(getDeltaTone(summaryMetrics.p95Ms, baselineMetrics?.p95Ms, "lower"))}`]}`}>
                <span>P95 差异</span>
                <strong>{formatDelta(summaryMetrics.p95Ms, baselineMetrics?.p95Ms, "latency")}</strong>
              </div>
              <div className={`${styles.deltaBlock} ${styles[`delta${capitalize(getDeltaTone(summaryMetrics.p99Ms, baselineMetrics?.p99Ms, "lower"))}`]}`}>
                <span>P99 差异</span>
                <strong>{formatDelta(summaryMetrics.p99Ms, baselineMetrics?.p99Ms, "latency")}</strong>
              </div>
            </div>
          ) : null}
        </section>

        <section className={styles.panel}>
          <div className={styles.sectionHeading}>
            <div>
              <p className={styles.eyebrow}>阶段结果</p>
              <h2>阶梯结果表</h2>
            </div>
            <small>{detailState === "loading" ? "正在刷新详情" : `${stageRows.length} 个阶段`}</small>
          </div>

          <div className={styles.stageRail}>
            {stageRows.map((stage) => (
              <div
                key={stage.key}
                className={`${styles.stageChip} ${selectedRun?.progress.currentStageIndex === stage.stepIndex ? styles.stageChipActive : ""}`}
              >
                <span>{stage.stepIndex + 1}</span>
                <strong>{stage.name}</strong>
                <small>{formatStatusLabel(stage.status)}</small>
              </div>
            ))}
          </div>

          <div className={styles.tableWrap}>
            <table className={styles.stageTable}>
              <thead>
                <tr>
                  <th>阶段</th>
                  <th>状态</th>
                  <th>并发</th>
                  <th>持续</th>
                  <th>RPS</th>
                  <th>成功率</th>
                  <th>P50</th>
                  <th>P95</th>
                  <th>P99</th>
                  <th>失败</th>
                </tr>
              </thead>
              <tbody>
                {stageRows.map((stage) => (
                  <tr key={stage.key}>
                    <td>
                      <div className={styles.tableCellMain}>
                        <strong>{stage.name}</strong>
                        <small>
                          {stage.startedAt ? formatTimestamp(stage.startedAt) : "待开始"}
                          {stage.finishedAt ? ` -> ${formatTimestamp(stage.finishedAt)}` : ""}
                        </small>
                      </div>
                    </td>
                    <td>
                      <StatusPill status={stage.status} />
                    </td>
                    <td>{formatDecimal(stage.concurrency)}</td>
                    <td>
                      {formatDecimal(stage.durationSec)}s
                      {typeof stage.warmupSec === "number" ? ` / warmup ${formatDecimal(stage.warmupSec)}s` : ""}
                    </td>
                    <td>{formatRps(stage.metrics.rps)}</td>
                    <td>{formatPercent(stage.metrics.successRate)}</td>
                    <td>{formatLatency(stage.metrics.p50Ms)}</td>
                    <td>{formatLatency(stage.metrics.p95Ms)}</td>
                    <td>{formatLatency(stage.metrics.p99Ms)}</td>
                    <td>{formatDecimal(stage.failureCount)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <div className={styles.lowerGrid}>
          <section className={styles.panel}>
            <div className={styles.sectionHeading}>
              <div>
                <p className={styles.eyebrow}>异常样本</p>
                <h2>最近失败请求</h2>
              </div>
              <small>{failureSamples.length} 条样本</small>
            </div>

            {failureSamples.length === 0 ? (
              <div className={styles.emptyState}>
                <strong>当前没有失败样本。</strong>
                <p>如果后端开始返回 `error_samples`，这里会优先展示最近错误。</p>
              </div>
            ) : (
              <div className={styles.sampleList}>
                {failureSamples.map((sample) => (
                  <article key={sample.sampleId} className={styles.sampleRow}>
                    <div className={styles.sampleHead}>
                      <strong>{sample.errorType ?? "请求失败"}</strong>
                      <small>{formatTimestamp(sample.occurredAt)}</small>
                    </div>
                    <div className={styles.sampleMeta}>
                      <span>阶段 {typeof sample.stepIndex === "number" ? sample.stepIndex + 1 : "--"}</span>
                      <span>状态码 {sample.statusCode ?? "--"}</span>
                      <span>耗时 {formatLatency(sample.latencyMs)}</span>
                    </div>
                    <p>{sample.message}</p>
                    <code>{sample.requestSummary ?? "后端未返回请求摘要"}</code>
                  </article>
                ))}
              </div>
            )}
          </section>

          <div className={styles.breakdownGrid}>
            <BreakdownList
              title="状态码分布"
              values={selectedRun?.aggregateMetrics?.statusCodeBreakdown}
              emptyLabel="暂无状态码分布"
            />
            <BreakdownList
              title="错误类型分布"
              values={selectedRun?.aggregateMetrics?.errorTypeBreakdown}
              emptyLabel="暂无错误类型分布"
            />
          </div>
        </div>
      </main>

      <aside className={styles.rightRail}>
        <section className={styles.panel}>
          <div className={styles.sectionHeading}>
            <div>
              <p className={styles.eyebrow}>历史记录</p>
              <h2>最近运行</h2>
            </div>
            <button className={styles.inlineButton} onClick={() => void loadRuns()} type="button">
              刷新列表
            </button>
          </div>

          {runsState === "loading" && orderedRuns.length === 0 ? (
            <div className={styles.emptyState}>
              <strong>正在加载压测记录。</strong>
              <p>列表用于查看历史结果和选择对比基线。</p>
            </div>
          ) : orderedRuns.length === 0 ? (
            <div className={styles.emptyState}>
              <strong>还没有压测记录。</strong>
              <p>启动一轮任务后，这里会展示历史记录。</p>
            </div>
          ) : (
            <div className={styles.historyList}>
              {orderedRuns.map((run) => (
                <article
                  key={run.runId}
                  className={`${styles.historyRow} ${selectedRunId === run.runId ? styles.historyRowSelected : ""}`}
                >
                  <button className={styles.historyMain} onClick={() => setSelectedRunId(run.runId)} type="button">
                    <div className={styles.historyHead}>
                      <strong>{run.caseName ?? run.caseId}</strong>
                      <StatusPill status={run.status} />
                    </div>
                    <small>{run.runId}</small>
                    <div className={styles.historyMetrics}>
                      <span>RPS {formatRps(run.aggregateMetrics?.rps)}</span>
                      <span>P99 {formatLatency(run.aggregateMetrics?.p99Ms)}</span>
                    </div>
                    <small>{formatTimestamp(run.updatedAt ?? run.startedAt)}</small>
                  </button>
                  <div className={styles.historyActions}>
                    <button
                      className={styles.inlineButton}
                      disabled={selectedRunId === run.runId}
                      onClick={() => setSelectedRunId(run.runId)}
                      type="button"
                    >
                      查看
                    </button>
                    <button
                      className={styles.inlineButton}
                      disabled={selectedRunId === run.runId}
                      onClick={() => setCompareRunId(run.runId)}
                      type="button"
                    >
                      对比
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>

        <section className={styles.panel}>
          <div className={styles.sectionHeading}>
            <div>
              <p className={styles.eyebrow}>基线</p>
              <h2>结果对比</h2>
            </div>
            {compareRun ? (
              <button
                className={styles.inlineButton}
                onClick={() => {
                  setCompareRunId(null);
                  setCompareRun(null);
                }}
                type="button"
              >
                清除
              </button>
            ) : null}
          </div>

          {compareState === "loading" ? (
            <div className={styles.emptyState}>
              <strong>正在加载基线详情。</strong>
              <p>完成后会显示关键指标对比。</p>
            </div>
          ) : compareRun ? (
            <div className={styles.comparePanel}>
              <strong>{compareRun.caseName ?? compareRun.caseId}</strong>
              <small>{compareRun.runId}</small>
              <div className={styles.compareMeta}>
                <span>状态 {formatStatusLabel(compareRun.status)}</span>
                <span>开始于 {formatTimestamp(compareRun.startedAt)}</span>
              </div>
              <div className={styles.compareMetricList}>
                <div>
                  <span>RPS</span>
                  <strong>{formatRps(compareRun.aggregateMetrics?.rps)}</strong>
                </div>
                <div>
                  <span>成功率</span>
                  <strong>{formatPercent(compareRun.aggregateMetrics?.successRate)}</strong>
                </div>
                <div>
                  <span>P95</span>
                  <strong>{formatLatency(compareRun.aggregateMetrics?.p95Ms)}</strong>
                </div>
                <div>
                  <span>P99</span>
                  <strong>{formatLatency(compareRun.aggregateMetrics?.p99Ms)}</strong>
                </div>
              </div>
            </div>
          ) : (
            <div className={styles.emptyState}>
              <strong>尚未选择基线运行。</strong>
              <p>从右侧历史记录里点“对比”，即可在当前结果旁显示差异。</p>
            </div>
          )}
        </section>
      </aside>
    </div>
  );
}
