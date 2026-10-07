import type { AgentResponse } from "@/lib/types";

export type ArtifactChoice = {
  id: string;
  kind: "chart" | "table";
  index: number;
  label: string;
  primary: boolean;
  group?: string;
  resultRole?: "intermediate" | "final";
};

const studyGroups: Record<string, string> = {
  function_ranks: "功能排名与相对转录贡献",
  size_distribution: "功能的粒径分布",
  ocean_distribution: "功能的海区分布",
  target_signals: "DUF285、LHC 的 MetaG/MetaT 与环境关联",
};

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

// 图号和内部任务称呼仅作来源证据，不要求用户了解它们才能读懂标题。
export function artifactTitle(title: string): string {
  return title.replace(/图\d+[a-z]?对照\s*·\s*/g, "")
    .replace(/论文对应项目与完成状态/g, "分析项目与完成状态")
    .replace(/作者参考重算/g, "使用公开参考数据计算")
    .replace(/作者参考排名/g, "公开参考数据排名")
    .replace(/作者参考贡献比例/g, "公开参考数据贡献比例")
    .replace(/论文参考资料/g, "补充参考资料");
}

// 只选择展示，不改变工具结果、科学角色、完成状态或数据范围。
export function artifactChoices(response: AgentResponse, tables: Array<{
  key: string; label: string; core?: boolean; methodVersion?: string; artifactKey?: string;
}>): ArtifactChoice[] {
  const study = "diatom-function-study-v1";
  const steps = Array.isArray(response.result.analysis_steps) ? response.result.analysis_steps.map(record) : [];
  const hasCoreStudy = tables.some((table) => table.core && table.methodVersion === study);
  const charts: ArtifactChoice[] = response.charts.map((chart, index) => {
    const stepId = /^步骤 (\d+) · /.exec(chart.title)?.[1];
    const step = stepId ? steps.find((item) => String(item.step_id) === stepId) : undefined;
    const result = step ? record(step.result) : response.result;
    const isStudy = result.method_version === study;
    const figure = /图(9a|9b|9c|10b|10e)对照/.exec(chart.title)?.[1];
    const legacyKey = figure ? ({ "9a": "function_ranks", "9b": "size_distribution", "9c": "ocean_distribution",
      "10b": "pls_results", "10e": "pls_results" } as Record<string, string>)[figure] : undefined;
    const key = chart.artifact_key ?? (isStudy ? legacyKey : undefined);
    const declared = key ? record(result.artifact_roles)[key] : undefined;
    const role = declared ?? step?.result_role ?? result.result_role ?? chart.result_role ?? "final";
    const groupKey = key === "pls_results" ? "target_signals" : key;
    return { id: `chart-${index}`, kind: "chart", index, label: artifactTitle(chart.title),
      resultRole: role === "intermediate" ? "intermediate" : "final",
      primary: role === "final" && (!hasCoreStudy || isStudy),
      group: isStudy && groupKey ? studyGroups[groupKey] : undefined };
  });
  return [...charts, ...tables.map((table, index): ArtifactChoice => ({
    id: `table-${table.key}`, kind: "table", index, label: table.label,
    primary: Boolean(table.core) && (!hasCoreStudy || table.methodVersion === study),
    group: table.methodVersion === study ? studyGroups[table.artifactKey ?? ""] : undefined,
  }))];
}
