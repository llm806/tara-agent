"""跨样本描述性分析；只比较明确的相对信号，不宣称绝对表达或蛋白活性。"""

import math
import statistics
from collections import defaultdict

import polars as pl
from scipy.stats import spearmanr

from tara_agent.analysis.function_atlas_models import (
    AtlasObservation,
    AtlasQuery,
    AtlasRank,
    AtlasResult,
    ComparisonResult,
    FunctionComparisonQuery,
    SequenceQuery,
    SequenceResult,
)
from tara_agent.analysis.function_signal import validate_function_frames
from tara_agent.data.matou_reader import MatouDataReader
from tara_agent.data.matou_study import correspondence, load_sequences, load_study, load_study_frame
from tara_agent.domain.contracts import DataProvenance, ResultMetadata, ResultWarning
from tara_agent.observability.contracts import ObservationKind, ObservationUpdate
from tara_agent.observability.execution import observe


def sum_values(values) -> float:
    try:
        total = math.fsum(values)
    except OverflowError as error:
        raise ValueError("功能信号求和溢出") from error
    if not math.isfinite(total):
        raise ValueError("功能信号求和溢出")
    return total


def variability(values):
    """同一有效对应集合的描述性离散程度；不做差异表达显著性判断。"""
    if len(values) < 2:
        return {"iqr": None, "population_cv": None}
    quartiles = statistics.quantiles(values, n=4, method="inclusive")
    mean = statistics.fmean(values)
    return {
        "iqr": quartiles[2] - quartiles[0],
        "population_cv": statistics.pstdev(values) / mean if mean else None,
    }


def summarize_sample(frame: pl.DataFrame, mapping: pl.DataFrame, name: str) -> dict:
    """完整聚合全部家族后再排名；未注释基因保留在分母中。"""
    validate_function_frames(frame, mapping, name)
    total = sum_values(frame["value"])
    linked = frame.select("geneID", "value").join(mapping, on="geneID", how="inner")
    grouped = linked.group_by("pfamAcc").agg(pl.col("value").sum().alias("value_sum"))
    if grouped.filter(~pl.col("value_sum").is_finite()).height:
        raise ValueError("功能家族信号求和溢出")
    families = dict(zip(grouped["pfamAcc"], grouped["value_sum"], strict=True))
    return {
        "sample_name": name,
        "taxon_value_sum": total,
        "observed_gene_count": frame.height,
        "families": families,
    }


class MatouAtlasService:
    def __init__(self, reader: MatouDataReader):
        self.reader = reader

    def _check_taxon(self, taxon):
        if (
            taxon is not None
            and taxon.casefold() != self.reader.manifest.selection["taxon"].casefold()
        ):
            raise ValueError("请求类群不在当前功能数据范围内")

    def _mapping(self, max_i_evalue):
        mapping = self.reader.load_gene_pfam()
        if max_i_evalue is not None:
            mapping = mapping.filter(pl.col("min_iEvalue") <= max_i_evalue)
        return mapping

    def _metadata(self, query, sample_count, warnings=()):
        manifest = self.reader.manifest
        datasets = ["matou_taxonomy", "matou_pfam"]
        if isinstance(query, AtlasQuery):
            datasets.append("matou_" + query.assay.lower())
        elif isinstance(query, FunctionComparisonQuery):
            datasets.extend(["matou_metag", "matou_metat"])
        else:
            datasets.append("matou_fasta")
        return ResultMetadata(
            provenance=DataProvenance(
                source_datasets=datasets,
                sample_count=sample_count,
                filters={
                    "query": query.model_dump(),
                    "selection": manifest.selection,
                    "dataset_version": manifest.dataset_version,
                    "manifest_sha256": self.reader.manifest_record.sha256,
                    "study_manifest_sha256": self.reader.study_sha256,
                    "source_files": {k: v.model_dump() for k, v in manifest.source_files.items()},
                    "source_reports": {
                        k: v.model_dump() for k, v in manifest.source_reports.items()
                    },
                    "method_reference": "https://doi.org/10.1038/s41467-025-58027-7",
                },
            ),
            warnings=[
                ResultWarning(
                    code="candidate_annotation",
                    message=(
                        "Pfam 是候选结构域关联，不证明生物学功能；"
                        "统一 E 条件不替代家族 gathering 阈值。"
                    ),
                ),
                ResultWarning(
                    code="relative_observed_signal",
                    message=(
                        "仅比较样本内已提供数值的相对贡献，"
                        "不解释为绝对表达、RNA/DNA 活性或蛋白活性。"
                    ),
                ),
                ResultWarning(
                    code="shared_genes",
                    message="每个 gene–Pfam 对仅计一次；不同家族可共享基因，份额不可相加为 100%。",
                ),
                *warnings,
            ],
        )

    def _frames(self, assay, names, mapping, max_i_evalue):
        study = load_study(self.reader)
        if study is not None and study.max_i_evalue == max_i_evalue:
            totals = load_study_frame(self.reader, study.summaries, "读取 MATOU 样本总信号缓存")
            profiles = load_study_frame(self.reader, study.profiles, "读取 MATOU 跨样本功能谱缓存")
            if totals.schema != {
                "assay": pl.String,
                "sample_name": pl.String,
                "taxon_value_sum": pl.Float64,
                "observed_gene_count": pl.Int64,
            } or profiles.schema != {
                "assay": pl.String,
                "sample_name": pl.String,
                "pfam_accession": pl.String,
                "value_sum": pl.Float64,
            }:
                raise ValueError("功能谱缓存列或类型不符合契约")
            if any(totals.null_count().row(0)) or any(profiles.null_count().row(0)):
                raise ValueError("功能谱缓存有未定义输入值")
            if (
                totals.unique(["assay", "sample_name"]).height != totals.height
                or profiles.unique(["assay", "sample_name", "pfam_accession"]).height
                != profiles.height
            ):
                raise ValueError("功能谱缓存存在重复键")
            if (
                totals.filter(
                    ~pl.col("taxon_value_sum").is_finite()
                    | (pl.col("taxon_value_sum") < 0)
                    | (pl.col("observed_gene_count") < 0)
                ).height
                or profiles.filter(
                    ~pl.col("value_sum").is_finite() | (pl.col("value_sum") < 0)
                ).height
            ):
                raise ValueError("功能谱缓存存在非法数值")
            totals = totals.filter((pl.col("assay") == assay) & pl.col("sample_name").is_in(names))
            if set(totals["sample_name"]) != set(names):
                raise ValueError("功能谱缓存缺少请求样本")
            profiles = profiles.filter(
                (pl.col("assay") == assay) & pl.col("sample_name").is_in(names)
            )
            bounds = profiles.join(totals, on=["assay", "sample_name"], how="left")
            if bounds.filter(pl.col("value_sum") > pl.col("taxon_value_sum") * (1 + 1e-12)).height:
                raise ValueError("功能家族信号超过类群分母")
            return totals, profiles
        if len(names) > 20:
            raise ValueError(
                "跨样本查询需要匹配条件的功能谱缓存；先执行 prepare-study，避免聊天重复扫描全部基因"
            )
        samples = [summarize_sample(self.reader.load_sample(assay, n), mapping, n) for n in names]
        totals = pl.DataFrame(
            [{"assay": assay, **{k: v for k, v in s.items() if k != "families"}} for s in samples],
            schema={
                "assay": pl.String,
                "sample_name": pl.String,
                "taxon_value_sum": pl.Float64,
                "observed_gene_count": pl.Int64,
            },
        )
        profiles = pl.DataFrame(
            [
                {
                    "assay": assay,
                    "sample_name": s["sample_name"],
                    "pfam_accession": f,
                    "value_sum": v,
                }
                for s in samples
                for f, v in s["families"].items()
            ],
            schema={
                "assay": pl.String,
                "sample_name": pl.String,
                "pfam_accession": pl.String,
                "value_sum": pl.Float64,
            },
        )
        return totals, profiles

    def _samples(self, assay, names, mapping, max_i_evalue, selected_families):
        totals, profiles = self._frames(assay, names, mapping, max_i_evalue)
        families = defaultdict(dict)
        for row in profiles.filter(pl.col("pfam_accession").is_in(selected_families)).iter_rows(
            named=True
        ):
            families[row["sample_name"]][row["pfam_accession"]] = row["value_sum"]
        return [
            {**row, "families": families[row["sample_name"]]}
            for row in totals.iter_rows(named=True)
        ]

    def atlas(self, query: AtlasQuery) -> AtlasResult:
        self._check_taxon(query.taxon)
        with observe(
            "计算 MATOU 跨样本功能谱",
            ObservationKind.SERVICE,
            ObservationUpdate(input_data={"query": query.model_dump()}),
        ) as observation:
            available = {s.sample_name: s for s in self.reader.list_samples(query.assay)}
            names = query.sample_names if query.sample_names is not None else sorted(available)
            if not names or len(names) > 2000 or not set(names) <= available.keys():
                raise ValueError("样本范围为空、超过 2000 个或包含未知原始编号")
            mapping = self._mapping(query.max_i_evalue)
            known = set(mapping["pfamAcc"])
            totals, profiles = self._frames(query.assay, names, mapping, query.max_i_evalue)
            informative = totals.filter(pl.col("taxon_value_sum") > 0).height
            contributions = (
                profiles.join(totals, on=["assay", "sample_name"])
                .filter(pl.col("taxon_value_sum") > 0)
                .with_columns(
                    (pl.col("value_sum") / pl.col("taxon_value_sum")).clip(0, 1).alias("fraction")
                )
            )
            family_count = contributions["pfam_accession"].n_unique()
            candidates = contributions
            if query.pfam_accessions is not None:
                candidates = candidates.filter(
                    pl.col("pfam_accession").is_in(query.pfam_accessions)
                )
            # 此分数是已观测贡献的等权平均，空记录贡献不等同于生物学零值。
            ranked = (
                candidates.group_by("pfam_accession")
                .agg(
                    (pl.col("fraction").sum() / informative if informative else pl.lit(0.0)).alias(
                        "mean_observed_contribution"
                    ),
                    pl.len().alias("samples_with_observed_records"),
                    pl.col("fraction").median().alias("median_fraction_when_observed"),
                )
                .sort(["mean_observed_contribution", "pfam_accession"], descending=[True, False])
                .head(query.top_n)
            )
            ordered = ranked["pfam_accession"].to_list()
            selected = ordered if query.pfam_accessions is None else query.pfam_accessions
            if len(selected) * len(names) > 100000:
                raise ValueError("结果超过 100000 行，请缩小样本或家族范围")
            ranks = [
                AtlasRank(**row, rank=i + 1, informative_samples=informative)
                for i, row in enumerate(ranked.iter_rows(named=True))
            ]
            matrix = totals.join(
                pl.DataFrame({"pfam_accession": selected}, schema={"pfam_accession": pl.String}),
                how="cross",
            ).join(profiles, on=["assay", "sample_name", "pfam_accession"], how="left")
            points = [
                AtlasObservation(
                    assay=query.assay,
                    sample_name=row["sample_name"],
                    pfam_accession=row["pfam_accession"],
                    status="observed"
                    if row["value_sum"] is not None
                    else (
                        "no_observed_gene_record"
                        if row["pfam_accession"] in known
                        else "not_in_retained_mapping"
                    ),
                    value_sum=row["value_sum"],
                    taxon_value_sum=row["taxon_value_sum"],
                    observed_fraction=min(1.0, row["value_sum"] / row["taxon_value_sum"])
                    if row["value_sum"] is not None and row["taxon_value_sum"]
                    else None,
                )
                for row in matrix.sort(["sample_name", "pfam_accession"]).iter_rows(named=True)
            ]
            result = AtlasResult(
                query=query,
                taxon=self.reader.manifest.selection["taxon"],
                dataset_version=self.reader.manifest.dataset_version,
                sample_count=len(names),
                informative_samples=informative,
                zero_denominator_samples=[
                    s["sample_name"]
                    for s in totals.iter_rows(named=True)
                    if not s["taxon_value_sum"]
                ],
                family_count=family_count,
                ranks=ranks,
                observations=points,
                sample_summaries=totals.sort("sample_name").to_dicts(),
                metadata=self._metadata(
                    query,
                    len(names),
                    [
                        ResultWarning(
                            code="ranking_scope",
                            message="总体排名按有正分母的原始样本等权计算已观测贡献，未出现记录不补为生物学零；观测覆盖同时报告。未合并重复采样或近似粒径，不是论文图9的精确复现。",
                        )
                    ],
                ),
            )
            self.reader.ensure_unchanged()
            observation.finish(
                ObservationUpdate(output_data={"samples": len(names), "families": family_count})
            )
            return result

    @staticmethod
    def _point(assay, sample, family, known):
        value = sample["families"].get(family)
        total = sample["taxon_value_sum"]
        return AtlasObservation(
            assay=assay,
            sample_name=sample["sample_name"],
            pfam_accession=family,
            status="observed"
            if value is not None
            else ("no_observed_gene_record" if family in known else "not_in_retained_mapping"),
            value_sum=value,
            observed_fraction=min(1.0, value / total) if value is not None and total else None,
            taxon_value_sum=total,
        )

    def compare(self, query: FunctionComparisonQuery) -> ComparisonResult:
        self._check_taxon(query.taxon)
        with observe(
            "比较对应采样编码的 MetaG 与 MetaT 相对功能信号",
            ObservationKind.SERVICE,
            ObservationUpdate(input_data={"query": query.model_dump()}),
        ) as observation:
            pairs, excluded, evidence = correspondence(self.reader)
            if query.sampling_keys is not None:
                if not set(query.sampling_keys) <= {p["sampling_key"] for p in pairs}:
                    raise ValueError("采样编码没有无歧义的 MetaG/MetaT 对应样本")
                pairs = [p for p in pairs if p["sampling_key"] in query.sampling_keys]
            if not pairs or len(pairs) > 2000:
                raise ValueError("没有可比较的无歧义采样编码或超过资源上限")
            mapping = self._mapping(query.max_i_evalue)
            known = set(mapping["pfamAcc"])
            gsamples = {
                s["sample_name"]: s
                for s in self._samples(
                    "MetaG",
                    [p["metag_sample"] for p in pairs],
                    mapping,
                    query.max_i_evalue,
                    query.pfam_accessions,
                )
            }
            tsamples = {
                s["sample_name"]: s
                for s in self._samples(
                    "MetaT",
                    [p["metat_sample"] for p in pairs],
                    mapping,
                    query.max_i_evalue,
                    query.pfam_accessions,
                )
            }
            points = []
            for pair in pairs:
                g, t = gsamples[pair["metag_sample"]], tsamples[pair["metat_sample"]]
                for family in query.pfam_accessions:
                    gp, tp = (
                        self._point("MetaG", g, family, known),
                        self._point("MetaT", t, family, known),
                    )
                    points.append(
                        {
                            **pair,
                            "pfam_accession": family,
                            "metag_fraction": gp.observed_fraction,
                            "metat_fraction": tp.observed_fraction,
                            "fraction_difference": tp.observed_fraction - gp.observed_fraction
                            if gp.observed_fraction is not None and tp.observed_fraction is not None
                            else None,
                            "metag_status": gp.status,
                            "metat_status": tp.status,
                            "metag_denominator": gp.taxon_value_sum,
                            "metat_denominator": tp.taxon_value_sum,
                        }
                    )
            summaries = []
            for family in query.pfam_accessions:
                selected = [p for p in points if p["pfam_accession"] == family]
                valid = [p for p in selected if p["fraction_difference"] is not None]
                gs, ts = [p["metag_fraction"] for p in valid], [p["metat_fraction"] for p in valid]
                rho = None
                if len(valid) >= 3 and len(set(gs)) > 1 and len(set(ts)) > 1:
                    rho = float(spearmanr(gs, ts).statistic)
                summaries.append(
                    {
                        "pfam_accession": family,
                        "complete_correspondences": len(valid),
                        "excluded_correspondences": len(selected) - len(valid),
                        "spearman_rho": rho,
                        "metag_median_fraction": statistics.median(gs) if gs else None,
                        "metat_median_fraction": statistics.median(ts) if ts else None,
                        "median_fraction_difference": statistics.median(
                            [p["fraction_difference"] for p in valid]
                        )
                        if valid
                        else None,
                        "correlation_status": "defined"
                        if rho is not None
                        else "insufficient_or_constant",
                        "inference": "descriptive_no_significance_test",
                        **{f"metag_{k}": v for k, v in variability(gs).items()},
                        **{f"metat_{k}": v for k, v in variability(ts).items()},
                    }
                )
            metadata = self._metadata(
                query,
                len(pairs) * 2,
                [
                    ResultWarning(
                        code="sampling_code_correspondence",
                        message=(
                            "按版本化作者编码规则精确匹配站位、水层、批次与原始过滤组，"
                            "仅比较 DNA11 与 cDNA14。编码对应不证明同一提取物；"
                            "不合并 WGA、重复或近似过滤组，不计算 RNA/DNA 活性。"
                        ),
                    ),
                    ResultWarning(
                        code="complete_case_description",
                        message=(
                            "相关仅使用两侧都有观测且分母为正的记录；缺失不补零，"
                            "rho 不作为显著性、因果或差异表达检验。"
                        ),
                    ),
                ],
            )
            metadata.provenance.filters["correspondence_evidence"] = evidence
            self.reader.ensure_unchanged()
            result = ComparisonResult(
                query=query,
                matched_sampling_keys=len(pairs),
                excluded_samples=excluded,
                summaries=summaries,
                points=points,
                metadata=metadata,
            )
            observation.finish(ObservationUpdate(output_data={"matched_sampling_keys": len(pairs)}))
            return result

    def sequences(self, query: SequenceQuery) -> SequenceResult:
        with observe(
            "查询已核验 MATOU 核酸序列",
            ObservationKind.SERVICE,
            ObservationUpdate(input_data={"query": query.model_dump()}),
        ) as observation:
            frame, evidence = load_sequences(self.reader)
            if query.gene_ids is None and query.pfam_accession is None:
                raise ValueError("序列查询须指定 geneID 或 Pfam")
            if query.gene_ids is not None:
                frame = frame.filter(pl.col("geneID").is_in(query.gene_ids))
            if query.pfam_accession is not None:
                mapping = self.reader.load_gene_pfam().filter(
                    pl.col("pfamAcc") == query.pfam_accession
                )
                frame = frame.join(mapping.select("geneID").unique(), on="geneID", how="semi")
            total = frame.height
            unavailable = sorted(set(query.gene_ids or []) - set(frame["geneID"]))
            selected = frame.sort("geneID").slice(query.offset, query.limit)
            if selected["sequence"].str.len_chars().sum() > 200000:
                raise ValueError("序列返回超过 200000 个碱基，请缩小查询范围")
            metadata = self._metadata(query, 0)
            metadata.provenance.filters["sequence_evidence"] = evidence
            result = SequenceResult(
                query=query,
                total=total,
                sequences=selected.to_dicts(),
                metadata=metadata,
                unavailable_gene_ids=unavailable,
            )
            self.reader.ensure_unchanged()
            observation.finish(ObservationUpdate(output_data={"total": total}))
            return result
