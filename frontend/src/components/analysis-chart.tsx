"use client";

import { Download } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { Config, Data, Layout } from "plotly.js";

import { createDownloadName } from "@/lib/downloads";
import type { ChartSpec } from "@/lib/types";

type AnalysisChartProps = {
  chart: ChartSpec;
};

export function AnalysisChart({ chart }: AnalysisChartProps) {
  const chartRef = useRef<HTMLDivElement>(null);
  const plotlyRef = useRef<typeof import("plotly.js-dist-min").default | null>(null);
  const [ready, setReady] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;
    const element = chartRef.current;

    async function renderChart() {
      try {
        const plotly = (await import("plotly.js-dist-min")).default;
        if (disposed || !element) {
          return;
        }
        plotlyRef.current = plotly;
        const theme = chartTheme();
        await plotly.react(
          element,
          chartData(chart, theme),
          chartLayout(chart, theme),
          chartConfig,
        );
        if (!disposed) {
          setReady(true);
        }
      } catch {
        if (!disposed) {
          setError("图表加载失败");
        }
      }
    }

    void renderChart();
    return () => {
      disposed = true;
      if (plotlyRef.current && element) {
        plotlyRef.current.purge(element);
      }
      plotlyRef.current = null;
    };
  }, [chart]);

  useEffect(() => {
    const element = chartRef.current;
    const plotly = plotlyRef.current;
    if (!ready || !element || !plotly) return;
    let previousWidth = 0;
    let disposed = false;
    // 卡片从折叠变为可见时重新适配宽度，避免图表按隐藏容器的尺寸绘制。
    const observer = new ResizeObserver(([entry]) => {
      const width = entry.contentRect.width;
      if (width > 0 && width !== previousWidth) {
        previousWidth = width;
        void Promise.resolve(plotly.Plots.resize(element)).catch(() => {
          if (!disposed) setError("图表尺寸更新失败，请重新展开卡片");
        });
      }
    });
    observer.observe(element);
    return () => {
      disposed = true;
      observer.disconnect();
    };
  }, [ready, chart]);

  async function handleDownload() {
    const element = chartRef.current;
    const plotly = plotlyRef.current;
    if (!element || !plotly || !ready) {
      return;
    }

    setDownloading(true);
    setError(null);
    try {
      await plotly.downloadImage(element, {
        format: "png",
        width: null,
        height: null,
        filename: createDownloadName(chart.title),
      });
    } catch {
      setError("图表下载失败，请重试");
    } finally {
      setDownloading(false);
    }
  }

  return (
    <>
      <div className="artifact-toolbar">
        <button
          type="button"
          className="artifact-download"
          disabled={!ready || downloading}
          onClick={() => void handleDownload()}
          aria-label={`下载图表 ${chart.title}`}
        >
          <Download size={14} aria-hidden="true" />
          {downloading ? "正在下载" : "下载 PNG"}
        </button>
      </div>
      <div className="analysis-chart-scroll">
        <div ref={chartRef} className="analysis-chart" style={{ height: chartHeight(chart),
          minWidth: ["horizontal_bar", "stacked_bar"].includes(chart.kind) ? 760
            : chart.kind === "correlation_circle" ? 620 : undefined }} aria-label={chart.title} />
      </div>
      {chart.kind === "correlation_circle" && (
        <p className="chart-reading-note">标签引线指向原始变量点，文字位置不代表坐标；悬停查看坐标，完整数值见 PLS 变量相关坐标表。</p>
      )}
      {error ? <p className="artifact-error" role="status">{error}</p> : null}
    </>
  );
}

const chartConfig: Partial<Config> = {
  displaylogo: false,
  responsive: true,
  showSendToCloud: false,
  topojsonURL: "/plotly-topojson/",
  modeBarButtonsToRemove: ["lasso2d", "select2d", "toImage"],
};

type ChartTheme = {
  ink: string;
  accent: string;
  coral: string;
  line: string;
  lineStrong: string;
  ocean: string;
  land: string;
};

function chartTheme(): ChartTheme {
  const styles = getComputedStyle(document.documentElement);
  const color = (name: string) => styles.getPropertyValue(name).trim();
  return {
    ink: color("--ink"),
    accent: color("--accent"),
    coral: color("--coral"),
    line: color("--line"),
    lineStrong: color("--line-strong"),
    ocean: color("--accent-soft"),
    land: color("--map-land"),
  };
}

function chartData(chart: ChartSpec, theme: ChartTheme): Data[] {
  if (chart.kind === "horizontal_bar") {
    return [{ type: "bar", orientation: "h", x: chart.y, y: chart.x,
      customdata: chart.labels, text: chart.y.map((value) => `${(value * 100).toFixed(3)}%`),
      textposition: "outside", cliponaxis: false, marker: { color: theme.accent },
      hovertemplate: "%{y}<br>%{customdata}<br>贡献 %{x:.3%}<extra></extra>" }];
  }
  if (chart.kind === "stacked_bar") {
    const palette = ["#286fa5", "#389b91", "#d58c35", "#925aa3", "#536a7b", "#c75c65", "#5c8d3b", "#6a77ba", "#af753d"];
    return (chart.series ?? []).map((series, index) => ({ type: "bar", orientation: "h",
      name: series.name, x: series.values ?? [], y: chart.x,
      marker: { color: palette[index % palette.length] },
      hovertemplate: "%{y}<br>%{fullData.name}: %{x:.2%}<extra></extra>" }));
  }
  if (chart.kind === "correlation_circle") {
    const categories = ["environment", "MetaG", "MetaT", "response"];
    return categories.flatMap((category): Data[] => {
      const indices = chart.labels.map((_, i) => i).filter((i) => circleCategory(chart, i) === category);
      if (!indices.length) return [];
      const color = circleCategoryColor(category, theme);
      return [{ type: "scatter", mode: "lines", showlegend: false, hoverinfo: "skip",
        x: indices.flatMap((i) => [0, Number(chart.x[i]), null]),
        y: indices.flatMap((i) => [0, chart.y[i], null]),
        line: { color, width: 1 }, opacity: 0.45, connectgaps: false },
        { type: "scatter", mode: "markers", name: circleCategoryName(category),
        legendgroup: category, showlegend: true,
        x: indices.map((i) => chart.x[i]), y: indices.map((i) => chart.y[i]),
        text: indices.map((i) => chart.labels[i]),
        marker: { size: 9, color },
        hovertemplate: "%{text}<br>成分1: %{x:.3f}<br>成分2: %{y:.3f}<extra></extra>" }];
    });
  }

  if (chart.kind === "sample_map") {
    const abundance = chart.series?.find((s) => s.name === "abundance")?.values;
    const diversity = chart.series?.find((s) => s.name === "diversity")?.values;
    const largest = Math.max(...(abundance ?? []).map((v) => v ?? 0), 0.001);
    return [
      {
        type: "scattergeo",
        mode: "markers",
        lon: chart.x as number[],
        lat: chart.y,
        text: chart.labels,
        hovertemplate: "%{text}<br>经度 %{lon:.2f}<br>纬度 %{lat:.2f}<extra></extra>",
        marker: { color: diversity?.map((v) => v ?? 0) ?? theme.accent,
          colorscale: "Viridis", showscale: Boolean(diversity),
          colorbar: { title: { text: "exp(Shannon)" } },
          size: abundance?.map((v) => v ?? 0) ?? 8,
          sizemode: "area", sizeref: 2 * largest / (24 * 24), sizemin: 3,
          symbol: abundance?.map((v) => v === 0 ? "x" : "circle") ?? "circle",
          line: { color: "#ffffff", width: 1 } },
      },
    ];
  }
  if (chart.kind === "heatmap") {
    return [{ type: "heatmap", x: chart.x, y: chart.labels,
      z: (chart.series ?? []).map((s) => s.values ?? []),
      text: (chart.series ?? []).map((s) => s.text ?? []),
      zmin: -1, zmax: 1, colorscale: "RdBu", reversescale: true,
      hovertemplate: "%{y} × %{x}<br>ρ=%{z:.3f}<br>%{text}<extra></extra>" } as unknown as Data];
  }
  if (chart.kind === "ordination") {
    return [{ type: "scatter", mode: "markers", name: "采样组", x: chart.x, y: chart.y, text: chart.labels,
      hovertemplate: "%{text}<br>NMDS1=%{x:.4g}<br>NMDS2=%{y:.4g}<extra></extra>" },
      ...(chart.series ?? []).map((s): Data => ({ type: "scatter", mode: "text+lines", name: s.name,
        x: s.x, y: s.y, text: ["", s.name], textposition: "top center",
        hovertemplate: "%{fullData.name}<extra></extra>" }))];
  }
  if (chart.kind === "bar") {
    return [
      {
        type: "bar",
        x: chart.x,
        y: chart.y,
        text: chart.labels,
        marker: { color: theme.accent },
        hovertemplate: "%{x}<br>%{y:.4g}<extra></extra>",
      },
    ];
  }
  return [
    {
      type: "scatter",
      mode: "markers",
      x: chart.x,
      y: chart.y,
      text: chart.labels,
      marker: { color: theme.coral, size: 8, opacity: 0.78 },
      hovertemplate: "%{text}<br>x %{x:.4g}<br>y %{y:.4g}<extra></extra>",
    },
  ];
}

function circleCategory(chart: ChartSpec, index: number): string {
  const response = chart.series?.find((s) => s.name === "response")?.indices?.includes(index);
  if (!response) return "environment";
  if (chart.labels[index].startsWith("MetaG_")) return "MetaG";
  if (chart.labels[index].startsWith("MetaT_")) return "MetaT";
  return "response";
}

function circleCategoryName(category: string): string {
  return ({ environment: "环境变量", MetaG: "MetaG（DNA）", MetaT: "MetaT（RNA）", response: "响应指标" })[category] ?? category;
}

function circleCategoryColor(category: string, theme: ChartTheme): string {
  return category === "environment" ? theme.accent : category === "MetaG" ? "#bd784c"
    : category === "MetaT" ? "#c74750" : theme.coral;
}

function circleAnnotations(chart: ChartSpec, theme: ChartTheme): NonNullable<Layout["annotations"]> {
  // 只移动文字：按原始纵坐标排序后在圆的左右两侧排开，引线保留点与名称的对应关系。
  const annotations: NonNullable<Layout["annotations"]> = [];
  for (const side of [-1, 1]) {
    const indices = chart.labels.map((_, i) => i)
      .filter((i) => (Number(chart.x[i]) < 0 ? -1 : 1) === side)
      .sort((a, b) => chart.y[a] - chart.y[b] || a - b);
    const spacing = Math.min(0.16, 2.28 / Math.max(1, indices.length - 1));
    const positions = indices.map((i) => Math.max(-1.14, Math.min(1.14, chart.y[i])));
    for (let i = 1; i < positions.length; i++) {
      positions[i] = Math.max(positions[i], positions[i - 1] + spacing);
    }
    if (positions.length) positions[positions.length - 1] = Math.min(1.14, positions.at(-1)!);
    for (let i = positions.length - 2; i >= 0; i--) {
      positions[i] = Math.min(positions[i], positions[i + 1] - spacing);
    }
    indices.forEach((index, i) => {
      const color = circleCategoryColor(circleCategory(chart, index), theme);
      annotations.push({ x: Number(chart.x[index]), y: chart.y[index], xref: "x", yref: "y",
        ax: side * 1.18, ay: positions[i], axref: "x", ayref: "y", text: chart.labels[index],
        xanchor: side < 0 ? "right" : "left", yanchor: "middle", showarrow: true,
        arrowhead: 0, arrowwidth: 0.8, arrowcolor: color, standoff: 5,
        font: { size: 12, color }, bgcolor: "rgba(255,255,255,0.9)", borderpad: 2 });
    });
  }
  return annotations;
}

function chartHeight(chart: ChartSpec): number {
  if (chart.kind === "horizontal_bar" || chart.kind === "stacked_bar") {
    return Math.max(420, chart.x.length * 30 + 160);
  }
  return chart.kind === "correlation_circle" ? 620 : 340;
}

function chartLayout(chart: ChartSpec, theme: ChartTheme): Partial<Layout> {
  const shared: Partial<Layout> = {
    autosize: true,
    height: chartHeight(chart),
    margin: { l: 58, r: 24, t: 52, b: 68 },
    paper_bgcolor: "#ffffff",
    plot_bgcolor: "#ffffff",
    font: { family: "Inter, system-ui, sans-serif", color: theme.ink, size: 15 },
    title: { text: chart.title, x: 0.02, xanchor: "left", font: { size: 15 } },
  };
  if (chart.kind === "horizontal_bar" || chart.kind === "stacked_bar") {
    return { ...shared,
      margin: { l: 270, r: 30, t: 95, b: 65 }, barmode: "stack",
      legend: { orientation: "h", y: 1.025, x: 0, font: { size: 14 } },
      xaxis: { title: { text: chart.x_label }, tickformat: ".0%", gridcolor: theme.line,
        range: chart.kind === "stacked_bar" ? [0, 1] : [0, Math.max(...chart.y) * 1.18 || 1] },
      yaxis: { automargin: true, autorange: "reversed", tickfont: { size: 15 },
        categoryorder: "array", categoryarray: chart.x as string[], tickmode: "array",
        tickvals: chart.x, ticktext: chart.x.map((label) => {
          const text = String(label);
          return text.length > 38 ? `${text.slice(0, 37)}…` : text;
        }) } };
  }
  if (chart.kind === "correlation_circle") {
    return { ...shared, margin: { l: 65, r: 30, t: 100, b: 65 },
      annotations: circleAnnotations(chart, theme),
      legend: { orientation: "h", x: 0, y: 1.12 },
      xaxis: { title: { text: chart.x_label }, range: [-2.2, 2.2], zeroline: true, gridcolor: theme.line },
      yaxis: { title: { text: chart.y_label }, range: [-1.35, 1.35], scaleanchor: "x", scaleratio: 1,
        zeroline: true, gridcolor: theme.line },
      shapes: [{ type: "circle", x0: -1, y0: -1, x1: 1, y1: 1, line: { color: theme.lineStrong, dash: "dot" } }] };
  }
  if (chart.kind === "sample_map") {
    return {
      ...shared,
      margin: { l: 16, r: 16, t: 52, b: 16 },
      geo: {
        fitbounds: false,
        projection: { type: "natural earth", scale: 1 },
        showland: true,
        landcolor: theme.land,
        showocean: true,
        oceancolor: theme.ocean,
        showcountries: true,
        countrycolor: theme.lineStrong,
      },
    };
  }
  return {
    ...shared,
    xaxis: { title: { text: chart.x_label }, gridcolor: theme.line, automargin: true },
    yaxis: { title: { text: chart.y_label }, gridcolor: theme.line, zeroline: false,
      ...(chart.kind === "ordination" ? { scaleanchor: "x", scaleratio: 1 } : {}) },
  };
}
