"""可审核的硅藻功能分析；区分当前候选数据和作者公开汇总表的计算口径。"""

import math
from collections import defaultdict

import polars as pl

from tara_agent.analysis.function_atlas import MatouAtlasService
from tara_agent.analysis.function_pls import fit_pls
from tara_agent.analysis.function_study_models import (
    FunctionStudyQuery,
    FunctionStudyResult,
    PLSResult,
)
from tara_agent.data.function_reference import REFERENCE_URL, FunctionReference
from tara_agent.data.matou_study import SAMPLE_CODE
from tara_agent.domain.contracts import DataProvenance, ResultMetadata, ResultWarning
from tara_agent.observability.contracts import ObservationKind, ObservationUpdate
from tara_agent.observability.execution import observe

FRACTIONS = {
    "GGMM": "0.8-5/2000",
    "GGQQ": "0.8-5/2000",
    "GGZZ": "0.8-5/2000",
    "MMQQ": "3/5-20",
    "KKQQ": "3/5-20",
    "KKZZ": "3/5-20",
    "QQSS": "20-180",
    "QQRR": "20-180",
    "SSUU": "180-2000",
}
SIZES = ["0.8-5/2000", "3/5-20", "20-180", "180-2000"]
OCEANS = ["MS", "RS", "IO", "SAO", "SO", "SPO", "NPO", "NAO", "AO"]
EXCLUDED = {
    "PF02100",
    "PF00992",
    "PF03164",
    "PF03490",
    "PF03986",
    "PF08802",
    "PF10381",
    "PF10914",
    "PF14878",
    "PF01576",
    "PF16077",
    "PF17088",
    "PF00510",
    "PF00861",
}
# 顺序遵循作者图9脚本；后续描述匹配可覆盖前面的功能命名。
EXPLICIT_GROUPS = [
    ("HMG-box domain", "PF00505 PF09011"),
    ("Pentatricopeptide repeat", "PF13812 PF01535 PF12854 PF13041"),
    ("S-adenosyl-L-homocysteine hydrolase", "PF00670 PF05221"),
    ("Pyridine nucleotide-disulphide oxidoreductase", "PF02852 PF07992 PF00070 PF13738"),
    ("Kelch motif", "PF01344 PF13418 PF13964 PF07646 PF13854 PF07707"),
    ("Ankyrin repeats", "PF00023 PF12796 PF13606 PF13637 PF13857"),
    ("Glyceraldehyde 3-phosphate dehydrogenase", "PF00044 PF02800"),
    ("Histidine kinase-, DNA gyrase B-, and HSP90-like ATPase", "PF02518 PF13589"),
    ("Elongation factor Tu", "PF00009 PF03143 PF03144 PF14578"),
    ("Elongation factor G", "PF00679 PF03764 PF14492 PF07299"),
    ("ABC transporter", "PF00664 PF00005"),
]
PATTERN_GROUPS = [
    ("Tetratricopeptide repeat", "tetratricopeptide"),
    ("EF hand domain", "ef-hand"),
    ("EF hand domain", "ef hand"),
    ("Helicase domains", "helicase"),
    ("Methyltransferase domain", "methyltransferase domain"),
    ("Ribosomal proteins", "ribosom"),
    ("Ubiquitin domains", "ubiquitin"),
    ("Glycosyl hydrolase family", "glycosyl hydrolase"),
]
PREDICTORS = [
    "size",
    "layer",
    "Depth.nominal",
    "Temperature",
    "Ammonium.5m",
    "NO2.5m",
    "NO3.5m",
    "Iron.5m",
    "Si",
    "PO4",
    "ChlorophyllA",
    "abslat",
]
SUBFAMILIES = ["LHCr", "LHCq", "LHCz", "LHCf", "LHCx"]


def function_mapping(pfam):
    result = {}
    seen = set()
    for row in pfam.iter_rows(named=True):
        acc, desc = row["pfam_accession"], row["description"]
        if not isinstance(desc, str) or not isinstance(acc, str) or acc in result:
            raise ValueError("Pfam参考名称有缺失或重复")
        original = desc
        if desc in seen:
            desc = row["name"] + " | " + desc
        seen.add(original)
        lower = desc.lower()
        excluded = acc in EXCLUDED or (
            "ribosom" in lower and ("mitochondrial" in lower or "plastid" in lower)
        )
        group = None
        if acc == "PF00313":
            desc = "Cold-shock DNA-binding domain"
        for name, members in EXPLICIT_GROUPS:
            if acc in members.split():
                group = name
        for name, pattern in PATTERN_GROUPS:
            if pattern in lower:
                group = name
        result[acc] = dict(function_id=group or acc, function_name=group or desc, excluded=excluded)
    return result


def sample_scope(names, assay, *, reference):
    accepted, excluded = {}, []
    for name in names:
        match = SAMPLE_CODE.fullmatch(name)
        if match is None:
            excluded.append({"sample_name": name, "reason": "无法解析作者采样编码"})
            continue
        r = match.groupdict()
        protocols = {"11", "12"} if assay == "MetaG" else {"13", "14", "15"}
        if not reference:
            protocols = {"11"} if assay == "MetaG" else {"14"}
        if r["depth"] not in {"SUR", "DCM"} or r["protocol"] not in protocols:
            excluded.append({"sample_name": name, "reason": "水层或实验协议不在当前方法范围"})
            continue
        accepted[name] = {
            "station": r["station"],
            "depth": r["depth"],
            "size_fraction": FRACTIONS.get(r["fraction"], r["fraction"]),
            "protocol": r["protocol"],
        }
    return accepted, excluded


def relative_profiles(profiles, scope, *, totals=None, excluded_families=(), protocol=True):
    """先合并重复/等价粒径，再归一化；当前数据分母包括未注释基因。"""
    keys = ["station", "depth", "size_fraction"] + (["protocol"] if protocol else [])
    grouped = defaultdict(float)
    denominators = defaultdict(float)
    for r in profiles.iter_rows(named=True):
        s = scope.get(r["sample_name"])
        if s is None or r["pfam_accession"] in excluded_families:
            continue
        key = tuple(s[k] for k in keys)
        grouped[(*key, r["pfam_accession"])] += r["value_sum"]
        if totals is None:
            denominators[key] += r["value_sum"]
    if totals is not None:
        for r in totals.iter_rows(named=True):
            s = scope.get(r["sample_name"])
            if s is not None:
                denominators[tuple(s[k] for k in keys)] += r["taxon_value_sum"]
    if not all(math.isfinite(v) and v >= 0 for v in [*grouped.values(), *denominators.values()]):
        raise ValueError("功能分组求和溢出或存在非法信号")
    rows = []
    for key, value in grouped.items():
        denominator = denominators.get(key[:-1], 0)
        if denominator > 0:
            rows.append(
                {
                    **dict(zip(keys, key[:-1], strict=True)),
                    "pfam_accession": key[-1],
                    "value_sum": value,
                    "relative_signal": value / denominator,
                    "denominator": denominator,
                }
            )
    return rows, denominators


def ranking_and_distribution(rows, mapping, oceans, top_n, *, sample_group_count=None):
    grouped = defaultdict(float)
    members = defaultdict(set)
    names = {}
    for r in rows:
        acc = r["pfam_accession"]
        m = mapping.get(acc)
        if m is None or m["excluded"]:
            continue
        fid = m["function_id"]
        key = (fid, r["station"], r["depth"], r["size_fraction"], r["protocol"])
        grouped[key] += r["relative_signal"]
        members[fid].add(acc)
        names[fid] = m["function_name"]
    weights = defaultdict(float)
    counts = defaultdict(int)
    group_count = (
        sample_group_count if sample_group_count is not None else len({k[1:] for k in grouped})
    )
    for k, v in grouped.items():
        weights[k[0]] += v
        counts[k[0]] += 1
    order = sorted(weights, key=lambda k: (-weights[k], k))[:top_n]
    total_weight = math.fsum(weights.values())
    # 作者图9再次除以所有保留Pfam的总相对信号；保留精确分母供核对。
    ranks = [
        {
            "rank": i + 1,
            "function_id": fid,
            "function_name": names[fid],
            "relative_contribution": weights[fid] / total_weight if total_weight else 0,
            "mean_sample_relative_signal": weights[fid] / group_count if group_count else 0,
            "member_accessions": sorted(members[fid]),
            "observed_groups": counts[fid],
        }
        for i, fid in enumerate(order)
    ]
    distributions = []
    for axis, levels in (("size", SIZES), ("ocean", OCEANS)):
        records = []
        sums_by_function, counts_by_function = defaultdict(float), defaultdict(int)
        selected = set(order)
        for key, value in grouped.items():
            if key[0] in selected:
                label = key[3] if axis == "size" else oceans.get(key[1])
                if label in levels:
                    sums_by_function[(key[0], label)] += value
                    counts_by_function[(key[0], label)] += 1
        for fid in order:
            sums = {label: sums_by_function[(fid, label)] for label in levels}
            ns = {label: counts_by_function[(fid, label)] for label in levels}
            mapped = math.fsum(sums.values())
            for label in levels:
                records.append(
                    {
                        "function_id": fid,
                        "function_name": names[fid],
                        "group": label,
                        "allocation_fraction": sums[label] / mapped if mapped else None,
                        "observed_groups": ns[label],
                        "mapped_signal_fraction": min(1.0, mapped / weights[fid])
                        if weights[fid]
                        else None,
                    }
                )
        distributions.append(records)
    return ranks, *distributions, group_count


class FunctionStudyService:
    def __init__(self, reader=None, reference=None):
        self.reader = reader
        self.reference = reference

    def _reference(self):
        if self.reference is None:
            if (
                self.reader is None
                or not (self.reader.directory / "paper_task2/manifest.json").is_file()
            ):
                raise ValueError(
                    "未准备论文功能参考资料；运行 deploy/prepare_function_reference.py"
                )
            self.reference = FunctionReference(self.reader.directory / "paper_task2")
        return self.reference

    def _profiles(self, assay, source):
        if source == "paper_reference":
            f = self._reference().frame(assay)
            f = f.select(
                pl.col("sample").alias("sample_name"),
                pl.col("pfamAcc").alias("pfam_accession"),
                pl.col("rpkm").alias("value_sum"),
            )
            totals = None
            names = f["sample_name"].unique().to_list()
        else:
            if self.reader is None:
                raise ValueError("未配置当前MATOU功能数据")
            atlas = MatouAtlasService(self.reader)
            if self.reader.manifest.selection["taxon"].casefold() != "bacillariophyta":
                raise ValueError("论文任务二仅适用于已准备的Bacillariophyta范围")
            names = [s.sample_name for s in self.reader.list_samples(assay)]
            totals, f = atlas._frames(assay, names, atlas._mapping(None), None)
        if (
            f.select(pl.struct("sample_name", "pfam_accession").n_unique()).item() != f.height
            or f.filter(~pl.col("value_sum").is_finite() | (pl.col("value_sum") < 0)).height
        ):
            raise ValueError("功能汇总表存在重复键或非法值")
        scope, excluded = sample_scope(names, assay, reference=source == "paper_reference")
        return f, totals, scope, excluded

    def analyze(self, query: FunctionStudyQuery) -> FunctionStudyResult:
        if query.source == "paper_reference" and query.normalization == "taxon_total":
            raise ValueError(
                "作者Pfam汇总表不含全部硅藻基因分母；参考重算须明确normalization=author_script，不能冒充论文图注总量口径"
            )
        with observe(
            "硅藻功能谱与论文对照分析",
            ObservationKind.SERVICE,
            ObservationUpdate(input_data=query.model_dump()),
        ) as operation:
            reference = self._reference()
            mapping = function_mapping(reference.frame("pfam"))
            ocean_frame = reference.frame("oceans")
            ocean_labels = defaultdict(set)
            for r in ocean_frame.iter_rows(named=True):
                ocean_labels[str(r["station"])].add(r["ocean"])
            ambiguous_oceans = sorted(k for k, v in ocean_labels.items() if len(v) != 1)
            oceans = {k: next(iter(v)) for k, v in ocean_labels.items() if len(v) == 1}
            f, totals, scope, excluded = self._profiles("MetaT", query.source)
            removed = {acc for acc, m in mapping.items() if m["excluded"]}
            unknown = set(f["pfam_accession"]) - mapping.keys()
            # 作者参考表缺少名称时R的aggregate会丢弃该行；当前结果显式记录数量。
            rows, denominators = relative_profiles(
                f, scope, totals=totals, excluded_families=removed | unknown
            )
            ranks, size, ocean, n = ranking_and_distribution(
                rows,
                mapping,
                oceans,
                query.top_n,
                sample_group_count=sum(value > 0 for value in denominators.values()),
            )
            if query.normalization == "taxon_total":
                for r in ranks:
                    r["relative_contribution"] = r["mean_sample_relative_signal"]
            if query.source == "current_data":
                rf, _, rs, _ = self._profiles("MetaT", "paper_reference")
                rr, _ = relative_profiles(rf, rs, excluded_families=removed | unknown)
                baseline, *_ = ranking_and_distribution(rr, mapping, oceans, 100)
                baseline = {r["function_id"]: r for r in baseline}
                for r in ranks:
                    b = baseline.get(r["function_id"])
                    r.update(
                        reference_rank=b["rank"] if b else None,
                        reference_contribution=b["relative_contribution"] if b else None,
                    )
            targets, pls = [], []
            if query.include_environment:
                targets, pls = self._environment(query.source)
            sections = [
                dict(
                    figure=figure,
                    output=label,
                    status="completed" if data else "blocked",
                    reason=None if data else "没有可用结果",
                )
                for figure, label, data in (
                    ("9a", "功能排名与相对转录贡献", ranks),
                    ("9b", "粒径分配", [r for r in size if r["allocation_fraction"] is not None]),
                    ("9c", "海区分配", [r for r in ocean if r["allocation_fraction"] is not None]),
                )
            ]
            sections.extend(
                dict(
                    figure=p.figure, output=p.target + "环境关联", status=p.status, reason=p.reason
                )
                for p in pls
            )
            done = sum(s["status"] == "completed" for s in sections)
            warnings = [
                ResultWarning(
                    code="paper_denominator_difference",
                    message=(
                        "论文图注称以硅藻总转录信号为分母；作者图9脚本以筛选后Pfam汇总信号归一化。"
                        + (
                            "当前榜单按全部已提供硅藻基因信号归一化后等权平均，保留功能不再二次归一化。"
                            if query.normalization == "taxon_total"
                            else "当前榜单按作者脚本二次归一化，不是全部硅藻总转录量中的百分比。"
                        )
                    ),
                ),
                ResultWarning(
                    code="nonexclusive_domains",
                    message=(
                        "功能合并沿用作者对Pfam信号求和的规则；共享基因与多结构域不分摊，"
                        "不能解释为互斥基因组成、蛋白活性或RNA/DNA活性。"
                    ),
                ),
                ResultWarning(
                    code="reference_scope",
                    message=(
                        "paper_reference使用作者公开汇总表；current_data使用当前唯一gene–Pfam候选映射，"
                        "仅保留DNA11/cDNA14。两者的结构域计数、样本范围和分母不同，不能声称精确复现。"
                    ),
                    details={"pfams_without_reference_name": len(unknown)},
                ),
            ]
            filters = {
                "query": query.model_dump(),
                "author_commit": reference.manifest["author_commit"],
                "reference_manifest_sha256": reference.record.sha256,
                "source_files": reference.manifest["sources"],
                "sample_merging": "station_depth_equivalent_fraction_protocol_before_normalization",
                "excluded_pfams": sorted(removed),
                "environment_source": "author_station_depth_table",
                "normalization": query.normalization,
                "reference_contribution_normalization": "author_script_retained_pfam",
                "ambiguous_ocean_stations_excluded": ambiguous_oceans,
                "method_reference": REFERENCE_URL,
            }
            if self.reader is not None and query.source == "current_data":
                self.reader.ensure_unchanged()
                filters.update(
                    manifest_sha256=self.reader.manifest_record.sha256,
                    study_manifest_sha256=self.reader.study_sha256,
                )
            result = FunctionStudyResult(
                query=query,
                status="completed" if done == len(sections) else "partial" if done else "blocked",
                denominator=(
                    "all_taxon_gene_signal_per_group_then_equal_group_mean"
                    if query.normalization == "taxon_total"
                    else "retained_pfam_signal_per_merged_sample_then_total_relative_signal"
                    if query.source == "paper_reference"
                    else "all_taxon_gene_signal_per_group_then_retained_function_total"
                ),
                sample_group_count=n,
                top_contribution_sum=math.fsum(r["relative_contribution"] for r in ranks),
                source_comparison="作者脚本口径的参考重算"
                if query.source == "paper_reference"
                else "当前数据计算与作者参考榜单并列，非精确复现",
                sections=sections,
                function_ranks=ranks,
                size_distribution=size,
                ocean_distribution=ocean,
                target_signals=targets,
                pls_results=pls,
                excluded_samples=excluded,
                metadata=ResultMetadata(
                    provenance=DataProvenance(
                        source_datasets=(
                            [
                                "Bacillariophyta.MATOU-v1.5.Pfam.metaG.tsv.gz",
                                "Bacillariophyta.MATOU-v1.5.Pfam.metaT.tsv.gz",
                            ]
                            if query.source == "paper_reference"
                            else [
                                "MATOU-v1.5.taxonomy.tsv.gz",
                                "MATOU-v1.5.pfam.gz",
                                "MATOU-v1.5.metaG.occurrences.gz",
                                "MATOU-v1.5.metaT.occurrences.gz",
                            ]
                        )
                        + [
                            "PfamA.list",
                            "station_ocean.tsv",
                            "physicochemistry.for.metaT.tsv",
                            "station_Lat_Long_uniq.withTaraPrefix.tsv",
                            *(
                                ["LHC.diatoms.MATOUv1.5.seqs.function.tsv.gz"]
                                if query.source == "current_data"
                                and "lhc_profiles" in reference.manifest["files"]
                                else []
                            ),
                        ],
                        sample_count=n,
                        excluded_sample_count=len(excluded),
                        filters=filters,
                    ),
                    warnings=warnings,
                ),
            )
            operation.finish(
                ObservationUpdate(output_data={"status": result.status, "functions": len(ranks)})
            )
            return result

    def _environment(self, source):
        reference = self._reference()
        env = reference.frame("environment")
        coords = reference.frame("coordinates")
        if (
            env.unique(["station", "depth"]).height != env.height
            or coords["station"].n_unique() != coords.height
        ):
            raise ValueError("作者环境或经纬度映射存在重复键")
        environments = {(str(r["station"]), r["depth"]): r for r in env.iter_rows(named=True)}
        latitudes = {str(r["station"]): r["lat"] for r in coords.iter_rows(named=True)}
        signals = []
        for assay in ("MetaG", "MetaT"):
            f, totals, scope, _ = self._profiles(assay, source)
            rows, denoms = relative_profiles(
                f, scope, totals=totals, excluded_families={"PF02100"}, protocol=False
            )
            for target, acc in (("DUF285", "PF03382"), ("LHC", "PF00504")):
                # 按目标分别索引，避免同一采样组的不同家族相互覆盖。
                values = {
                    (r["station"], r["depth"], r["size_fraction"]): r
                    for r in rows
                    if r["pfam_accession"] == acc
                }
                for key, denominator in denoms.items():
                    if key[2] not in SIZES:
                        continue
                    r = values.get(key)
                    fill = (
                        r is None
                        and denominator > 0
                        and source == "paper_reference"
                        and target == "DUF285"
                    )
                    signals.append(
                        dict(
                            target=target,
                            assay=assay,
                            station=key[0],
                            depth=key[1],
                            size_fraction=key[2],
                            denominator=denominator,
                            numerator=r["value_sum"] if r else 0.0 if fill else None,
                            relative_signal=r["relative_signal"] if r else 0.0 if fill else None,
                            denominator_scope="all_taxon_gene_signal"
                            if source == "current_data"
                            else "retained_pfam_signal",
                            status="observed" if r else "paper_zero_fill" if fill else "missing",
                        )
                    )
        du_rows = self._pls_rows(
            [s for s in signals if s["target"] == "DUF285"],
            environments,
            latitudes,
            source,
            subfamily=False,
        )
        pls = [
            fit_pls(
                du_rows, PREDICTORS, ["MetaG_DUF285", "MetaT_DUF285"], target="DUF285", figure="10b"
            )
        ]
        if source == "current_data" and "lhc_profiles" in reference.manifest["files"]:
            expected = reference.manifest.get("lhc_source_manifest_sha256")
            if expected != self.reader.manifest_record.sha256:
                raise ValueError("LHC亚家族预计算与当前MATOU版本不一致")
            cached = reference.frame("lhc_profiles")
            for assay in ("MetaG", "MetaT"):
                current = cached.filter(pl.col("assay") == assay)
                scope, _ = sample_scope(
                    current["sample_name"].unique().to_list(), assay, reference=False
                )
                f = current.rename({"subfamily": "pfam_accession"})
                rows, denoms = relative_profiles(f, scope, protocol=False)
                for subfamily in SUBFAMILIES:
                    index = {
                        (r["station"], r["depth"], r["size_fraction"]): r
                        for r in rows
                        if r["pfam_accession"] == subfamily
                    }
                    for key, denom in denoms.items():
                        if key[2] in SIZES:
                            r = index.get(key)
                            signals.append(
                                dict(
                                    target=subfamily,
                                    assay=assay,
                                    station=key[0],
                                    depth=key[1],
                                    size_fraction=key[2],
                                    denominator=denom,
                                    numerator=r["value_sum"] if r else None,
                                    relative_signal=r["relative_signal"] if r else None,
                                    denominator_scope="all_classified_LHC_signal_including_LHCr9Homolog",
                                    status="observed" if r else "missing",
                                )
                            )
            lhc_rows = self._pls_rows(
                [s for s in signals if s["target"] in SUBFAMILIES],
                environments,
                latitudes,
                source,
                subfamily=True,
            )
            pls.append(
                fit_pls(
                    lhc_rows,
                    PREDICTORS,
                    [a + "_" + s for a in ("MetaG", "MetaT") for s in SUBFAMILIES],
                    target="LHC亚家族",
                    figure="10e",
                )
            )
        else:
            pls.append(
                PLSResult(
                    target="LHC亚家族",
                    figure="10e",
                    status="blocked",
                    reason=(
                        "需要以作者逐geneID亚家族注释与当前MetaG/MetaT基因信号预计算的lhc_profiles；"
                        "PF00504总量不能代替LHCf/LHCq/LHCr/LHCx/LHCz。作者公开Pfam汇总表不能还原亚家族。"
                    ),
                )
            )
        return signals, pls

    @staticmethod
    def _pls_rows(signals, environments, latitudes, source, *, subfamily):
        wide = {}
        for s in signals:
            key = (s["station"], s["depth"], s["size_fraction"])
            if key not in wide:
                env = environments.get(key[:2], {})
                lat = latitudes.get(key[0])
                size = float(key[2].split("-")[0].split("/")[0])
                wide[key] = {
                    "sampling_group": ":".join(key),
                    **{p: env.get(p) for p in PREDICTORS},
                    "size": size if subfamily else math.log1p(size),
                    "layer": 1.0 if key[1] == "SUR" else 2.0,
                    "abslat": abs(lat) if lat is not None else None,
                }
                for assay in ("MetaG", "MetaT"):
                    for target in SUBFAMILIES if subfamily else ["DUF285"]:
                        wide[key][assay + "_" + target] = None
            wide[key][s["assay"] + "_" + s["target"]] = s["relative_signal"]
        # 当前数据不将空记录当零；完整案例剔除由fit_pls报告。
        return [wide[key] for key in sorted(wide)]
