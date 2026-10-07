"use client";

import {
  BarChart3,
  Braces,
  Database,
  FileSearch,
  FlaskConical,
  Lightbulb,
  Map as MapIcon,
  RefreshCw,
  Workflow,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { getQuestionSuggestions } from "@/lib/api";
import type { QuestionSuggestion } from "@/lib/types";

const categoryIcons = {
  "样本筛选": MapIcon,
  "样本详情": FileSearch,
  "分类群检索": Braces,
  "丰度分布": BarChart3,
  "多样性比较": Database,
  "环境关联": FlaskConical,
  "候选功能谱": Braces,
  "候选条件敏感性": Workflow,
  "丰度与环境核查": Workflow,
  "V4 / V9 趋势比较": Workflow,
};

const taskLabels: Record<QuestionSuggestion["task_type"], string> = {
  data_query: "数据检索",
  statistical_analysis: "统计分析",
  function_analysis: "候选功能分析",
  multi_step_analysis: "多步骤分析",
  research_discussion: "结果与方法讨论",
  literature_review: "资料研读",
};

const datasetLabels: Record<string, string> = {
  context_general: "context_general.tsv",
  context_stat: "context_stat.tsv",
  "18s_v4": "TARA-Oceans_18S-V4_dada2_table.tsv",
  "18s_v9": "TARA-Oceans_18S-V9_dada2_table.tsv",
  matou_taxonomy: "MATOU-v1.5.taxonomy.tsv.gz",
  matou_pfam: "MATOU-v1.5.pfam.gz",
  matou_metag: "MATOU-v1.5.metaG.occurrences.gz",
  matou_metat: "MATOU-v1.5.metaT.occurrences.gz",
};

type QuestionSuggestionsProps = {
  onSelect: (question: string) => Promise<void>;
};

export function QuestionSuggestions({ onSelect }: QuestionSuggestionsProps) {
  const [items, setItems] = useState<QuestionSuggestion[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();
  const recentIds = useRef<string[]>([]);

  function remember(newItems: QuestionSuggestion[]) {
    const ids = [...recentIds.current, ...newItems.map((item) => item.id)];
    recentIds.current = [...new Set(ids)].slice(-8);
  }

  useEffect(() => {
    const controller = new AbortController();
    getQuestionSuggestions([], controller.signal)
      .then((response) => {
        if (response.items.length === 0) {
          setError("当前没有可用的示例问题");
          return;
        }
        setItems(response.items);
        remember(response.items);
        setError(undefined);
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") {
          return;
        }
        setError(reason instanceof Error ? reason.message : "无法加载示例问题");
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, []);

  async function refresh() {
    setLoading(true);
    setError(undefined);
    try {
      const response = await getQuestionSuggestions(recentIds.current);
      if (response.items.length === 0) {
        setError("当前没有可用的示例问题");
        return;
      }
      setItems(response.items);
      remember(response.items);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法加载示例问题");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="suggestions">
      <div className="suggestion-toolbar">
        <button
          className="suggestion-refresh"
          type="button"
          disabled={loading}
          aria-label={loading ? "正在更新推荐问题" : "换一批推荐问题"}
          aria-busy={loading}
          onClick={() => void refresh()}
        >
          <span className="suggestion-refresh-icon">
            <RefreshCw className={loading ? "spin" : undefined} size={18} aria-hidden="true" />
          </span>
          <span aria-live="polite">{loading ? "正在更新" : "换一批问题"}</span>
        </button>
      </div>
      {error && items.length > 0 ? <p className="suggestion-inline-error">{error}</p> : null}
      {error && items.length === 0 ? (
        <div className="suggestion-error" role="status">
          <span>{error}</span>
          <button type="button" onClick={() => void refresh()}>重试</button>
        </div>
      ) : (
        <div className={`example-grid${loading ? " loading" : ""}`} aria-label="选择一个示例问题">
          {items.length === 0
            ? Array.from({ length: 4 }, (_, index) => (
                <div className="example-card-skeleton" key={index} aria-hidden="true" />
              ))
            : items.map((item) => {
                const Icon = categoryIcons[item.category as keyof typeof categoryIcons] ?? Lightbulb;
                return (
                  <article className="example-card" key={item.id}>
                    <button
                      className="example-question-button"
                      type="button"
                      disabled={loading || item.availability === "planned"}
                      onClick={() => void onSelect(item.question)}
                    >
                      <span className="example-card-heading">
                        <span className="example-card-label">
                          <Icon size={16} aria-hidden="true" />
                          {item.category}
                        </span>
                        <span className={`example-task-badge ${item.task_type}`}>
                          {taskLabels[item.task_type] ?? "数据检索"}
                        </span>
                      </span>
                      <span className="example-card-question">{item.question}</span>
                    </button>
                    <div className="example-data-details">
                      <div className="example-data-heading">
                        <Database size={14} aria-hidden="true" />
                        涉及数据集（{item.datasets.length}）
                      </div>
                      <div className="example-datasets" aria-label="涉及的数据集">
                        {item.datasets.map((dataset) => (
                          <span className="example-dataset-chip" key={dataset} title={datasetLabels[dataset] ?? dataset}>
                            {datasetLabels[dataset] ?? dataset}
                          </span>
                        ))}
                      </div>
                    </div>
                    {item.limitation ? <p className="example-limitation">{item.limitation}</p> : null}
                  </article>
                );
              })}
        </div>
      )}
    </div>
  );
}
