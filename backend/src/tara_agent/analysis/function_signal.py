"""计算已提供记录中的候选功能份额，不解释原始物理单位或补齐未出现记录。"""

import math

import polars as pl

from tara_agent.analysis.function_signal_models import (
    FunctionSignalContext,
    FunctionSignalObservation,
    FunctionSignalQuery,
    FunctionSignalResult,
)
from tara_agent.domain.contracts import DataProvenance, ResultMetadata, ResultWarning


def _check_frame(frame: pl.DataFrame, schema: dict[str, pl.DataType]) -> None:
    if frame.schema != schema or any(frame.null_count().row(0)):
        raise ValueError("输入列、类型或空值不符合候选功能计算契约")
    if frame.filter(pl.col("geneID") <= 0).height:
        raise ValueError("geneID 必须为正整数")


def _finite_nonnegative(frame: pl.DataFrame, column: str) -> None:
    if frame.filter(~pl.col(column).is_finite() | (pl.col(column) < 0)).height:
        raise ValueError(f"{column} 必须有限且非负")


def _value_sum(values: pl.Series) -> float:
    try:
        result = math.fsum(values)
    except OverflowError as error:
        raise ValueError("信号求和溢出，不能发布有限结果") from error
    if not math.isfinite(result):
        raise ValueError("信号求和溢出，不能发布有限结果")
    return result


def _fraction(part: float, total: float) -> float | None:
    # 键唯一与关联规则已保证每个家族是输入基因的子集；仅约束浮点累计的边界舍入。
    return min(1.0, part / total) if total else None


def validate_function_frames(
    occurrences: pl.DataFrame,
    gene_pfam: pl.DataFrame,
    sample_name: str,
) -> None:
    """单双样本计算共享唯一键和有限非负数检查，避免两套科学输入规则。"""
    _check_frame(occurrences, {"geneID": pl.Int64, "samplename": pl.String, "value": pl.Float64})
    _check_frame(gene_pfam, {"geneID": pl.Int64, "pfamAcc": pl.String, "min_iEvalue": pl.Float64})
    _finite_nonnegative(occurrences, "value")
    _finite_nonnegative(gene_pfam, "min_iEvalue")
    if occurrences.filter(pl.col("samplename") != sample_name).height:
        raise ValueError("定量输入包含其他样本，不能混合计算")
    if occurrences["geneID"].n_unique() != occurrences.height:
        raise ValueError("定量输入有重复基因，不自动合并")
    if gene_pfam.select("geneID", "pfamAcc").unique().height != gene_pfam.height:
        raise ValueError("需要已去重的 gene–Pfam 映射，不能直接使用 domain hits")
    if gene_pfam.filter(~pl.col("pfamAcc").str.contains(r"^PF[0-9]{5}$")).height:
        raise ValueError("映射的 Pfam 编号格式无效")


def compute_function_signal(
    occurrences: pl.DataFrame,
    gene_pfam: pl.DataFrame,
    context: FunctionSignalContext,
    query: FunctionSignalQuery,
) -> FunctionSignalResult:
    """输入须由数据层核验来源和类群范围；此函数不访问文件或调用模型。"""
    validate_function_frames(occurrences, gene_pfam, context.sample_name)
    retained = gene_pfam
    if query.max_i_evalue is not None:
        retained = retained.filter(pl.col("min_iEvalue") <= query.max_i_evalue)
    values = occurrences.select("geneID", "value")
    linked = values.join(retained.select("geneID", "pfamAcc"), on="geneID", how="inner")
    memberships = linked.group_by("geneID").len()
    annotated_values = values.join(memberships.select("geneID"), on="geneID", how="semi")
    total = _value_sum(values["value"])
    annotated = _value_sum(annotated_values["value"])

    # 分母先在全部输入记录上计算；质量条件、指定家族与 Top N 都不改变分母。
    families = linked.group_by("pfamAcc").agg(
        pl.col("value").sum().alias("value_sum"), pl.len().alias("observed_gene_count")
    )
    observed_family_count = families.height
    unobserved: list[str] = []
    missing: list[FunctionSignalObservation] = []
    if query.pfam_accessions is not None:
        observed = set(families["pfamAcc"].to_list())
        known = set(retained["pfamAcc"].unique().to_list())
        unobserved = [p for p in query.pfam_accessions if p not in observed]
        # 不把未出现的基因记录或未保留的注释转换成生物学零值。
        missing = [
            FunctionSignalObservation(
                pfam_accession=p,
                status="no_observed_gene_record" if p in known else "not_in_retained_mapping",
                observed_gene_count=0,
            )
            for p in unobserved
        ]
        families = families.filter(pl.col("pfamAcc").is_in(query.pfam_accessions))
    families = families.sort(["value_sum", "pfamAcc"], descending=[True, False]).head(query.top_n)
    observations = [
        FunctionSignalObservation(
            pfam_accession=row["pfamAcc"],
            status="observed",
            observed_gene_count=row["observed_gene_count"],
            value_sum=row["value_sum"],
            fraction_of_observed_taxon_signal=_fraction(row["value_sum"], total),
        )
        for row in families.iter_rows(named=True)
    ]
    multiple = memberships.filter(pl.col("len") > 1).height
    warnings = [
        ResultWarning(
            code="candidate_annotation",
            message="结果描述候选 Pfam 关联，不证明生物学功能或解决翻译框冲突。",
        ),
        ResultWarning(
            code="unconfirmed_export_unit",
            message="份额仅针对提供的数值；不称为原始 RPKM、绝对表达量或 RNA/DNA 活性。",
        ),
        ResultWarning(
            code="observed_records_only",
            message="仅汇总输入中实际提供的类群基因记录；未出现记录不补零。",
        ),
    ]
    if query.max_i_evalue is not None:
        warnings.append(
            ResultWarning(
                code="candidate_evalue_condition",
                message="统一 i-Evalue 条件不是 Pfam 家族的官方 gathering 阈值。",
            )
        )
    if multiple:
        warnings.append(
            ResultWarning(
                code="shared_genes_between_families",
                message="不同 Pfam 共享基因，各功能份额相加可能超过 100%。",
            )
        )
    if not total:
        warnings.append(
            ResultWarning(
                code="zero_observed_denominator",
                message="没有正的输入总信号，份额未定义；不据此认定样本中生物学功能缺失。",
            )
        )
    return FunctionSignalResult(
        context=context,
        query=query,
        observed_gene_count=values.height,
        taxon_value_sum=total,
        annotated_gene_count=annotated_values.height,
        annotated_value_sum=annotated,
        annotated_signal_fraction=_fraction(annotated, total),
        genes_with_multiple_pfams=multiple,
        observed_family_count=observed_family_count,
        unobserved_requested_pfams=unobserved,
        observations=observations + missing,
        metadata=ResultMetadata(
            provenance=DataProvenance(
                source_datasets=[f"matou_{context.assay.lower()}", "matou_pfam"],
                sample_count=1,
                filters={"context": context.model_dump(), "query": query.model_dump()},
            ),
            warnings=warnings,
        ),
    )
