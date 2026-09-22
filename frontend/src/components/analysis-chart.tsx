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
      <div ref={chartRef} className="analysis-chart" aria-label={chart.title} />
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
  if (chart.kind === "sample_map") {
    return [
      {
        type: "scattergeo",
        mode: "markers",
        lon: chart.x as number[],
        lat: chart.y,
        text: chart.labels,
        hovertemplate: "%{text}<br>经度 %{lon:.2f}<br>纬度 %{lat:.2f}<extra></extra>",
        marker: { color: theme.accent, size: 8, line: { color: "#ffffff", width: 1 } },
      },
    ];
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

function chartLayout(chart: ChartSpec, theme: ChartTheme): Partial<Layout> {
  const shared: Partial<Layout> = {
    autosize: true,
    height: 340,
    margin: { l: 58, r: 24, t: 52, b: 68 },
    paper_bgcolor: "#ffffff",
    plot_bgcolor: "#ffffff",
    font: { family: "Inter, system-ui, sans-serif", color: theme.ink, size: 12 },
    title: { text: chart.title, x: 0.02, xanchor: "left", font: { size: 15 } },
  };
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
    yaxis: { title: { text: chart.y_label }, gridcolor: theme.line, zeroline: false },
  };
}
