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
const DEFAULT_TARGET_LABEL = "router-api-test.intent.svc.cluster.local:8000";

type AsyncState = "idle" | "loading" | "ready" | "error";
type WorkspaceMode = "configure" | "monitor";
type PlanStrategy = "ladder" | "fatigue";
type PlanSource = "case_default" | "ladder_template" | "fatigue_template" | "manual";

type LadderTemplateDraft = {
  startConcurrency: number;
  stepConcurrency: number;
  maxConcurrency: number;
  stageDurationSec: number;
  warmupSec: number;
  timeoutMs: number;
};

type FatigueTemplateDraft = {
  startConcurrency: number;
  rampStepConcurrency: number;
  targetConcurrency: number;
  rampStageDurationSec: number;
  steadyDurationSec: number;
  steadyWarmupSec: number;
  timeoutMs: number;
};

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

function normalizePositiveNumber(value: number | null | undefined, fallback: number) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return fallback;
  }
  return value > 0 ? Math.round(value) : fallback;
}

function normalizeNonNegativeNumber(value: number | null | undefined, fallback = 0) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return fallback;
  }
  return value >= 0 ? Math.round(value) : fallback;
}

function normalizeOptionalPositiveNumber(value: number | null | undefined) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return undefined;
  }
  const rounded = Math.round(value);
  return rounded > 0 ? rounded : undefined;
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
  return `${formatDecimal(value, value >= 100 ? 0 : 1)} 毫秒`;
}

function formatRps(value?: number | null) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "--";
  }
  return `${formatDecimal(value, value >= 100 ? 0 : 1)} 次/秒`;
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
    `任务编号: ${run.runId}`,
    `用例编号: ${run.caseId}`,
    `状态: ${formatStatusLabel(run.status)}`,
    `发起时间: ${run.createdAt ?? "未知"}`,
    `进入执行: ${run.startedAt ?? "未知"}`,
    `结束时间: ${run.finishedAt ?? "未知"}`,
    `吞吐: ${formatRps(run.aggregateMetrics?.rps)}`,
    `成功率: ${formatPercent(run.aggregateMetrics?.successRate)}`,
    `95 分位延迟: ${formatLatency(run.aggregateMetrics?.p95Ms)}`,
    `99 分位延迟: ${formatLatency(run.aggregateMetrics?.p99Ms)}`,
    `失败数: ${formatDecimal(run.aggregateMetrics?.failureCount)}`
  ];

  if (compareRun) {
    lines.push(
      `基线任务: ${compareRun.runId}`,
      `基线吞吐: ${formatRps(compareRun.aggregateMetrics?.rps)}`,
      `基线 95 分位延迟: ${formatLatency(compareRun.aggregateMetrics?.p95Ms)}`,
      `基线 99 分位延迟: ${formatLatency(compareRun.aggregateMetrics?.p99Ms)}`
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
    return `${prefix}${normalized.toFixed(1)} 个百分点`;
  }

  if (kind === "rps") {
    return `${prefix}${formatDecimal(delta, Math.abs(delta) >= 100 ? 0 : 1)} 次/秒`;
  }

  return `${prefix}${formatDecimal(delta, Math.abs(delta) >= 100 ? 0 : 1)} 毫秒`;
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

function localizeCaseName(caseId?: string | null, caseName?: string | null) {
  switch (caseId) {
    case "transfer-intent-slot-analysis":
      return "转账意图与提槽压测";
    default:
      return caseName?.trim() || caseId || "未命名用例";
  }
}

function localizeCaseDescription(caseId?: string | null, description?: string | null) {
  switch (caseId) {
    case "transfer-intent-slot-analysis":
      return "只发送一条转账输入，在 router_only 边界直接返回，不触发 agent 执行，适合做阶梯压测基线。";
    default:
      return description?.trim() || "从后端加载结构化压测用例，优先确认用例和阶梯参数。";
  }
}

function localizeCaseCategory(category?: string | null) {
  switch (category) {
    case "router_only":
      return "路由边界返回";
    default:
      return category?.trim() || "默认场景";
  }
}

function localizeStepName(name: string | undefined | null, index: number) {
  const trimmed = name?.trim();
  if (!trimmed) {
    return `阶段 ${index + 1}`;
  }
  const generatedMatch = trimmed.match(/^(\d+)\s+concurrency\s*\/\s*(\d+(?:\.\d+)?)s$/i);
  if (generatedMatch) {
    return `并发 ${generatedMatch[1]} / ${generatedMatch[2]} 秒`;
  }
  if (trimmed.toLowerCase() === "smoke") {
    return "快速验证";
  }
  return trimmed;
}

function normalizeStepDraft(step: PerfTestStepInput, index: number): PerfTestStepInput {
  const fallback = createStepDraft(index);
  const durationSec = normalizePositiveNumber(step.durationSec, fallback.durationSec);
  const warmupSec = Math.min(normalizeNonNegativeNumber(step.warmupSec, fallback.warmupSec ?? 0), durationSec);

  return {
    ...step,
    name: localizeStepName(step.name ?? fallback.name, index),
    concurrency: normalizePositiveNumber(step.concurrency, fallback.concurrency),
    durationSec,
    warmupSec,
    requestLimit: normalizeOptionalPositiveNumber(step.requestLimit),
    cooldownSec: normalizeNonNegativeNumber(step.cooldownSec, 0),
    timeoutMs: normalizePositiveNumber(step.timeoutMs, fallback.timeoutMs ?? 10000)
  };
}

const INITIAL_PLAN_STEPS = [createStepDraft(0), createStepDraft(1)].map((step, index) => normalizeStepDraft(step, index));

function createDefaultLadderTemplate(steps: PerfTestStepInput[] = INITIAL_PLAN_STEPS): LadderTemplateDraft {
  const normalized = (steps.length > 0 ? steps : INITIAL_PLAN_STEPS).map((step, index) => normalizeStepDraft(step, index));
  const first = normalized[0];
  const maxConcurrency = normalized.reduce((max, step) => Math.max(max, step.concurrency), first.concurrency);
  const firstGap =
    normalized.length > 1 ? normalized[1].concurrency - normalized[0].concurrency : Math.max(10, Math.round(first.concurrency / 2));

  return {
    startConcurrency: first.concurrency,
    stepConcurrency: firstGap > 0 ? firstGap : Math.max(10, Math.round(first.concurrency / 2)),
    maxConcurrency,
    stageDurationSec: first.durationSec,
    warmupSec: first.warmupSec ?? 0,
    timeoutMs: first.timeoutMs ?? 10000
  };
}

function createDefaultFatigueTemplate(steps: PerfTestStepInput[] = INITIAL_PLAN_STEPS): FatigueTemplateDraft {
  const normalized = (steps.length > 0 ? steps : INITIAL_PLAN_STEPS).map((step, index) => normalizeStepDraft(step, index));
  const first = normalized[0];
  const last = normalized[normalized.length - 1] ?? first;
  const targetConcurrency = normalized.reduce((max, step) => Math.max(max, step.concurrency), first.concurrency);
  const firstGap =
    normalized.length > 1 ? normalized[1].concurrency - normalized[0].concurrency : Math.max(10, Math.round(targetConcurrency / 4));
  const steadyWarmupSec = last.warmupSec ?? 0;

  return {
    startConcurrency: first.concurrency,
    rampStepConcurrency: firstGap > 0 ? firstGap : Math.max(10, Math.round(targetConcurrency / 4)),
    targetConcurrency,
    rampStageDurationSec: normalized.length > 1 ? first.durationSec : 60,
    steadyDurationSec: Math.max(last.durationSec - steadyWarmupSec, 300),
    steadyWarmupSec,
    timeoutMs: last.timeoutMs ?? first.timeoutMs ?? 10000
  };
}

function calculatePlanStats(steps: PerfTestStepInput[]) {
  return steps.reduce(
    (summary, rawStep, index) => {
      const step = normalizeStepDraft(rawStep, index);
      return {
        stageCount: summary.stageCount + 1,
        peakConcurrency: Math.max(summary.peakConcurrency, step.concurrency),
        scheduledDurationSec: summary.scheduledDurationSec + step.durationSec + (step.cooldownSec ?? 0),
        measuredDurationSec: summary.measuredDurationSec + Math.max(step.durationSec - (step.warmupSec ?? 0), 0)
      };
    },
    {
      stageCount: 0,
      peakConcurrency: 0,
      scheduledDurationSec: 0,
      measuredDurationSec: 0
    }
  );
}

function calculateLadderStageCount(draft: LadderTemplateDraft) {
  if (draft.stepConcurrency <= 0 || draft.maxConcurrency < draft.startConcurrency) {
    return 0;
  }
  return Math.floor((draft.maxConcurrency - draft.startConcurrency) / draft.stepConcurrency) + 1;
}

function calculateFatigueStageCount(draft: FatigueTemplateDraft) {
  if (draft.rampStepConcurrency <= 0 || draft.targetConcurrency < draft.startConcurrency) {
    return 0;
  }
  if (draft.targetConcurrency === draft.startConcurrency) {
    return 1;
  }
  return Math.ceil((draft.targetConcurrency - draft.startConcurrency) / draft.rampStepConcurrency) + 1;
}

function validateLadderTemplate(draft: LadderTemplateDraft) {
  const issues: string[] = [];
  if (draft.startConcurrency <= 0) issues.push("起始并发必须大于 0");
  if (draft.stepConcurrency <= 0) issues.push("阶梯步长必须大于 0");
  if (draft.maxConcurrency < draft.startConcurrency) issues.push("结束并发不能小于起始并发");
  if (draft.stageDurationSec <= 0) issues.push("每阶段总时长必须大于 0");
  if (draft.warmupSec < 0) issues.push("每阶段预热时长不能为负数");
  if (draft.warmupSec > draft.stageDurationSec) issues.push("每阶段预热时长不能超过总时长");
  if (draft.timeoutMs <= 0) issues.push("请求超时必须大于 0");
  if (calculateLadderStageCount(draft) > 40) issues.push("生成阶段过多，请提高步长或降低结束并发");
  return issues;
}

function validateFatigueTemplate(draft: FatigueTemplateDraft) {
  const issues: string[] = [];
  if (draft.startConcurrency <= 0) issues.push("起始并发必须大于 0");
  if (draft.rampStepConcurrency <= 0) issues.push("爬升步长必须大于 0");
  if (draft.targetConcurrency < draft.startConcurrency) issues.push("目标并发不能小于起始并发");
  if (draft.rampStageDurationSec <= 0) issues.push("爬升阶段时长必须大于 0");
  if (draft.steadyDurationSec <= 0) issues.push("稳态持压时长必须大于 0");
  if (draft.steadyWarmupSec < 0) issues.push("稳态预热时长不能为负数");
  if (draft.timeoutMs <= 0) issues.push("请求超时必须大于 0");
  if (calculateFatigueStageCount(draft) > 40) issues.push("生成阶段过多，请提高爬升步长或降低目标并发");
  return issues;
}

function buildLadderPlan(draft: LadderTemplateDraft) {
  const issues = validateLadderTemplate(draft);
  if (issues.length > 0) {
    return [];
  }

  const stageCount = calculateLadderStageCount(draft);
  return Array.from({ length: stageCount }, (_, index) =>
    normalizeStepDraft(
      {
        name: `并发 ${draft.startConcurrency + draft.stepConcurrency * index}`,
        concurrency: draft.startConcurrency + draft.stepConcurrency * index,
        durationSec: draft.stageDurationSec,
        warmupSec: draft.warmupSec,
        timeoutMs: draft.timeoutMs
      },
      index
    )
  );
}

function buildFatiguePlan(draft: FatigueTemplateDraft) {
  const issues = validateFatigueTemplate(draft);
  if (issues.length > 0) {
    return [];
  }

  const steps: PerfTestStepInput[] = [];
  let currentConcurrency = draft.startConcurrency;

  while (currentConcurrency < draft.targetConcurrency) {
    steps.push(
      normalizeStepDraft(
        {
          name: `爬升 / 并发 ${currentConcurrency}`,
          concurrency: currentConcurrency,
          durationSec: draft.rampStageDurationSec,
          warmupSec: 0,
          timeoutMs: draft.timeoutMs
        },
        steps.length
      )
    );
    currentConcurrency = Math.min(currentConcurrency + draft.rampStepConcurrency, draft.targetConcurrency);
  }

  steps.push(
    normalizeStepDraft(
      {
        name: `稳态持压 / 并发 ${draft.targetConcurrency}`,
        concurrency: draft.targetConcurrency,
        durationSec: draft.steadyDurationSec + draft.steadyWarmupSec,
        warmupSec: draft.steadyWarmupSec,
        timeoutMs: draft.timeoutMs
      },
      steps.length
    )
  );

  return steps;
}

function formatPlanSourceLabel(source: PlanSource) {
  switch (source) {
    case "case_default":
      return "用例默认";
    case "ladder_template":
      return "阶梯策略生成";
    case "fatigue_template":
      return "疲劳策略生成";
    case "manual":
      return "手动微调";
    default:
      return source;
  }
}

function formatJsonText(value: unknown) {
  return JSON.stringify(value ?? {}, null, 2);
}

function parseJsonObjectText(label: string, value: string): {
  value?: Record<string, unknown>;
  error?: string;
} {
  try {
    const parsed = JSON.parse(value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {
        error: `${label}必须是 JSON 对象`
      };
    }
    return {
      value: parsed as Record<string, unknown>
    };
  } catch (error) {
    return {
      error: `${label}不是合法 JSON: ${error instanceof Error ? error.message : "解析失败"}`
    };
  }
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
  const [workspaceMode, setWorkspaceMode] = useState<WorkspaceMode>("configure");
  const [selectedCaseId, setSelectedCaseId] = useState("");
  const [planStrategy, setPlanStrategy] = useState<PlanStrategy>("ladder");
  const [planSource, setPlanSource] = useState<PlanSource>("case_default");
  const [templateDirty, setTemplateDirty] = useState(false);
  const [ladderTemplate, setLadderTemplate] = useState<LadderTemplateDraft>(() => createDefaultLadderTemplate(INITIAL_PLAN_STEPS));
  const [fatigueTemplate, setFatigueTemplate] = useState<FatigueTemplateDraft>(() => createDefaultFatigueTemplate(INITIAL_PLAN_STEPS));
  const [ladderSteps, setLadderSteps] = useState<PerfTestStepInput[]>(INITIAL_PLAN_STEPS);
  const [caseEditorOpen, setCaseEditorOpen] = useState(false);
  const [caseEditorDirty, setCaseEditorDirty] = useState(false);
  const [sessionRequestText, setSessionRequestText] = useState(formatJsonText({}));
  const [messageRequestText, setMessageRequestText] = useState(formatJsonText({}));
  const [expectationsText, setExpectationsText] = useState(formatJsonText({}));
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
  const [isCancelling, setIsCancelling] = useState(false);
  const [copied, setCopied] = useState(false);
  const [lastPolledAt, setLastPolledAt] = useState<string | null>(null);

  const selectedCase = cases.find((item) => item.caseId === selectedCaseId) ?? null;
  const orderedRuns = [...runs].sort((left, right) => (right.updatedAt ?? "").localeCompare(left.updatedAt ?? ""));
  const currentStage = pickCurrentStage(selectedRun);
  const currentMetrics = currentStage?.metrics ?? selectedRun?.aggregateMetrics ?? {};
  const failureSamples = collectFailureSamples(selectedRun);
  const templateIssues = planStrategy === "ladder" ? validateLadderTemplate(ladderTemplate) : validateFatigueTemplate(fatigueTemplate);
  const strategyPreviewSteps = planStrategy === "ladder" ? buildLadderPlan(ladderTemplate) : buildFatiguePlan(fatigueTemplate);
  const currentPlanStats = calculatePlanStats(ladderSteps);
  const strategyPreviewStats = calculatePlanStats(strategyPreviewSteps);
  const planSourceLabel = formatPlanSourceLabel(planSource);

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
      name: localizeStepName(stageResult?.name ?? plannedStep?.name, index),
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

  function applyCaseDefaults(nextCase?: PerfTestCase | null) {
    const nextSteps =
      nextCase?.defaultSteps.length
        ? nextCase.defaultSteps.map((step, index) => normalizeStepDraft(step, index))
        : INITIAL_PLAN_STEPS;

    setLadderSteps(nextSteps);
    setLadderTemplate(createDefaultLadderTemplate(nextSteps));
    setFatigueTemplate(createDefaultFatigueTemplate(nextSteps));
    setPlanSource("case_default");
    setTemplateDirty(false);
    setCaseEditorDirty(false);
    setCaseEditorOpen(false);
    setSessionRequestText(formatJsonText(nextCase?.sessionRequest ?? {}));
    setMessageRequestText(formatJsonText(nextCase?.messageRequest ?? {}));
    setExpectationsText(formatJsonText(nextCase?.expectations ?? {}));
  }

  const sessionRequestDraft = parseJsonObjectText("会话请求", sessionRequestText);
  const messageRequestDraft = parseJsonObjectText("消息请求", messageRequestText);
  const expectationsDraft = parseJsonObjectText("校验规则", expectationsText);

  const validationIssues = [
    selectedCaseId ? null : "请选择测试用例",
    ladderSteps.length > 0 ? null : "至少需要一个执行阶段",
    sessionRequestDraft.error ?? null,
    messageRequestDraft.error ?? null,
    expectationsDraft.error ?? null,
    ...ladderSteps.flatMap((step, index) => {
      const issues: string[] = [];
      if (!step.name?.trim()) issues.push(`阶段 ${index + 1} 缺少名称`);
      if (step.concurrency <= 0) issues.push(`阶段 ${index + 1} 并发数必须大于 0`);
      if (step.durationSec <= 0) issues.push(`阶段 ${index + 1} 总时长必须大于 0`);
      if (typeof step.warmupSec === "number" && step.warmupSec < 0) issues.push(`阶段 ${index + 1} 预热时长不能为负数`);
      if (typeof step.warmupSec === "number" && step.warmupSec > step.durationSec) issues.push(`阶段 ${index + 1} 预热时长不能超过总时长`);
      if (typeof step.timeoutMs === "number" && step.timeoutMs <= 0) issues.push(`阶段 ${index + 1} 超时阈值必须大于 0`);
      if (typeof step.requestLimit === "number" && step.requestLimit <= 0) issues.push(`阶段 ${index + 1} 请求上限必须大于 0`);
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
        applyCaseDefaults(items[0]);
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
              : detail.status === "cancelled"
                ? `压测任务 ${detail.runId} 已结束`
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
          name: localizeStepName(step.name, index)
        })),
        caseOverride: {
          sessionRequest: sessionRequestDraft.value,
          messageRequest: messageRequestDraft.value,
          expectations: expectationsDraft.value
        }
      });

      setWorkspaceMode("monitor");
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

  async function handleCancelRun() {
    if (!selectedRunId) return;

    try {
      setIsCancelling(true);
      setErrorText(null);
      setWorkspaceStatus("正在结束压测任务");
      const detail = await api.cancelPerfTestRun(selectedRunId);
      startTransition(() => {
        setSelectedRun(detail);
        setSelectedRunId(detail.runId);
      });
      await loadRuns({ silent: true });
      setWorkspaceStatus(`压测任务 ${detail.runId} 已结束`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "结束压测任务失败";
      setErrorText(message);
      setWorkspaceStatus("结束压测任务失败");
    } finally {
      setIsCancelling(false);
    }
  }

  function updateStep(index: number, patch: Partial<PerfTestStepInput>) {
    setLadderSteps((current) =>
      current.map((step, stepIndex) => (stepIndex === index ? { ...step, ...patch } : step))
    );
    setPlanSource("manual");
    setWorkspaceStatus(`已更新阶段 ${index + 1}`);
  }

  function duplicateStep(index: number) {
    setLadderSteps((current) => {
      const source = current[index];
      if (!source) return current;

      const copy = [...current];
      copy.splice(index + 1, 0, {
        ...source,
        name: `${localizeStepName(source.name, index)} 副本`
      });
      return copy;
    });
    setPlanSource("manual");
    setWorkspaceStatus(`已复制阶段 ${index + 1}`);
  }

  function insertStep(index: number) {
    setLadderSteps((current) => {
      const copy = [...current];
      copy.splice(index + 1, 0, createStepDraft(index + 1));
      return copy;
    });
    setPlanSource("manual");
    setWorkspaceStatus(`已在阶段 ${index + 1} 后插入新阶段`);
  }

  function removeStep(index: number) {
    setLadderSteps((current) => current.filter((_, stepIndex) => stepIndex !== index));
    setPlanSource("manual");
    setWorkspaceStatus(`已删除阶段 ${index + 1}`);
  }

  function handleGeneratePlan() {
    if (templateIssues.length > 0) {
      setWorkspaceStatus("请先修正测试策略参数");
      return;
    }

    if (strategyPreviewSteps.length === 0) {
      setWorkspaceStatus("当前策略没有生成可执行阶段");
      return;
    }

    setLadderSteps(strategyPreviewSteps);
    setPlanSource(planStrategy === "ladder" ? "ladder_template" : "fatigue_template");
    setTemplateDirty(false);
    setWorkspaceStatus(
      planStrategy === "ladder"
        ? `已生成 ${strategyPreviewSteps.length} 个阶梯阶段`
        : `已生成 ${strategyPreviewSteps.length} 个疲劳测试阶段`
    );
  }

  function restoreCasePlan() {
    applyCaseDefaults(selectedCase);
    setWorkspaceStatus("已恢复用例默认计划");
  }

  async function copySummary() {
    if (!selectedRun) return;
    await navigator.clipboard.writeText(buildSummaryText(selectedRun, compareRun));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  }

  const baselineMetrics = compareRun?.aggregateMetrics;
  const summaryMetrics = selectedRun?.aggregateMetrics ?? {};
  const selectedCaseNotes = selectedCase?.notes ?? [];
  const selectedCaseCategory = localizeCaseCategory(selectedCase?.category);
  const selectedCaseDescription = localizeCaseDescription(selectedCase?.caseId, selectedCase?.description);
  const configuredCaseLabel = localizeCaseName(selectedCase?.caseId, selectedCase?.name);
  const activeRunCaseLabel = localizeCaseName(
    selectedRun?.caseId ?? selectedCase?.caseId,
    selectedRun?.caseName ?? selectedCase?.name
  );
  const activeStrategyLabel = planStrategy === "ladder" ? "阶梯测试" : "疲劳测试";
  const strategyPreviewLabel = planStrategy === "ladder" ? "固定步长逐级升载" : "分段爬升后稳态持压";
  const strategyNotes =
    planStrategy === "ladder"
      ? [
          "适合找吞吐拐点、成功率突降点和尾延迟恶化点。",
          "如果阶段很多，优先调大步长，而不是手工堆阶段卡片。"
        ]
      : [
          "疲劳测试按“多段爬升 + 一段稳态持压”生成，符合当前后端执行能力。",
          "稳态阶段的预热只用于过滤初始抖动，不会线性升并发。"
        ];
  const strategyStatusText = templateDirty
    ? "策略参数已修改，点击“生成计划”后会覆盖当前阶段表。"
    : planStrategy === "fatigue" && planSource !== "fatigue_template"
      ? "当前阶段表不是疲劳测试计划，点击“生成计划”切换。"
      : planStrategy === "ladder" && planSource === "fatigue_template"
        ? "当前阶段表来自疲劳测试计划，点击“生成计划”切回阶梯计划。"
        : planSource === "manual"
          ? "当前阶段表已手动微调，策略参数不会自动覆盖。"
          : `当前阶段表来源：${planSourceLabel}。`;

  return (
    <div className={styles.shell}>
      <header className={styles.pageHeader}>
        <div className={styles.pageTitleBlock}>
          <p className={styles.pageEyebrow}>管理后台 / 性能测试</p>
          <h1>性能测试工作台</h1>
          <p className={styles.pageIntro}>先配置任务，再启动压测，最后进入运行监控。配置区和结果区不再混在一起。</p>
        </div>

        <div className={styles.pageActions}>
          <div className={styles.modeSwitch} role="tablist" aria-label="工作模式">
            <button
              className={`${styles.modeButton} ${workspaceMode === "configure" ? styles.modeButtonActive : ""}`}
              onClick={() => setWorkspaceMode("configure")}
              type="button"
            >
              准备任务
            </button>
            <button
              className={`${styles.modeButton} ${workspaceMode === "monitor" ? styles.modeButtonActive : ""}`}
              onClick={() => setWorkspaceMode("monitor")}
              type="button"
            >
              运行监控
            </button>
          </div>

          <Link className={styles.backLink} href="/">
            返回意图控制台
          </Link>
        </div>
      </header>

      {errorText ? <div className={styles.alert}>{errorText}</div> : null}

      <div className={styles.contentGrid}>
        <main className={styles.mainStage}>
          {workspaceMode === "configure" ? (
            <section className={`${styles.panel} ${styles.composePanel}`}>
              <div className={styles.sectionHeading}>
                <div>
                  <p className={styles.eyebrow}>准备任务</p>
                  <h2>压测配置</h2>
                </div>
                <small>{casesState === "loading" ? "正在加载用例" : `${cases.length} 个可用用例`}</small>
              </div>

              <div className={styles.configStrip}>
                <div className={styles.configStat}>
                  <span>目标服务</span>
                  <strong>{DEFAULT_TARGET_LABEL}</strong>
                </div>
                <div className={styles.configStat}>
                  <span>当前用例</span>
                  <strong>{configuredCaseLabel}</strong>
                </div>
                <div className={styles.configStat}>
                  <span>配置策略</span>
                  <strong>{activeStrategyLabel}</strong>
                </div>
                <div className={styles.configStat}>
                  <span>阶段数量</span>
                  <strong>{currentPlanStats.stageCount} 个阶段</strong>
                </div>
                <div className={styles.configStat}>
                  <span>编排总时长</span>
                  <strong>{formatDecimal(currentPlanStats.scheduledDurationSec)} 秒</strong>
                </div>
                <div className={styles.configStat}>
                  <span>有效采样时长</span>
                  <strong>{formatDecimal(currentPlanStats.measuredDurationSec)} 秒</strong>
                </div>
                <div className={styles.configStat}>
                  <span>峰值并发</span>
                  <strong>{formatDecimal(currentPlanStats.peakConcurrency)}</strong>
                </div>
                <div className={styles.configStat}>
                  <span>阶段来源</span>
                  <strong>{planSourceLabel}</strong>
                </div>
              </div>

              <div className={styles.configBlock}>
                <label className={styles.field}>
                  <span>测试用例</span>
                  <select
                    value={selectedCaseId}
                    onChange={(event) => {
                      const nextCaseId = event.target.value;
                      setSelectedCaseId(nextCaseId);
                      const nextCase = cases.find((item) => item.caseId === nextCaseId);
                      applyCaseDefaults(nextCase);
                      setWorkspaceStatus(
                        nextCase
                          ? `已加载 ${localizeCaseName(nextCase.caseId, nextCase.name)} 默认计划`
                          : "请先选择测试用例"
                      );
                    }}
                  >
                    {cases.length === 0 ? <option value="">暂无测试用例</option> : null}
                    {cases.map((item) => (
                      <option key={item.caseId} value={item.caseId}>
                        {localizeCaseName(item.caseId, item.name)}
                      </option>
                    ))}
                  </select>
                </label>

                <div className={styles.caseBox}>
                  <div className={styles.caseHead}>
                    <div>
                      <strong>{configuredCaseLabel}</strong>
                      <div className={styles.inlineMeta}>
                        <span>场景 {selectedCaseCategory}</span>
                        <span>发起 管理端服务</span>
                        <span>接口 {selectedCase?.targetRoute ?? "/api/router/v2/sessions/{session_id}/messages"}</span>
                      </div>
                    </div>
                    <button className={styles.inlineButton} onClick={() => setCaseEditorOpen((current) => !current)} type="button">
                      {caseEditorOpen ? "收起用例内容" : "编辑测试用例内容"}
                    </button>
                  </div>
                  <p>{selectedCaseDescription}</p>
                  {selectedCaseNotes.length > 0 ? (
                    <ul className={styles.notesList}>
                      {selectedCaseNotes.map((note) => (
                        <li key={note}>{note}</li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              </div>

              {caseEditorOpen ? (
                <section className={styles.caseEditorSection}>
                  <div className={styles.sectionHeading}>
                    <div>
                      <p className={styles.eyebrow}>用例内容</p>
                      <h2>本次执行覆盖</h2>
                    </div>
                    <div className={styles.headingActions}>
                      <span className={styles.sourceBadge}>仅当前任务生效</span>
                      <button
                        className={styles.secondaryButton}
                        onClick={() => {
                          setSessionRequestText(formatJsonText(selectedCase?.sessionRequest ?? {}));
                          setMessageRequestText(formatJsonText(selectedCase?.messageRequest ?? {}));
                          setExpectationsText(formatJsonText(selectedCase?.expectations ?? {}));
                          setCaseEditorDirty(false);
                          setWorkspaceStatus("已恢复测试用例默认内容");
                        }}
                        type="button"
                      >
                        恢复默认内容
                      </button>
                    </div>
                  </div>

                  <p className={styles.strategyNote}>这里修改的是 admin 侧本次执行内容，不会改动后端默认 case 文件。</p>

                  <div className={styles.editorGrid}>
                    <label className={styles.field}>
                      <span>会话请求 JSON</span>
                      <textarea
                        className={styles.jsonEditor}
                        spellCheck={false}
                        value={sessionRequestText}
                        onChange={(event) => {
                          setSessionRequestText(event.target.value);
                          setCaseEditorDirty(true);
                        }}
                      />
                    </label>
                    <label className={styles.field}>
                      <span>消息请求 JSON</span>
                      <textarea
                        className={styles.jsonEditor}
                        spellCheck={false}
                        value={messageRequestText}
                        onChange={(event) => {
                          setMessageRequestText(event.target.value);
                          setCaseEditorDirty(true);
                        }}
                      />
                    </label>
                    <label className={styles.field}>
                      <span>校验规则 JSON</span>
                      <textarea
                        className={styles.jsonEditor}
                        spellCheck={false}
                        value={expectationsText}
                        onChange={(event) => {
                          setExpectationsText(event.target.value);
                          setCaseEditorDirty(true);
                        }}
                      />
                    </label>
                  </div>
                </section>
              ) : null}

              <section className={styles.strategySection}>
                <div className={styles.sectionHeading}>
                  <div>
                    <p className={styles.eyebrow}>测试策略</p>
                    <h2>生成计划</h2>
                  </div>
                  <small>{strategyPreviewLabel}</small>
                </div>

                <div className={styles.strategyTabs} role="tablist" aria-label="测试策略">
                  <button
                    className={`${styles.strategyTab} ${planStrategy === "ladder" ? styles.strategyTabActive : ""}`}
                    onClick={() => setPlanStrategy("ladder")}
                    type="button"
                  >
                    阶梯测试
                  </button>
                  <button
                    className={`${styles.strategyTab} ${planStrategy === "fatigue" ? styles.strategyTabActive : ""}`}
                    onClick={() => setPlanStrategy("fatigue")}
                    type="button"
                  >
                    疲劳测试
                  </button>
                </div>

                <div className={styles.strategyLayout}>
                  <div className={styles.strategyMain}>
                    {planStrategy === "ladder" ? (
                      <div className={styles.parameterGrid}>
                        <label className={styles.field}>
                          <span>起始并发</span>
                          <input
                            type="number"
                            min={1}
                            value={ladderTemplate.startConcurrency}
                            onChange={(event) => {
                              setLadderTemplate((current) => ({
                                ...current,
                                startConcurrency: Number(event.target.value)
                              }));
                              setTemplateDirty(true);
                            }}
                          />
                        </label>
                        <label className={styles.field}>
                          <span>阶梯步长</span>
                          <input
                            type="number"
                            min={1}
                            value={ladderTemplate.stepConcurrency}
                            onChange={(event) => {
                              setLadderTemplate((current) => ({
                                ...current,
                                stepConcurrency: Number(event.target.value)
                              }));
                              setTemplateDirty(true);
                            }}
                          />
                        </label>
                        <label className={styles.field}>
                          <span>结束并发</span>
                          <input
                            type="number"
                            min={1}
                            value={ladderTemplate.maxConcurrency}
                            onChange={(event) => {
                              setLadderTemplate((current) => ({
                                ...current,
                                maxConcurrency: Number(event.target.value)
                              }));
                              setTemplateDirty(true);
                            }}
                          />
                        </label>
                        <label className={styles.field}>
                          <span>每阶段总时长（秒）</span>
                          <input
                            type="number"
                            min={1}
                            value={ladderTemplate.stageDurationSec}
                            onChange={(event) => {
                              setLadderTemplate((current) => ({
                                ...current,
                                stageDurationSec: Number(event.target.value)
                              }));
                              setTemplateDirty(true);
                            }}
                          />
                        </label>
                        <label className={styles.field}>
                          <span>每阶段预热（秒）</span>
                          <input
                            type="number"
                            min={0}
                            value={ladderTemplate.warmupSec}
                            onChange={(event) => {
                              setLadderTemplate((current) => ({
                                ...current,
                                warmupSec: Number(event.target.value)
                              }));
                              setTemplateDirty(true);
                            }}
                          />
                        </label>
                        <label className={styles.field}>
                          <span>请求超时（毫秒）</span>
                          <input
                            type="number"
                            min={1}
                            value={ladderTemplate.timeoutMs}
                            onChange={(event) => {
                              setLadderTemplate((current) => ({
                                ...current,
                                timeoutMs: Number(event.target.value)
                              }));
                              setTemplateDirty(true);
                            }}
                          />
                        </label>
                      </div>
                    ) : (
                      <div className={styles.parameterGrid}>
                        <label className={styles.field}>
                          <span>起始并发</span>
                          <input
                            type="number"
                            min={1}
                            value={fatigueTemplate.startConcurrency}
                            onChange={(event) => {
                              setFatigueTemplate((current) => ({
                                ...current,
                                startConcurrency: Number(event.target.value)
                              }));
                              setTemplateDirty(true);
                            }}
                          />
                        </label>
                        <label className={styles.field}>
                          <span>爬升步长</span>
                          <input
                            type="number"
                            min={1}
                            value={fatigueTemplate.rampStepConcurrency}
                            onChange={(event) => {
                              setFatigueTemplate((current) => ({
                                ...current,
                                rampStepConcurrency: Number(event.target.value)
                              }));
                              setTemplateDirty(true);
                            }}
                          />
                        </label>
                        <label className={styles.field}>
                          <span>目标并发</span>
                          <input
                            type="number"
                            min={1}
                            value={fatigueTemplate.targetConcurrency}
                            onChange={(event) => {
                              setFatigueTemplate((current) => ({
                                ...current,
                                targetConcurrency: Number(event.target.value)
                              }));
                              setTemplateDirty(true);
                            }}
                          />
                        </label>
                        <label className={styles.field}>
                          <span>每个爬升阶段时长（秒）</span>
                          <input
                            type="number"
                            min={1}
                            value={fatigueTemplate.rampStageDurationSec}
                            onChange={(event) => {
                              setFatigueTemplate((current) => ({
                                ...current,
                                rampStageDurationSec: Number(event.target.value)
                              }));
                              setTemplateDirty(true);
                            }}
                          />
                        </label>
                        <label className={styles.field}>
                          <span>稳态持压时长（秒）</span>
                          <input
                            type="number"
                            min={1}
                            value={fatigueTemplate.steadyDurationSec}
                            onChange={(event) => {
                              setFatigueTemplate((current) => ({
                                ...current,
                                steadyDurationSec: Number(event.target.value)
                              }));
                              setTemplateDirty(true);
                            }}
                          />
                        </label>
                        <label className={styles.field}>
                          <span>稳态预热（秒）</span>
                          <input
                            type="number"
                            min={0}
                            value={fatigueTemplate.steadyWarmupSec}
                            onChange={(event) => {
                              setFatigueTemplate((current) => ({
                                ...current,
                                steadyWarmupSec: Number(event.target.value)
                              }));
                              setTemplateDirty(true);
                            }}
                          />
                        </label>
                        <label className={styles.field}>
                          <span>请求超时（毫秒）</span>
                          <input
                            type="number"
                            min={1}
                            value={fatigueTemplate.timeoutMs}
                            onChange={(event) => {
                              setFatigueTemplate((current) => ({
                                ...current,
                                timeoutMs: Number(event.target.value)
                              }));
                              setTemplateDirty(true);
                            }}
                          />
                        </label>
                      </div>
                    )}

                    <p className={styles.strategyNote}>{strategyStatusText}</p>

                    <div className={styles.actionBar}>
                      <div className={styles.actionMeta}>
                        <strong>{planStrategy === "ladder" ? "先用参数生成阶梯，再在表格里微调" : "先生成爬升 + 稳态计划，再按阶段表修正"}</strong>
                        <span>{planStrategy === "ladder" ? "适合找容量边界。" : "适合观察长时间持压下的稳定性。"}</span>
                      </div>

                      <div className={styles.actionButtons}>
                        <button className={styles.secondaryButton} onClick={restoreCasePlan} type="button">
                          恢复默认
                        </button>
                        <button className={styles.primaryButton} disabled={templateIssues.length > 0} onClick={handleGeneratePlan} type="button">
                          生成计划
                        </button>
                      </div>
                    </div>

                    {templateIssues.length > 0 ? (
                      <div className={styles.issueList}>
                        {templateIssues.map((issue) => (
                          <span key={issue}>{issue}</span>
                        ))}
                      </div>
                    ) : null}
                  </div>

                  <aside className={styles.strategyAside}>
                    <div className={styles.configStat}>
                      <span>生成后阶段数</span>
                      <strong>{strategyPreviewStats.stageCount} 个阶段</strong>
                    </div>
                    <div className={styles.configStat}>
                      <span>生成后峰值并发</span>
                      <strong>{formatDecimal(strategyPreviewStats.peakConcurrency)}</strong>
                    </div>
                    <div className={styles.configStat}>
                      <span>生成后总时长</span>
                      <strong>{formatDecimal(strategyPreviewStats.scheduledDurationSec)} 秒</strong>
                    </div>
                    <div className={styles.configStat}>
                      <span>生成后采样时长</span>
                      <strong>{formatDecimal(strategyPreviewStats.measuredDurationSec)} 秒</strong>
                    </div>

                    <div className={styles.caseBox}>
                      <div className={styles.caseHead}>
                        <strong>策略提示</strong>
                        <span className={styles.sourceBadge}>{activeStrategyLabel}</span>
                      </div>
                      <ul className={styles.notesList}>
                        {strategyNotes.map((note) => (
                          <li key={note}>{note}</li>
                        ))}
                      </ul>
                    </div>
                  </aside>
                </div>
              </section>

              <section className={styles.scheduleSection}>
                <div className={styles.sectionHeading}>
                  <div>
                    <p className={styles.eyebrow}>阶段表</p>
                    <h2>执行计划</h2>
                  </div>
                  <div className={styles.headingActions}>
                    <span className={styles.sourceBadge}>{planSourceLabel}</span>
                    <button
                      className={styles.secondaryButton}
                      onClick={() => {
                        setLadderSteps([...ladderSteps, createStepDraft(ladderSteps.length)]);
                        setPlanSource("manual");
                        setWorkspaceStatus("已新增空白阶段");
                      }}
                      type="button"
                    >
                      新增阶段
                    </button>
                  </div>
                </div>

                <div className={styles.tableWrap}>
                  <table className={`${styles.stageTable} ${styles.planTable}`}>
                    <thead>
                      <tr>
                        <th>序号</th>
                        <th>阶段名称</th>
                        <th>并发数</th>
                        <th>总时长</th>
                        <th>预热</th>
                        <th>采样时长</th>
                        <th>超时</th>
                        <th>请求上限</th>
                        <th>操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {ladderSteps.map((step, index) => (
                        <tr key={`${step.name}-${index}`}>
                          <td>{index + 1}</td>
                          <td>
                            <input
                              className={styles.tableInput}
                              value={step.name ?? ""}
                              onChange={(event) => updateStep(index, { name: event.target.value })}
                            />
                          </td>
                          <td>
                            <input
                              className={styles.tableInput}
                              min={1}
                              type="number"
                              value={step.concurrency}
                              onChange={(event) => updateStep(index, { concurrency: Number(event.target.value) })}
                            />
                          </td>
                          <td>
                            <input
                              className={styles.tableInput}
                              min={1}
                              type="number"
                              value={step.durationSec}
                              onChange={(event) => updateStep(index, { durationSec: Number(event.target.value) })}
                            />
                          </td>
                          <td>
                            <input
                              className={styles.tableInput}
                              min={0}
                              type="number"
                              value={step.warmupSec ?? 0}
                              onChange={(event) => updateStep(index, { warmupSec: Number(event.target.value) })}
                            />
                          </td>
                          <td>{formatDecimal(Math.max(step.durationSec - (step.warmupSec ?? 0), 0))} 秒</td>
                          <td>
                            <input
                              className={styles.tableInput}
                              min={1}
                              type="number"
                              value={step.timeoutMs ?? 10000}
                              onChange={(event) => updateStep(index, { timeoutMs: Number(event.target.value) })}
                            />
                          </td>
                          <td>
                            <input
                              className={styles.tableInput}
                              min={0}
                              type="number"
                              value={step.requestLimit ?? 0}
                              onChange={(event) =>
                                updateStep(index, {
                                  requestLimit: Number(event.target.value) > 0 ? Number(event.target.value) : undefined
                                })
                              }
                            />
                          </td>
                          <td>
                            <div className={styles.rowActions}>
                              <button className={styles.inlineButton} onClick={() => insertStep(index)} type="button">
                                插入
                              </button>
                              <button className={styles.inlineButton} onClick={() => duplicateStep(index)} type="button">
                                复制
                              </button>
                              <button
                                className={styles.inlineButton}
                                disabled={ladderSteps.length <= 1}
                                onClick={() => removeStep(index)}
                                type="button"
                              >
                                删除
                              </button>
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>

              <div className={styles.actionBar}>
                <div className={styles.actionMeta}>
                  <strong>{validationIssues.length > 0 ? "启动前仍有待修正项" : "阶段表校验通过，可以发起压测"}</strong>
                  <span>{workspaceStatus}</span>
                </div>

                <div className={styles.actionButtons}>
                  {selectedRun ? (
                    <button className={styles.inlineButton} onClick={() => setWorkspaceMode("monitor")} type="button">
                      查看当前运行
                    </button>
                  ) : null}
                  <button className={styles.primaryButton} onClick={handleStartRun} disabled={isSubmitting || validationIssues.length > 0} type="button">
                    {isSubmitting ? "正在提交" : "启动压测"}
                  </button>
                </div>
              </div>

              {validationIssues.length > 0 ? (
                <div className={styles.issueList}>
                  {validationIssues.map((issue) => (
                    <span key={issue}>{issue}</span>
                  ))}
                </div>
              ) : null}
            </section>
          ) : (
            <div className={styles.monitorStage}>
              <section className={`${styles.panel} ${styles.monitorHeader}`}>
                <div className={styles.sectionHeading}>
                  <div>
                    <p className={styles.eyebrow}>运行监控</p>
                    <h2>{selectedRun ? "当前任务" : "暂无运行中的任务"}</h2>
                  </div>
                  <div className={styles.headingActions}>
                    <button className={styles.inlineButton} onClick={() => setWorkspaceMode("configure")} type="button">
                      返回配置
                    </button>
                    {selectedRun && isActiveRunStatus(selectedRun.status) ? (
                      <button className={styles.secondaryButton} disabled={isCancelling} onClick={handleCancelRun} type="button">
                        {isCancelling ? "正在结束" : "结束任务"}
                      </button>
                    ) : null}
                    <button className={styles.inlineButton} disabled={!selectedRun} onClick={() => selectedRun && void copySummary()} type="button">
                      {copied ? "已复制摘要" : "复制摘要"}
                    </button>
                  </div>
                </div>

                <div className={styles.overviewGrid}>
                  <div className={styles.runSummaryCard}>
                    <span>任务概览</span>
                    <strong>{selectedRun ? activeRunCaseLabel : "等待选择运行记录"}</strong>
                    <p>
                      {selectedRun
                        ? `任务编号 ${selectedRun.runId}，目标 ${selectedRun.targetBaseUrl ?? DEFAULT_TARGET_LABEL}，admin 发起于 ${formatTimestamp(selectedRun.createdAt)}`
                        : "先在“准备任务”里配置参数，或者从右侧选择历史运行记录。"}
                    </p>
                    {selectedRun ? <StatusPill status={selectedRun.status} /> : null}
                  </div>
                  <MetricCell label="吞吐" value={formatRps(currentMetrics.rps)} hint="每秒请求数" />
                  <MetricCell label="成功率" value={formatPercent(currentMetrics.successRate)} hint="请求成功占比" tone="good" />
                  <MetricCell label="中位延迟" value={formatLatency(currentMetrics.p50Ms)} hint="50 分位" />
                  <MetricCell label="95 分位延迟" value={formatLatency(currentMetrics.p95Ms)} hint="尾部体验边界" />
                  <MetricCell
                    label="99 分位延迟"
                    value={formatLatency(currentMetrics.p99Ms)}
                    hint="长尾抖动"
                    tone="danger"
                  />
                </div>

                <div className={styles.progressStrip}>
                  <div>
                    <span>当前阶段</span>
                    <strong>{selectedRun?.progress.currentStageName ?? currentStage?.name ?? "等待运行"}</strong>
                  </div>
                  <div>
                    <span>阶段进度</span>
                    <strong>{selectedRun ? `${selectedRun.progress.completedStages}/${selectedRun.progress.totalStages}` : "--"}</strong>
                  </div>
                  <div>
                    <span>任务耗时</span>
                    <strong>{typeof selectedRun?.progress.elapsedSec === "number" ? `${formatDecimal(selectedRun.progress.elapsedSec)} 秒` : "--"}</strong>
                  </div>
                  <div>
                    <span>最后轮询</span>
                    <strong>{formatTimestamp(lastPolledAt)}</strong>
                  </div>
                </div>

                {selectedRun && compareRun ? (
                  <div className={styles.compareStrip}>
                    <div className={styles.deltaBlock}>
                      <span>对比基线</span>
                      <strong>{compareRun.runId}</strong>
                    </div>
                    <div className={`${styles.deltaBlock} ${styles[`delta${capitalize(getDeltaTone(summaryMetrics.rps, baselineMetrics?.rps, "higher"))}`]}`}>
                      <span>吞吐差异</span>
                      <strong>{formatDelta(summaryMetrics.rps, baselineMetrics?.rps, "rps")}</strong>
                    </div>
                    <div className={`${styles.deltaBlock} ${styles[`delta${capitalize(getDeltaTone(summaryMetrics.successRate, baselineMetrics?.successRate, "higher"))}`]}`}>
                      <span>成功率差异</span>
                      <strong>{formatDelta(summaryMetrics.successRate, baselineMetrics?.successRate, "percent")}</strong>
                    </div>
                    <div className={`${styles.deltaBlock} ${styles[`delta${capitalize(getDeltaTone(summaryMetrics.p95Ms, baselineMetrics?.p95Ms, "lower"))}`]}`}>
                      <span>95 分位差异</span>
                      <strong>{formatDelta(summaryMetrics.p95Ms, baselineMetrics?.p95Ms, "latency")}</strong>
                    </div>
                    <div className={`${styles.deltaBlock} ${styles[`delta${capitalize(getDeltaTone(summaryMetrics.p99Ms, baselineMetrics?.p99Ms, "lower"))}`]}`}>
                      <span>99 分位差异</span>
                      <strong>{formatDelta(summaryMetrics.p99Ms, baselineMetrics?.p99Ms, "latency")}</strong>
                    </div>
                  </div>
                ) : null}
              </section>

              <section className={`${styles.panel} ${styles.stagePanel}`}>
                <div className={styles.sectionHeading}>
                  <div>
                    <p className={styles.eyebrow}>阶段结果</p>
                    <h2>阶梯明细</h2>
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
                        <th>并发数</th>
                        <th>持续时长</th>
                        <th>吞吐</th>
                        <th>成功率</th>
                        <th>中位延迟</th>
                        <th>95 分位</th>
                        <th>99 分位</th>
                        <th>失败数</th>
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
                            {formatDecimal(stage.durationSec)} 秒
                            {typeof stage.warmupSec === "number" ? ` / 预热 ${formatDecimal(stage.warmupSec)} 秒` : ""}
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
                <section className={`${styles.panel} ${styles.failurePanel}`}>
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
                      <p>如果后端后续补充错误样本，这里会优先展示最近异常请求。</p>
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
            </div>
          )}
        </main>

        <aside className={styles.sideRail}>
          <section className={`${styles.panel} ${styles.sidePanel}`}>
            <div className={styles.sectionHeading}>
              <div>
                <p className={styles.eyebrow}>最近记录</p>
                <h2>历史运行</h2>
              </div>
              <button className={styles.inlineButton} onClick={() => void loadRuns()} type="button">
                刷新
              </button>
            </div>

            {runsState === "loading" && orderedRuns.length === 0 ? (
              <div className={styles.emptyState}>
                <strong>正在加载运行记录。</strong>
                <p>这里用于选择要查看的运行结果，或者选一条记录作为基线。</p>
              </div>
            ) : orderedRuns.length === 0 ? (
              <div className={styles.emptyState}>
                <strong>还没有运行记录。</strong>
                <p>先在“准备任务”里启动一轮压测。</p>
              </div>
            ) : (
              <div className={styles.historyList}>
                {orderedRuns.map((run) => (
                  <article key={run.runId} className={`${styles.historyRow} ${selectedRunId === run.runId ? styles.historyRowSelected : ""}`}>
                    <button
                      className={styles.historyMain}
                      onClick={() => {
                        setWorkspaceMode("monitor");
                        setSelectedRunId(run.runId);
                      }}
                      type="button"
                    >
                      <div className={styles.historyHead}>
                        <strong>{localizeCaseName(run.caseId, run.caseName)}</strong>
                        <StatusPill status={run.status} />
                      </div>
                      <small>{run.runId}</small>
                      <div className={styles.historyMetrics}>
                        <span>吞吐 {formatRps(run.aggregateMetrics?.rps)}</span>
                        <span>99 分位 {formatLatency(run.aggregateMetrics?.p99Ms)}</span>
                      </div>
                      <small>{formatTimestamp(run.updatedAt ?? run.startedAt)}</small>
                    </button>
                    <div className={styles.historyActions}>
                      <button
                        className={styles.inlineButton}
                        disabled={selectedRunId === run.runId}
                        onClick={() => {
                          setWorkspaceMode("monitor");
                          setSelectedRunId(run.runId);
                        }}
                        type="button"
                      >
                        查看运行
                      </button>
                      <button
                        className={styles.inlineButton}
                        disabled={selectedRunId === run.runId}
                        onClick={() => setCompareRunId(run.runId)}
                        type="button"
                      >
                        选为基线
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </section>

          <section className={`${styles.panel} ${styles.sidePanel}`}>
            <div className={styles.sectionHeading}>
              <div>
                <p className={styles.eyebrow}>基线</p>
                <h2>对比结果</h2>
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
                <p>加载完成后，这里会显示关键指标。</p>
              </div>
            ) : compareRun ? (
              <div className={styles.comparePanel}>
                <strong>{localizeCaseName(compareRun.caseId, compareRun.caseName)}</strong>
                <small>{compareRun.runId}</small>
                <div className={styles.compareMeta}>
                  <span>状态 {formatStatusLabel(compareRun.status)}</span>
                  <span>开始于 {formatTimestamp(compareRun.startedAt)}</span>
                </div>
                <div className={styles.compareMetricList}>
                  <div>
                    <span>吞吐</span>
                    <strong>{formatRps(compareRun.aggregateMetrics?.rps)}</strong>
                  </div>
                  <div>
                    <span>成功率</span>
                    <strong>{formatPercent(compareRun.aggregateMetrics?.successRate)}</strong>
                  </div>
                  <div>
                    <span>95 分位</span>
                    <strong>{formatLatency(compareRun.aggregateMetrics?.p95Ms)}</strong>
                  </div>
                  <div>
                    <span>99 分位</span>
                    <strong>{formatLatency(compareRun.aggregateMetrics?.p99Ms)}</strong>
                  </div>
                </div>
              </div>
            ) : (
              <div className={styles.emptyState}>
                <strong>还没有基线记录。</strong>
                <p>从“最近记录”中选择一条运行结果作为对比基线。</p>
              </div>
            )}
          </section>
        </aside>
      </div>
    </div>
  );
}
