import type { AgentResponse, ChartSpec } from "@/lib/types";
import { dataSourceLabel } from "@/lib/trace-data-summary";

export function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

export function artifactSummary(result: Record<string, unknown>, fallbackSources: string[] = []) {
  const context = asRecord(result.context);
  const provenance = asRecord(asRecord(result.metadata).provenance);
  const filters = asRecord(provenance.filters);
  const query = { ...asRecord(filters.query), ...asRecord(result.query) };
  const selection = asRecord(filters.selection);
  const parts: string[] = [];
  const add = (value: unknown) => {
    if (typeof value === "string" && value.trim()) parts.push(value);
  };
  add(context.assay ?? result.assay ?? query.assay);
  add(context.taxon ?? result.taxon ?? query.taxon ?? selection.taxon);
  const marker = result.marker ?? query.marker ?? provenance.marker;
  if (typeof marker === "string") add(`18S ${marker.toUpperCase()}`);
  add(context.sample_name ?? query.sample_name ?? asRecord(result.sample).sample_id_pangaea);
  for (const key of ["sample_ids", "sample_names", "sampling_keys"]) {
    const values = query[key];
    if (Array.isArray(values) && values.length) {
      add(values.length <= 2 ? values.join("、") : `指定 ${values.length} 个${key === "sampling_keys" ? "采样编码" : "样本"}`);
    }
  }
  add(query.ocean_region);
  add(result.environment_variable ?? query.environment_variable);
  const groups: Record<string, string> = { station: "按站点汇总", ocean_region: "按海区分组", depth: "按水层分组", size_fraction: "按粒径分组", polar: "按极地/非极地分组" };
  const groupBy = result.group_by ?? query.group_by;
  if (typeof groupBy === "string") add(groups[groupBy] ?? groupBy);
  if (query.source === "paper_reference") add("使用公开参考数据计算");
  if (query.source === "current_data") add("当前数据重算");
  add(context.dataset_version ?? result.dataset_version ?? filters.dataset_version);
  if (typeof provenance.sample_count === "number") add(`参与样本 ${provenance.sample_count}`);
  if (typeof query.max_i_evalue === "number") add(`候选 i-Evalue ≤ ${query.max_i_evalue}`);
  const declared = Array.isArray(provenance.source_datasets)
    ? provenance.source_datasets.filter((value): value is string => typeof value === "string") : [];
  const sources = [...new Set((declared.length ? declared : fallbackSources).map((source) => {
    // 仅对已知MATOU版本使用原文件名，不把未来版本误标成v1.5。
    const version = context.dataset_version ?? result.dataset_version ?? filters.dataset_version;
    if (source.startsWith("matou_") && version && version !== "MATOU-v1.5") return source;
    return dataSourceLabel(source);
  }))];
  return { scope: [...new Set(parts)].join(" · "), sources };
}

export const chartKindLabels: Record<ChartSpec["kind"], string> = {
  sample_map: "样本分布地图", bar: "柱状图", horizontal_bar: "条形图",
  stacked_bar: "堆叠比例图", scatter: "散点图", correlation_circle: "PLS相关圆图",
  ordination: "NMDS排序图", heatmap: "相关性热图",
};

export function chartSummary(chart: ChartSpec, response: AgentResponse) {
  if (response.result.workflow !== "multi_step") return artifactSummary(response.result, response.sources);
  // 多步骤图标题由后端绑定真实步骤编号；不把其他步骤的数据来源套到本图。
  const stepId = /^步骤 (\d+) · /.exec(chart.title)?.[1];
  const steps = response.result.analysis_steps;
  const step = Array.isArray(steps)
    ? steps.map(asRecord).find((item) => String(item.step_id) === stepId) : undefined;
  return artifactSummary(asRecord(step?.result));
}
