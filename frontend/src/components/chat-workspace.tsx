"use client";

import {
  Activity,
  AlertTriangle,
  ArrowUp,
  BrainCircuit,
  Check,
  ChevronDown,
  Copy,
  FileText,
  Info,
  LoaderCircle,
  Map as MapIcon,
  Network,
  UserRound,
  Waves,
} from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { type FormEvent, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { ResultTable } from "@/components/result-table";
import { QuestionSuggestions } from "@/components/question-suggestions";
import { SessionSidebar } from "@/components/session-sidebar";
import { apiBaseUrl, getSession } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import type {
  AgentResponse,
  AgentStep,
  AgentStreamEvent,
  ChatMessage,
  SessionDetail,
} from "@/lib/types";

const AnalysisChart = dynamic(
  () => import("@/components/analysis-chart").then((module) => module.AnalysisChart),
  { ssr: false, loading: () => <div className="analysis-chart chart-loading">正在加载图表…</div> },
);

type Run = {
  id: string;
  question: string;
  createdAt: string;
  completedAt?: string;
  steps: AgentStep[];
  streamedReasoning: string;
  streamedAnswer: string;
  response?: AgentResponse;
  error?: string;
  historical?: boolean;
  model?: string;
  sources?: string[];
  traceId?: string;
};

export function ChatWorkspace() {
  const [question, setQuestion] = useState("");
  const [runs, setRuns] = useState<Run[]>([]);
  const [sessionId, setSessionId] = useState<string>();
  const [historyVersion, setHistoryVersion] = useState(0);
  const [loadingSession, setLoadingSession] = useState(false);
  const [busy, setBusy] = useState(false);
  const followOutput = useRef(true);
  const canSubmit = question.trim().length > 0 && !busy;
  const latestRun = runs[runs.length - 1];
  const latestTracedRun = [...runs].reverse().find((run) => run.traceId);
  const currentTraceHref = sessionId && latestTracedRun?.traceId
    ? traceHref(sessionId, latestTracedRun.traceId)
    : undefined;
  const traceIsRunning = Boolean(busy && latestRun?.traceId === latestTracedRun?.traceId);

  useEffect(() => {
    function updateFollowPreference() {
      const remaining = document.documentElement.scrollHeight - window.scrollY - window.innerHeight;
      followOutput.current = remaining < 180;
    }

    window.addEventListener("scroll", updateFollowPreference, { passive: true });
    return () => window.removeEventListener("scroll", updateFollowPreference);
  }, []);

  useEffect(() => {
    if (!followOutput.current) {
      return;
    }
    const frame = requestAnimationFrame(() => {
      window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "auto" });
    });
    return () => cancelAnimationFrame(frame);
  }, [runs]);

  async function submit(nextQuestion: string) {
    const normalized = nextQuestion.trim();
    if (!normalized || busy) {
      return;
    }
    const id = crypto.randomUUID();
    followOutput.current = true;
    setRuns((current) => [
      ...current,
      {
        id,
        question: normalized,
        createdAt: new Date().toISOString(),
        steps: [],
        streamedReasoning: "",
        streamedAnswer: "",
      },
    ]);
    setQuestion("");
    setBusy(true);

    let pendingDelta = "";
    let pendingEvent: "reasoning_delta" | "answer_delta" | null = null;
    let flushTimer: number | null = null;

    const applyToRun = (event: AgentStreamEvent) => {
      setRuns((current) =>
        current.map((run) => (run.id === id ? applyEvent(run, event) : run)),
      );
    };

    const flushDelta = () => {
      flushTimer = null;
      if (!pendingDelta || !pendingEvent) {
        return;
      }
      const delta = pendingDelta;
      const event = pendingEvent;
      pendingDelta = "";
      pendingEvent = null;
      applyToRun({ event, delta });
    };

    const handleEvent = (event: AgentStreamEvent) => {
      if (event.event === "run_started" && event.session_id) {
        setSessionId(event.session_id);
      }
      if (event.event === "complete" || event.event === "error") {
        setHistoryVersion((current) => current + 1);
      }
      if (
        (event.event === "reasoning_delta" || event.event === "answer_delta") &&
        event.delta
      ) {
        if (pendingEvent !== null && pendingEvent !== event.event) {
          if (flushTimer !== null) {
            window.clearTimeout(flushTimer);
          }
          flushDelta();
        }
        pendingEvent = event.event;
        pendingDelta += event.delta;
        if (flushTimer === null) {
          flushTimer = window.setTimeout(flushDelta, 40);
        }
        return;
      }

      if (flushTimer !== null) {
        window.clearTimeout(flushTimer);
      }
      flushDelta();
      applyToRun(event);
    };

    try {
      await streamQuestion(normalized, sessionId, handleEvent);
    } catch (error) {
      const message = error instanceof Error ? error.message : "请求失败";
      setRuns((current) =>
        current.map((run) => (
          run.id === id
            ? { ...run, error: message, completedAt: new Date().toISOString() }
            : run
        )),
      );
    } finally {
      if (flushTimer !== null) {
        window.clearTimeout(flushTimer);
      }
      flushDelta();
      setBusy(false);
    }
  }

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void submit(question);
  }

  function startNewSession() {
    setSessionId(undefined);
    setRuns([]);
    setQuestion("");
  }

  function handleSessionsDeleted(sessionIds: string[]) {
    if (sessionId && sessionIds.includes(sessionId)) {
      startNewSession();
    }
  }

  async function selectSession(nextSessionId: string) {
    if (busy || loadingSession || nextSessionId === sessionId) {
      return;
    }
    setLoadingSession(true);
    try {
      const session = await getSession(nextSessionId);
      setSessionId(session.id);
      setRuns(runsFromSession(session));
      window.scrollTo({ top: 0, behavior: "auto" });
    } catch (error) {
      const message = error instanceof Error ? error.message : "无法加载对话";
      setRuns([failedHistoryRun(nextSessionId, message)]);
    } finally {
      setLoadingSession(false);
    }
  }

  return (
    <div className="app-shell">
      <SessionSidebar
        activeSessionId={sessionId}
        refreshVersion={historyVersion}
        disabled={busy || loadingSession}
        onNewSession={startNewSession}
        onSelectSession={(id) => void selectSession(id)}
        onSessionsDeleted={handleSessionsDeleted}
      />

      <main className="workspace">
        <header className="topbar">
          <div>
            <span className="context-label">Tara Agent</span>
            <h1>海洋数据分析</h1>
          </div>
          {currentTraceHref ? (
            <Link
              className="topbar-trace-link"
              href={currentTraceHref}
              target={traceIsRunning ? "_blank" : undefined}
              rel={traceIsRunning ? "noopener noreferrer" : undefined}
            >
              <Activity size={15} aria-hidden="true" />
              {traceIsRunning ? "查看实时链路" : "查看链路"}
            </Link>
          ) : null}
        </header>

        <div className="conversation" aria-live="polite">
          {loadingSession ? <div className="workspace-loading"><LoaderCircle className="spin" />正在加载对话…</div> : null}
          {!loadingSession && runs.length === 0 ? <EmptyState onExample={submit} /> : null}
          {runs.map((run) => <AnalysisRun key={run.id} run={run} />)}
        </div>

        <div className="composer-wrap">
          <form className="composer" onSubmit={onSubmit}>
            <textarea
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  if (canSubmit) {
                    void submit(question);
                  }
                }
              }}
              maxLength={2000}
              rows={2}
              placeholder="询问样本、分类群、丰度、多样性或环境关联…"
              aria-label="输入 Tara 数据问题"
            />
            <button className="send-button" type="submit" disabled={!canSubmit}>
              {busy ? <LoaderCircle className="spin" size={18} /> : <ArrowUp size={18} />}
              <span>{busy ? "分析中" : "发送"}</span>
            </button>
          </form>
        </div>
      </main>
    </div>
  );
}

function EmptyState({ onExample }: { onExample: (question: string) => Promise<void> }) {
  return (
    <section className="empty-state">
      <div className="empty-icon"><Waves size={27} aria-hidden="true" /></div>
      <p className="context-label">Tara Agent</p>
      <QuestionSuggestions onSelect={onExample} />
    </section>
  );
}

function AnalysisRun({ run }: { run: Run }) {
  const inProgress = !run.historical && !run.response && !run.error;
  const reasoningActive = inProgress && !run.streamedAnswer;
  const answer = run.response?.answer ?? run.streamedAnswer;
  const analysis = run.response?.route?.kind === "analysis"
    || run.steps.some((step) => step.stage === "execute" || step.stage === "answer");

  return (
    <article className="analysis-run">
      <UserMessage question={run.question} createdAt={run.createdAt} />
      <div className="agent-response">
        <div className={`agent-avatar${inProgress ? " running" : ""}`} aria-hidden="true">
          {inProgress ? <Activity className="analysis-running-icon" size={18} /> : <Network size={17} />}
        </div>
        <div className="agent-message">
          <div className="response-content">
            {!run.historical || run.response ? (
              <ProcessPanel
                steps={run.steps}
                response={run.response}
                active={inProgress}
                analysis={analysis}
              />
            ) : null}
            {run.streamedReasoning ? (
              <ReasoningPanel reasoning={run.streamedReasoning} active={reasoningActive} />
            ) : null}
            {answer ? (
              <AnswerPanel
                answer={answer}
                active={inProgress}
                analysis={analysis}
              />
            ) : null}
            {run.error ? (
              <div className="request-error"><AlertTriangle size={17} />{run.error}</div>
            ) : null}
            {run.response ? (
              <CompletedArtifacts
                response={run.response}
                timestamp={run.completedAt ?? run.createdAt}
              />
            ) : null}
          </div>
          {run.historical && !run.response ? (
            <HistoricalMeta run={run} />
          ) : !run.response ? (
            <div className="message-meta agent-message-meta">
              <time dateTime={run.completedAt ?? run.createdAt}>
                {formatDateTime(run.completedAt ?? run.createdAt)}
              </time>
            </div>
          ) : null}
        </div>
      </div>
    </article>
  );
}

function UserMessage({ question, createdAt }: { question: string; createdAt: string }) {
  const [copied, setCopied] = useState(false);
  const resetTimer = useRef<number | null>(null);

  useEffect(() => () => {
    if (resetTimer.current !== null) {
      window.clearTimeout(resetTimer.current);
    }
  }, []);

  async function copyQuestion() {
    try {
      await navigator.clipboard.writeText(question);
    } catch {
      return;
    }
    setCopied(true);
    if (resetTimer.current !== null) {
      window.clearTimeout(resetTimer.current);
    }
    resetTimer.current = window.setTimeout(() => setCopied(false), 1_500);
  }

  return (
    <div className="user-turn">
      <div className="user-message-block">
        <div className="user-message">{question}</div>
        <div className="message-meta user-message-meta">
          <time dateTime={createdAt}>{formatDateTime(createdAt)}</time>
          <button
            type="button"
            className="copy-message"
            onClick={() => void copyQuestion()}
            aria-label={copied ? "已复制问题" : "复制问题"}
            title={copied ? "已复制" : "复制问题"}
          >
            {copied ? <Check size={13} /> : <Copy size={13} />}
          </button>
        </div>
      </div>
      <div className="user-avatar" aria-hidden="true"><UserRound size={16} /></div>
    </div>
  );
}

function HistoricalMeta({ run }: { run: Run }) {
  return (
    <footer className="provenance">
      <time dateTime={run.completedAt ?? run.createdAt}>
        {formatDateTime(run.completedAt ?? run.createdAt)}
      </time>
      {run.model ? <span>模型 {run.model}</span> : null}
      {run.sources && run.sources.length > 0 ? (
        <span className="source-files">
          数据来源
          {run.sources.map((source) => <code key={source}>{source}</code>)}
        </span>
      ) : null}
    </footer>
  );
}

type ProcessPanelProps = {
  steps: AgentStep[];
  response?: AgentResponse;
  active: boolean;
  analysis: boolean;
};

function ProcessPanel({ steps, response, active, analysis }: ProcessPanelProps) {
  const serializedArguments = useMemo(
    () => response?.tool ? JSON.stringify(response.tool.arguments, null, 2) : "",
    [response],
  );
  return (
    <DisclosureSection
      className={`process-panel${active ? " active" : ""}`}
      icon={active ? <LoaderCircle className="spin" size={16} /> : <Network size={16} />}
      title={analysis ? "分析过程" : "处理过程"}
      meta={active ? "正在进行" : `${steps.length} 个步骤`}
    >
      {steps.length > 0 ? (
        <ol>
          {steps.map((step) => <li key={step.stage}><strong>{step.title}</strong><span>{step.detail}</span></li>)}
        </ol>
      ) : (
        <p className="progress-placeholder">
          正在理解问题
          <span className="progress-dots" aria-hidden="true"><i /><i /><i /></span>
        </p>
      )}
      {response?.tool ? (
        <div className="tool-call">
          <span>调用工具</span><code>{response.tool.name}</code>
          <pre>{serializedArguments}</pre>
        </div>
      ) : null}
    </DisclosureSection>
  );
}

function ReasoningPanel({ reasoning, active }: { reasoning: string; active: boolean }) {
  const [open, setOpen] = useState(active);
  const previousActive = useRef(active);
  const contentRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (previousActive.current !== active) {
      setOpen(active);
      previousActive.current = active;
    }
  }, [active]);

  useEffect(() => {
    if (!active || !open) {
      return;
    }
    const content = contentRef.current;
    content?.scrollTo({ top: content.scrollHeight, behavior: "auto" });
  }, [active, open, reasoning]);

  return (
    <details
      className={`response-section reasoning-panel${active ? " active" : ""}`}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <BrainCircuit size={16} />
        <span>{active ? "正在思考" : "模型思考"}</span>
        <small>{active ? "实时生成" : "已完成"}</small>
        <ChevronDown size={16} />
      </summary>
      <div className="section-body">
        <div ref={contentRef} className="reasoning-content">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{reasoning}</ReactMarkdown>
        </div>
      </div>
    </details>
  );
}

function AnswerPanel({
  answer,
  active,
  analysis,
}: {
  answer: string;
  active: boolean;
  analysis: boolean;
}) {
  return (
    <DisclosureSection
      className="answer-panel"
      icon={<FileText size={16} />}
      title={analysis ? "分析结论" : "回答"}
      meta={active ? "正在生成" : "已完成"}
    >
      <div className={active ? "streaming-answer" : undefined}>
        <MarkdownAnswer answer={answer} />
        {active ? <span className="stream-caret" aria-hidden="true" /> : null}
      </div>
    </DisclosureSection>
  );
}

function MarkdownAnswer({ answer }: { answer: string }) {
  return (
    <div className="answer-text">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{answer}</ReactMarkdown>
    </div>
  );
}

function CompletedArtifacts({
  response,
  timestamp,
}: {
  response: AgentResponse;
  timestamp: string;
}) {
  return (
    <>
      {response.warnings.length > 0 ? (
        <DisclosureSection
          className="notes-panel"
          icon={<Info size={16} />}
          title="数据说明"
          meta={`${response.warnings.length} 项`}
        >
          <div className="analysis-notes">
            {response.warnings.map((warning) => (
              <div key={warning.code}>
                <strong>{warningTitle(warning.code)}</strong>
                <span>{warning.message}</span>
              </div>
            ))}
          </div>
        </DisclosureSection>
      ) : null}

      {response.charts.map((chart) => (
        <DisclosureSection
          key={`${chart.kind}-${chart.title}`}
          className="chart-panel"
          icon={<MapIcon size={16} />}
          title="可视化结果"
          meta={chart.title}
        >
          <AnalysisChart chart={chart} />
        </DisclosureSection>
      ))}
      <ResultTable result={response.result} />

      <footer className="provenance">
        <time dateTime={timestamp}>{formatDateTime(timestamp)}</time>
        <span>模型 {response.model}</span>
        {response.tool ? (
          <span className="source-files">
            数据来源
            {response.sources.length > 0
              ? response.sources.map((source) => <code key={source}>{source}</code>)
              : "未声明"}
          </span>
        ) : null}
      </footer>
    </>
  );
}

type DisclosureSectionProps = {
  className?: string;
  icon: ReactNode;
  title: string;
  meta?: string;
  children: ReactNode;
};

function DisclosureSection({ className = "", icon, title, meta, children }: DisclosureSectionProps) {
  const [open, setOpen] = useState(true);

  return (
    <details
      className={`response-section ${className}`.trim()}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        {icon}
        <span>{title}</span>
        {meta ? <small>{meta}</small> : null}
        <ChevronDown size={16} />
      </summary>
      <div className="section-body">{children}</div>
    </details>
  );
}

function warningTitle(code: string): string {
  const titles: Record<string, string> = {
    missing_filter_values_excluded: "缺失值处理",
    missing_environment_values_excluded: "缺失值处理",
    missing_group_values_excluded: "缺失值处理",
    samples_missing_marker_excluded: "样本覆盖范围",
    read_count_is_not_cell_abundance: "指标解释",
    unrarefied_diversity: "计算方法",
    asymptotic_p_value_caution: "统计解释",
    insufficient_samples: "样本量限制",
    constant_input: "统计限制",
    undefined_correlation: "统计限制",
    taxon_not_found: "匹配结果",
    zero_reads_in_analysis_set: "零读数处理",
    zero_read_library: "零读数处理",
  };
  return titles[code] ?? "结果解释";
}

function runsFromSession(session: SessionDetail): Run[] {
  const assistantsByParent = new Map<string, ChatMessage>();
  for (const message of session.messages) {
    if (message.role === "assistant" && message.parent_message_id) {
      assistantsByParent.set(message.parent_message_id, message);
    }
  }

  return session.messages
    .filter((message) => message.role === "user")
    .map((userMessage) => {
      const assistant = assistantsByParent.get(userMessage.id);
      const failed = assistant?.status === "failed";
      const response = assistant?.agent_response ?? undefined;
      return {
        id: userMessage.trace_id ?? userMessage.id,
        question: userMessage.content,
        createdAt: userMessage.created_at,
        completedAt: assistant?.updated_at,
        steps: response?.steps ?? [],
        streamedReasoning: response?.reasoning ?? reasoningFromMessage(assistant),
        streamedAnswer: response?.answer ?? assistant?.content ?? "",
        response,
        error: failed ? errorFromMessage(assistant) : undefined,
        historical: true,
        model: stringMetadata(assistant, "model"),
        sources: stringListMetadata(assistant, "sources"),
        traceId: userMessage.trace_id ?? undefined,
      };
    });
}

function reasoningFromMessage(message?: ChatMessage): string {
  if (!message) {
    return "";
  }
  const part = message.content_parts.find((item) => item.type === "reasoning");
  return part && typeof part.content === "string" ? part.content : "";
}

function errorFromMessage(message?: ChatMessage): string {
  const value = message?.metadata.error_message;
  return typeof value === "string" && value ? value : "本次分析未完成";
}

function stringMetadata(message: ChatMessage | undefined, key: string): string | undefined {
  const value = message?.metadata[key];
  return typeof value === "string" && value ? value : undefined;
}

function stringListMetadata(message: ChatMessage | undefined, key: string): string[] {
  const value = message?.metadata[key];
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is string => typeof item === "string" && item.length > 0);
}

function failedHistoryRun(sessionId: string, message: string): Run {
  const now = new Date().toISOString();
  return {
    id: `history-error-${sessionId}`,
    question: "加载历史对话",
    createdAt: now,
    completedAt: now,
    steps: [],
    streamedReasoning: "",
    streamedAnswer: "",
    error: message,
    historical: true,
  };
}

function applyEvent(run: Run, event: AgentStreamEvent): Run {
  if (event.event === "run_started" && event.trace_id) {
    return { ...run, traceId: event.trace_id };
  }
  if (event.event === "step" && event.step) {
    return { ...run, steps: [...run.steps, event.step] };
  }
  if (event.event === "answer_delta" && event.delta) {
    return { ...run, streamedAnswer: run.streamedAnswer + event.delta };
  }
  if (event.event === "reasoning_delta" && event.delta) {
    return { ...run, streamedReasoning: run.streamedReasoning + event.delta };
  }
  if (event.event === "complete" && event.response) {
    return {
      ...run,
      response: event.response,
      steps: event.response.steps,
      streamedReasoning: event.response.reasoning,
      streamedAnswer: event.response.answer,
      completedAt: new Date().toISOString(),
    };
  }
  if (event.event === "error") {
    return {
      ...run,
      error: event.error ?? "分析失败",
      completedAt: new Date().toISOString(),
    };
  }
  return run;
}

function traceHref(sessionId: string, traceId: string): string {
  return `/sessions/${encodeURIComponent(sessionId)}/traces/${encodeURIComponent(traceId)}`;
}

async function streamQuestion(
  question: string,
  sessionId: string | undefined,
  onEvent: (event: AgentStreamEvent) => void,
) {
  const response = await fetch(`${apiBaseUrl}/api/v1/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    credentials: "include",
    body: JSON.stringify({ question, session_id: sessionId }),
  });
  if (!response.ok || !response.body) {
    if (response.status === 401) {
      window.dispatchEvent(new Event("tara-auth-expired"));
    }
    const payload = await response.json().catch(() => null) as { detail?: string } | null;
    throw new Error(payload?.detail ?? `请求失败（HTTP ${response.status}）`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminalEventReceived = false;

  function emitBlock(block: string) {
    const data = block
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data) {
      return;
    }
    const event = JSON.parse(data) as AgentStreamEvent;
    if (event.event === "complete" || event.event === "error") {
      terminalEventReceived = true;
    }
    onEvent(event);
  }

  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done }).replaceAll("\r\n", "\n");
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() ?? "";
    for (const block of blocks) {
      emitBlock(block);
    }
    if (done) {
      break;
    }
  }
  if (buffer.trim()) {
    emitBlock(buffer);
  }
  if (!terminalEventReceived) {
    throw new Error("流式响应意外中断，请重试");
  }
}
