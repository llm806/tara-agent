"use client";

import { ChevronDown, Table2, Download } from "lucide-react";
import { type ReactNode, useState } from "react";

import { downloadCsv, downloadFasta } from "@/lib/downloads";
import { useResponseCard } from "@/components/response-card-group";
import { ArtifactHeading } from "@/components/artifact-heading";
import { artifactSummary } from "@/lib/artifact-summary";
import { artifactTitle } from "@/lib/artifact-presentation";

export type ResultRows = {
  key: string;
  label: string;
  rows: Array<Record<string, unknown>>;
  scope: string;
  sources: string[];
  core?: boolean;
  methodVersion?: string;
  artifactKey?: string;
};

export function ResultTableSection({ label, rows, scope, sources }: Omit<ResultRows, "key">) {
  const [open, setOpen] = useResponseCard();
  const [page, setPage] = useState(0);
  const pageSize = 100;
  const currentPage = Math.min(page, Math.max(0, Math.ceil(rows.length / pageSize) - 1));
  const visibleRows = rows.slice(currentPage * pageSize, (currentPage + 1) * pageSize);
  const columns = Object.keys(rows[0]);

  return (
    <details
      className="response-section result-panel"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <Table2 size={18} />
        <ArtifactHeading title={label} scope={scope} sources={sources} />
        <small>表 · {rows.length} 行</small>
        <ChevronDown size={16} />
      </summary>
      <div className="section-body">
        <div className="artifact-toolbar">
          {rows.every((row) => typeof row.header === "string" && typeof row.sequence === "string") && (
            <button type="button" className="artifact-download"
              onClick={() => downloadFasta(rows, label)} aria-label="下载当前页核酸序列 FASTA">
              <Download size={14} aria-hidden="true" />下载当前页 FASTA
            </button>
          )}
          <button
            type="button"
            className="artifact-download"
            onClick={() => downloadCsv(rows, label)}
            aria-label={`下载${label}`}
          >
            <Download size={14} aria-hidden="true" />
            下载 CSV
          </button>
        </div>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                {columns.map((column) => (
                  <th key={column}>{humanize(column)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {/* 结果顺序固定，以全表行位置标识，避免同一 Pfam 或样本的多条记录重名。 */}
              {visibleRows.map((row, index) => (
                <tr key={currentPage * pageSize + index}>
                  {columns.map((column) => (
                    <td key={column} data-column={column} title={typeof row[column] === "string" ? row[column] : undefined}>{column === "sequence" && typeof row[column] === "string"
                      ? <details><summary>{row[column].length} 个碱基</summary><pre>{row[column]}</pre></details>
                      : formatCell(column, row[column])}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {rows.length > pageSize && (
          <div className="artifact-toolbar">
            <button type="button" className="artifact-download" disabled={currentPage === 0}
              onClick={() => setPage(currentPage - 1)}>上一页</button>
            <span>第 {currentPage + 1} / {Math.ceil(rows.length / pageSize)} 页 · CSV 包含全部 {rows.length} 行</span>
            <button type="button" className="artifact-download" disabled={(currentPage + 1) * pageSize >= rows.length}
              onClick={() => setPage(currentPage + 1)}>下一页</button>
          </div>
        )}
      </div>
    </details>
  );
}

const resultLists = [
  ["items", "样本"],
  ["asvs", "分类单元"],
  ["sample_occurrences", "样本出现记录"],
  ["observations", "观测记录"],
  ["groups", "分组摘要"],
  ["station_leaders", "均值与最大值最高站点（并列取代表）"],
  ["composition", "样本内分类组成"],
  ["composition_summaries", "样本组成与优势类群摘要"],
  ["points", "关联数据点"],
  ["ranks", "总体 Pfam 排名"],
  ["summaries", "功能比较摘要"],
  ["sample_summaries", "样本分母与记录覆盖"],
  ["excluded_samples", "未进入比较的样本"],
  ["sequences", "核酸序列"],
  ["sections", "论文对应项目与完成状态"],
  ["function_ranks", "图9a对照 · 功能排名与相对转录贡献"],
  ["size_distribution", "图9b对照 · 各功能粒径分配比例"],
  ["ocean_distribution", "图9c对照 · 各功能海区分配比例"],
  ["target_signals", "图10对照 · DUF285、LHC及亚家族MetaG/MetaT相对信号"],
  ["associations", "环境关联统计（样本数、Spearman、p值及BH校正）"],
  ["size_composition", "四粒径相对丰度分布"],
] as const;

export function findTables(result: Record<string, unknown>, sources: string[] = []): ResultRows[] {
  if (result.workflow === "multi_step" && Array.isArray(result.analysis_steps)) {
    return result.analysis_steps.flatMap((step, index) => {
      if (!isRecord(step) || !isRecord(step.result)) return [];
      return findTables({ ...step.result, result_role: step.result_role ?? "intermediate" }).map((table) => ({
        ...table,
        key: `step-${index}-${table.key}`,
        label: `步骤 ${step.step_id ?? index + 1} · ${table.label}`,
      }));
    });
  }
  const tables: ResultRows[] = [];
  const summary = artifactSummary(result, sources);
  const includedKeys = new Set<string>();
  const present = (rows: ResultRows[]) => rows.map((table) => {
    const declared = isRecord(result.artifact_roles) ? result.artifact_roles[table.key] : undefined;
    const final = declared ? declared === "final" : result.result_role !== "intermediate";
    // 完整输入、模型坐标与覆盖核查保留在按需列表，不作为默认科研产物。
    const primaryKeys = result.method_version === "diatom-function-study-v1"
      ? ["function_ranks", "size_distribution", "ocean_distribution", "target_signals"]
      : ["items", "asvs", "observations", "groups", "composition", "ranks", "summaries",
        "associations", "size_composition", "sequences", "sample"];
    const auxiliary = ["sections", "excluded_samples", "sample_summaries", "mapping-coverage", "pls-models"]
      .includes(table.key) || /-(coordinates|scores|input_rows|explained_variance|stress|distance_rows|environment_fit)$/.test(table.key);
    return { ...table, label: artifactTitle(table.label), artifactKey: table.key,
      methodVersion: typeof result.method_version === "string" ? result.method_version : undefined,
      core: final && !auxiliary && (primaryKeys.includes(table.key) || declared === "final") };
  });
  const role = (key: string) => {
    const declared = isRecord(result.artifact_roles) ? result.artifact_roles[key] : undefined;
    return (declared ?? result.result_role) === "intermediate" ? "【中间步骤结果】" : "【最终结果】";
  };
  for (const [key, label] of resultLists) {
    const value = result[key];
    if (Array.isArray(value) && value.length > 0 && value.every(isRecord)) {
      const observationLabel = result.method_version === "candidate-function-signal-v1"
        ? "候选 Pfam 信号与相对份额"
        : "ranks" in result ? "逐样本 Pfam 相对贡献"
          : "matching_asv_count" in result ? "样本类群相对丰度"
            : "shannon_index" in value[0] ? "样本 ASV 丰富度与 Shannon 多样性" : label;
      const tableLabel = key === "observations" ? observationLabel
        : key === "groups" && result.group_by === "station" ? "站点相对丰度均值与最大值排名"
          : key === "points" && "environment_variable" in result ? "环境变量与类群相对丰度对应数据"
            : key === "points" && "summaries" in result ? "MetaG/MetaT 对应功能份额与差值"
              : label;
      const title = key === "groups" && result.method_version === "community-study-v1" ? "群落相对丰度、Shannon与exp(Shannon)汇总" : tableLabel;
      tables.push({ key, label: `${role(key)}${title}表`, rows: value, ...summary });
      includedKeys.add(key);
    }
  }
  for (const [key, value] of Object.entries(result)) {
    if (
      !includedKeys.has(key)
      && key !== "pls_results"
      && key !== "ordinations"
      && Array.isArray(value)
      && value.length > 0
      && value.every(isRecord)
    ) {
      tables.push({ key, label: `${role(key)}${humanize(key)}表`, rows: value, ...summary });
    }
  }
  if (Array.isArray(result.pls_results)) {
    const models = result.pls_results.filter(isRecord).map((item) => ({
      figure: item.figure, target: item.target, status: item.status, sample_count: item.sample_count,
      excluded_rows: item.excluded_rows, constant_variables: item.constant_variables,
      reason: item.reason, method: item.method,
    }));
    if (models.length) tables.push({ key: "pls-models", label: `${role("sections")}PLS计算条件与完成状态表`, rows: models, ...summary });
    for (const item of result.pls_results) {
      if (!isRecord(item)) continue;
      for (const [key, label] of [["coordinates", "PLS变量相关坐标"], ["scores", "PLS样本坐标"],
        ["explained_variance", "PLS成分解释比例"], ["input_rows", "PLS完整输入与缺失值核查"]]) {
        const rows = item[key];
        if (Array.isArray(rows) && rows.length && rows.every(isRecord)) {
          tables.push({ key: `${item.target}-${key}`, label: `${role(key === "input_rows" || key === "scores" ? "sections" : "pls_results")}图${item.figure}对照 · ${item.target} · ${label}表`, rows, ...summary });
        }
      }
    }
  }
  if (Array.isArray(result.ordinations)) {
    for (const item of result.ordinations) {
      if (!isRecord(item)) continue;
      for (const [key, label] of [["scores", "NMDS样本坐标"], ["environment_fit", "NMDS环境拟合与BH校正"], ["distance_rows", "Bray–Curtis距离矩阵"]]) {
        const rows = item[key];
        if (Array.isArray(rows) && rows.length && rows.every(isRecord)) {
          tables.push({ key: `${item.marker}-${key}`, label: `${role(key === "distance_rows" ? "sections" : "ordinations")}${item.marker} · ${label}表`, rows, ...summary });
        }
      }
      tables.push({ key: `${item.marker}-stress`, label: `${role("ordinations")}${item.marker} · NMDS stress与收敛状态表`,
        rows: [{ marker: item.marker, status: item.status, stress: item.stress, converged: item.converged,
          sample_count: item.sample_count, starts: item.starts, seed: item.seed, method: item.method }], ...summary });
    }
  }
  if (isRecord(result.mapping_coverage)) tables.push({ key: "mapping-coverage", label: `${role("mapping_coverage")}跨库映射覆盖表`, rows: [result.mapping_coverage], ...summary });
  if (isRecord(result.query) && result.method_version === "diatom-function-study-v1") {
    const source = result.query.source === "paper_reference" ? "使用公开参考数据计算" : "当前数据";
    const priority = ["sections", "function_ranks", "size_distribution", "ocean_distribution", "target_signals", "pls-models"];
    tables.sort((a, b) => {
      const order = (key: string) => priority.includes(key) ? priority.indexOf(key) : priority.length;
      return order(a.key) - order(b.key);
    });
    return present(tables).map((table) => ({ ...table, label: `${source} · ${table.label}` }));
  }
  if (isRecord(result.sample)) {
    tables.push({ key: "sample", label: `${role("sample")}样本采集与环境信息表`, rows: [result.sample], ...summary });
  }
  return present(tables);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function humanize(value: string): string {
  const labels: Record<string, string> = {
    pfam_accession: "Pfam 编号", value_sum: "已提供信号之和",
    fraction_of_observed_taxon_signal: "已提供类群记录内份额",
    function_id: "功能编号", function_name: "功能名称", member_accessions: "所含Pfam",
    relative_contribution: "相对转录贡献（分母见分析范围）", mean_sample_relative_signal: "样本组平均相对信号",
    reference_rank: "作者参考排名", reference_contribution: "作者参考贡献比例",
    observed_groups: "有记录的样本组数", allocation_fraction: "组内分配比例",
    mapped_signal_fraction: "成功映射的信号比例", group: "粒径/海区", target: "目标功能",
    assay: "DNA/RNA实验", depth: "水层", size_fraction: "粒径组（μm）",
    relative_signal: "相对信号", numerator: "功能信号", denominator: "分母信号",
    denominator_scope: "分母范围",
    figure: "来源图表编号", output: "输出项目", status: "完成状态", reason: "原因",
    excluded_rows: "因缺失排除的采样组", constant_variables: "常量变量", method: "计算方法",
    variable: "变量", role: "变量类型", component_1: "成分1", component_2: "成分2",
    component: "成分", x_fraction: "环境解释比例", y_fraction: "响应解释比例",
    sampling_group: "站点:水层:粒径组", Temperature: "温度", "Depth.nominal": "采样深度",
    "Ammonium.5m": "铵盐", "NO2.5m": "亚硝酸盐", "NO3.5m": "硝酸盐",
    "Iron.5m": "铁", Si: "硅", PO4: "磷酸盐", ChlorophyllA: "叶绿素a", abslat: "绝对纬度",
    station: "站点", mean_rank: "均值排名", max_rank: "最大值排名",
    mean_relative_abundance: "平均相对丰度", max_relative_abundance: "最大相对丰度",
    sample_count: "样本数", valid_sample_count: "有效样本数", sample_ids: "所含样本",
    sample_id: "样本", rank: "排名", taxon_label: "分类名称", classification_status: "鉴定状态",
    read_count: "测序读数", relative_abundance: "全样本相对丰度",
    fraction_within_selected_taxon: "所选类群内占比", cumulative_fraction: "类群内累计占比",
    asv_count: "ASV 数", taxonomy: "分类路径", dominant_taxon: "占比最高的分类",
    dominant_fraction: "最高分类的类群内占比", total_groups: "分类总数", returned_groups: "本次返回数",
    selected_taxon_read_count: "所选类群总读数", sample_total_read_count: "样本总读数",
    unresolved_read_count: "未鉴定到该层级的读数",
    environment_value: "环境变量值（单位见分析范围）", target_signal: "目标Pfam信号（口径见分析范围）",
    sample_names: "MATOU原样本", mapping_evidence: "样本对应证据",
    marker: "18S标记", shannon_index: "Shannon指数", exp_shannon: "exp(Shannon)有效ASV数",
    taxon_read_count: "筛选后类群读数", eukaryotic_read_count: "全部真核读数分母",
    observed_asv_richness: "检出ASV数", retained_asv_count: "筛选保留ASV数",
    rho: "Spearman ρ", p_value: "置换p值", p_adjusted: "BH校正q值",
    stress: "Stress1", converged: "是否收敛", r_squared: "环境拟合R²",
    axis_1: "环境向量轴1", axis_2: "环境向量轴2", response: "响应指标",
  };
  if (labels[value]) return labels[value];
  return value.replaceAll("_", " ");
}

function formatCell(column: string, value: unknown): ReactNode {
  if (column === "member_accessions" && Array.isArray(value)) {
    return <details><summary>{value.length} 个 Pfam</summary><pre>{value.join(" · ")}</pre></details>;
  }
  if (typeof value === "number" && ["relative_contribution", "mean_sample_relative_signal",
    "reference_contribution", "allocation_fraction", "mapped_signal_fraction", "relative_signal",
    "x_fraction", "y_fraction"].includes(column)) {
    return `${(value * 100).toPrecision(5)}%`;
  }
  return formatValue(value);
}

function formatValue(value: unknown): ReactNode {
  if (value === null || value === undefined) {
    return <span className="null-value">NA</span>;
  }
  if (typeof value === "number") {
    return Number.isInteger(value) ? value.toLocaleString() : value.toPrecision(5);
  }
  if (typeof value === "boolean") {
    return value ? "Yes" : "No";
  }
  if (value === "completed") return "已完成";
  if (value === "blocked") return "未完成";
  if (value === "partial") return "部分完成";
  if (value === "PLS2_NIPALS_2_components_scaled_sample_sd_no_inference") return "PLS · 2成分 · 标准化 · 描述性关联";
  if (value === "all_taxon_gene_signal") return "该采样组硅藻全部基因信号";
  if (value === "retained_pfam_signal") return "该采样组保留 Pfam 信号";
  if (value === "all_classified_LHC_signal_including_LHCr9Homolog") return "全部已分类 LHC 信号（含 LHCr9Homolog）";
  if (value === "observed") return "有观测记录";
  if (value === "missing") return "缺少记录";
  if (value === "paper_zero_fill") return "作者脚本补零";
  if (value === "environment") return "环境变量";
  if (value === "response") return "功能信号";
  if (value === "assigned") return "原始注释已命名";
  if (value === "unresolved") return "未鉴定到所选层级";
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}
