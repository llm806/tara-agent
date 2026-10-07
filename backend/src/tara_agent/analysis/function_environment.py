"""以经核验的显式映射连接功能信号与context_stat；重复映射目标只贡献一个统计观测。"""

import math
from collections import defaultdict

import polars as pl

from tara_agent.analysis.community_models import FunctionEnvironmentQuery, FunctionEnvironmentResult
from tara_agent.analysis.ecology_statistics import association, finite
from tara_agent.analysis.function_atlas import MatouAtlasService
from tara_agent.data.research_context import ResearchContext
from tara_agent.domain.contracts import DataProvenance, ResultMetadata, ResultWarning
from tara_agent.observability.contracts import ObservationKind, ObservationUpdate
from tara_agent.observability.execution import observe


class FunctionEnvironmentService:
    def __init__(self, reader, matou_reader, research=None):
        self.reader, self.matou = reader, matou_reader
        self.research = research or ResearchContext(reader.processed_dir)
        self.atlas = MatouAtlasService(matou_reader)

    def analyze(self, query: FunctionEnvironmentQuery):
        with observe(
            "功能转录信号与环境关联",
            ObservationKind.SERVICE,
            ObservationUpdate(input_data=query.model_dump(mode="json")),
        ):
            return self._analyze(query)

    def _analyze(self, query):
        self.atlas._check_taxon(query.taxon)
        available = {s.sample_name for s in self.matou.list_samples(query.assay)}
        names = query.sample_names if query.sample_names is not None else sorted(available)
        if not names or not set(names) <= available:
            raise ValueError("样本范围为空或含未知原始编号")
        points, excluded, stats = [], [], []
        supplemental_source = None
        supplemental_used = 0
        reason = None
        coverage = {
            "selected": len(names),
            "mapped": 0,
            "matched_context_samples": 0,
            "valid_statistical_samples": 0,
        }
        try:
            mapping = self.research.frame(
                "sample_mapping",
                generation=self.reader.manifest.generation,
                matou_sha256=self.matou.manifest_record.sha256,
            )
        except ValueError as error:
            reason = str(error)
        else:
            mapping = mapping.filter(pl.col("assay") == query.assay)
            mappings = {r["sample_name"]: r for r in mapping.to_dicts()}
            context = {
                r["sample_id_pangaea"]: r for r in self.reader.load_sample_context().to_dicts()
            }
            field = query.environment_variable.value
            supplements = {}
            if self.research.manifest and "sample_environment" in self.research.manifest.get(
                "files", {}
            ):
                supplement = self.research.frame(
                    "sample_environment", generation=self.reader.manifest.generation
                ).filter(pl.col("variable") == field)
                expected_unit = (
                    "degree_Celsius"
                    if field == "temperature"
                    else self.research.manifest.get("units", {}).get(field)
                )
                if supplement.height and (
                    not expected_unit or set(supplement["unit"]) != {expected_unit}
                ):
                    raise ValueError("样本补充环境与核心变量单位未经一致性核验：" + field)
                supplements = {r["sample_id_pangaea"]: r for r in supplement.to_dicts()}
                supplemental_source = self.research.manifest["sources"]["sample_environment"]
            linked = [n for n in names if n in mappings]
            coverage["mapped"] = len(linked)
            excluded.extend(
                {"sample_name": n, "reason": "无经核验跨库映射"} for n in names if n not in mappings
            )
            if linked:
                samples = self.atlas._samples(
                    query.assay,
                    linked,
                    self.atlas._mapping(query.max_i_evalue),
                    query.max_i_evalue,
                    [query.pfam_accession],
                )
                by_target = defaultdict(list)
                for sample in samples:
                    mapping_row = mappings[sample["sample_name"]]
                    by_target[mapping_row["sample_id_pangaea"]].append(sample)
                coverage["matched_context_samples"] = sum(
                    sid in context or sid in supplements for sid in by_target
                )
                for sid, members in by_target.items():
                    source = context.get(sid)
                    value = source.get(field) if source else None
                    supplement = supplements.get(sid) if not finite(value) else None
                    if supplement is not None:
                        value = supplement["value"]
                        supplemental_used += int(finite(value))
                    values = [s["families"].get(query.pfam_accession) for s in members]
                    denominators = [s["taxon_value_sum"] for s in members]
                    # 任一技术记录缺家族不能用其他记录覆盖；缺失不按生物学零处理。
                    numerator = math.fsum(values) if all(v is not None for v in values) else None
                    denominator = math.fsum(denominators)
                    relative = (
                        numerator / denominator
                        if numerator is not None and denominator > 0
                        else None
                    )
                    signal = relative if query.signal == "relative" else numerator
                    points.append(
                        dict(
                            sample_id=sid,
                            sample_names=[s["sample_name"] for s in members],
                            environment_value=value,
                            target_signal=signal,
                            numerator=numerator,
                            denominator=denominator,
                            relative_signal=relative,
                            status="observed" if finite(signal) and finite(value) else "missing",
                            mapping_evidence=[
                                mappings[s["sample_name"]]["evidence"] for s in members
                            ],
                            environment_source="sample_environment"
                            if supplement is not None
                            else "context_stat",
                            environment_evidence=supplement["evidence"]
                            if supplement is not None
                            else None,
                            environment_method=supplement["method"]
                            if supplement is not None
                            else None,
                            environment_context_details=supplement["context_details"]
                            if supplement is not None
                            else None,
                        )
                    )
                stats = [
                    association(
                        points,
                        "environment_value",
                        "target_signal",
                        permutations=query.permutations,
                        seed=query.seed,
                    )
                ]
                coverage["valid_statistical_samples"] = stats[0]["sample_count"]
                if stats[0]["status"] != "completed":
                    reason = stats[0]["reason"]
            else:
                reason = "所选范围无可用跨库映射"
        if reason:
            excluded.append({"reason": reason})
        self.matou.ensure_unchanged()
        self.research.ensure_unchanged()
        return FunctionEnvironmentResult(
            query=query,
            status="blocked" if reason else "partial" if excluded else "completed",
            points=points,
            associations=stats,
            excluded_samples=excluded,
            mapping_coverage=coverage,
            metadata=ResultMetadata(
                provenance=DataProvenance(
                    source_datasets=[
                        "context_stat",
                        "matou_taxonomy",
                        "matou_pfam",
                        "matou_" + query.assay.lower(),
                    ]
                    + (["research_sample_environment"] if supplemental_used else []),
                    sample_count=coverage["valid_statistical_samples"],
                    excluded_sample_count=len(names) - coverage["mapped"],
                    filters={
                        "query": query.model_dump(mode="json"),
                        "core_generation": self.reader.manifest.generation,
                        "manifest_sha256": self.matou.manifest_record.sha256,
                        "research_manifest_sha256": self.research.sha256,
                        "mapping_source": self.research.manifest.get("sources", {}).get(
                            "sample_mapping"
                        )
                        if self.research.manifest
                        else None,
                        "signal_unit": "dimensionless_fraction"
                        if query.signal == "relative"
                        else "provided_MATOU_value_unit_not_inferred",
                        "environment_unit": "degree_Celsius"
                        if query.environment_variable.value == "temperature"
                        else "see_context_stat_source_metadata",
                        "replicate_aggregation": (
                            "sum_numerators_and_denominators_per_explicit_PANGAEA_sample"
                        ),
                        "denominator": "all_provided_taxon_gene_signal_including_unannotated",
                        "sample_environment_source": supplemental_source,
                        "supplemental_environment_values_used": supplemental_used,
                        "environment_merge": (
                            "retain_finite_core_value; exact_sample_ID_only; require_equal_units"
                        ),
                    },
                ),
                warnings=[
                    ResultWarning(
                        code="candidate_relative_signal",
                        message="候选Pfam信号不等于蛋白活性；相对信号不等于绝对表达。缺失不补零。",
                    ),
                    ResultWarning(
                        code="statistical_scope",
                        message="双侧Spearman置换检验；一个PANGAEA样本一行。相关仅作探索，不证明因果；未校正建库方法、航次、站位重复及其他环境混杂，须按已核实方法分层核验。",
                    ),
                ]
                + (
                    [
                        ResultWarning(
                            code="supplemental_environment_context",
                            message="部分环境值来自按确切样本ID核验的补充资料；测量方法、统计口径和时空间隔见逐点证据，不等同于同步水样实测。",
                        )
                    ]
                    if supplemental_used
                    else []
                ),
            ),
        )
