export type DataTag = { label: string; value: string; detail?: string };

// 标签仅概括已有条件；长名单保留完整提示，未记录的范围不推断为全量。
export function summarizeDataFilters(name: string, filters: Record<string, unknown>): DataTag[] {
  const record = (value: unknown): Record<string, unknown> => (
    value !== null && typeof value === "object" && !Array.isArray(value)
      ? value as Record<string, unknown> : {}
  );
  const selection = record(filters.selection);
  const query = { ...record(filters.context), ...filters, ...record(filters.query) };
  const tags: DataTag[] = [];
  const add = (label: string, value: unknown) => {
    if ((typeof value === "string" && value.trim()) || typeof value === "number") {
      tags.push({ label, value: String(value) });
    }
  };
  add("版本", query.dataset_version);
  const taxon = query.taxon ?? selection.taxon;
  add("类群", taxon === "Bacillariophyta" ? "硅藻" : taxon);
  if (taxon === "Bacillariophyta") tags[tags.length - 1].detail = "Bacillariophyta";
  add("信号层", query.assay === "MetaG" ? "MetaG（DNA）" : query.assay === "MetaT" ? "MetaT（RNA）" : query.assay);
  if (name === "compare_function_signals") add("比较", "MetaG ↔ MetaT");
  add("标记", query.marker);
  for (const [key, label] of [
    ["sample_name", "样本"], ["sample_id", "样本"], ["sample_id_pangaea", "样本"],
    ["pfam_accession", "目标 Pfam"], ["ocean_region_contains", "海区名称包含"],
    ["sample_name_contains", "样本名称包含"], ["environment_variable", "环境变量"],
  ]) add(label, query[key]);
  for (const [key, label] of [
    ["sample_names", "指定样本"], ["sample_ids", "指定样本"], ["sampling_keys", "采样编码"],
    ["pfam_accessions", "目标 Pfam"], ["gene_ids", "geneID"], ["depths", "水层"], ["size_fractions", "粒径组"],
  ]) {
    const value = query[key];
    if (Array.isArray(value) && value.length) {
      const items = value.map(String);
      tags.push({ label, value: items.length > 2 ? `${items.slice(0, 2).join("、")} 等 ${items.length} 项` : items.join("、"), detail: items.join("、") });
    }
  }
  if (name === "function_atlas" && query.sample_names === null) add("范围", "全部可用样本");
  if (name === "compare_function_signals" && query.sampling_keys === null) add("范围", "全部合格采样编码");
  if (["function_atlas", "function_profile"].includes(name) && typeof query.top_n === "number") add("排名", `Top ${query.top_n} Pfam`);
  if (typeof query.max_i_evalue === "number") add("注释筛选", `i-Evalue ≤ ${query.max_i_evalue}`);
  if (typeof query.temperature_min === "number") add("温度", `≥ ${query.temperature_min} °C`);
  if (typeof query.temperature_max === "number") add("温度", `≤ ${query.temperature_max} °C`);
  if (typeof query.polar === "boolean") add("区域", query.polar ? "极地" : "非极地");
  const groups: Record<string, string> = { station: "站位", polar: "极地/非极地", ocean_region: "海区", depth: "水层", size_fraction: "粒径组" };
  if (typeof query.group_by === "string") add("分组", groups[query.group_by] ?? query.group_by);
  for (const [key, label] of [["limit", "本页上限"], ["asv_limit", "分类单元上限"], ["sample_limit", "样本上限"], ["point_limit", "数据点上限"]]) {
    if (typeof query[key] === "number") add(label, `${query[key]} 条`);
  }
  return tags;
}

export function dataSourceLabel(name: string): string {
  return ({
    context_general: "context_general.tsv", context_stat: "context_stat.tsv",
    "18s_v4": "TARA-Oceans_18S-V4_dada2_table.tsv",
    "18s_v9": "TARA-Oceans_18S-V9_dada2_table.tsv",
    matou_taxonomy: "MATOU-v1.5.taxonomy.tsv.gz", matou_pfam: "MATOU-v1.5.pfam.gz",
    matou_metag: "MATOU-v1.5.metaG.occurrences.gz", matou_metat: "MATOU-v1.5.metaT.occurrences.gz",
    matou_fasta: "MATOU-v1.5.fna.gz",
  } as Record<string, string>)[name] ?? name;
}
