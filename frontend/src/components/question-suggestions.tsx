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
          onClick={() => void refresh()}
        >
          <RefreshCw className={loading ? "spin" : undefined} size={15} aria-hidden="true" />
          换一批
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
                  <button
                    key={item.id}
                    type="button"
                    disabled={loading}
                    onClick={() => void onSelect(item.question)}
                  >
                    <span className="example-card-label">
                      <Icon size={16} aria-hidden="true" />
                      {item.category}
                    </span>
                    <span className="example-card-question">{item.question}</span>
                  </button>
                );
              })}
        </div>
      )}
    </div>
  );
}
