"""组合经过来源校验的 MATOU 数据读取与候选功能计算。"""

from typing import Literal

from tara_agent.analysis.function_query_models import (
    FunctionProfileQuery,
    FunctionSampleItem,
    FunctionSamplesQuery,
    FunctionSamplesResult,
)
from tara_agent.analysis.function_signal import compute_function_signal
from tara_agent.analysis.function_signal_models import (
    FunctionSignalContext,
    FunctionSignalQuery,
    FunctionSignalResult,
)
from tara_agent.data.matou_reader import MatouDataReader
from tara_agent.domain.contracts import DataProvenance, ResultMetadata, ResultWarning
from tara_agent.observability.contracts import ObservationKind, ObservationUpdate
from tara_agent.observability.execution import observe


class MatouFunctionService:
    def __init__(self, reader: MatouDataReader):
        self.reader = reader

    def _check_taxon(self, taxon: str | None):
        if (
            taxon is not None
            and taxon.casefold() != self.reader.manifest.selection["taxon"].casefold()
        ):
            raise ValueError("请求类群不在当前功能数据范围内")

    def find_samples(self, query: FunctionSamplesQuery) -> FunctionSamplesResult:
        self._check_taxon(query.taxon)
        with observe(
            "检索 MATOU 功能样本",
            ObservationKind.SERVICE,
            ObservationUpdate(input_data={"query": query.model_dump()}),
        ) as observation:
            samples = self.reader.list_samples(query.assay)
            if query.sample_name_contains is not None:
                samples = [s for s in samples if query.sample_name_contains in s.sample_name]
            result = FunctionSamplesResult(
                assay=query.assay,
                taxon=self.reader.manifest.selection["taxon"],
                dataset_version=self.reader.manifest.dataset_version,
                query=query,
                total=len(samples),
                items=[
                    FunctionSampleItem(
                        sample_name=s.sample_name, observed_gene_records=s.artifact.rows
                    )
                    for s in samples[query.offset : query.offset + query.limit]
                ],
                metadata=ResultMetadata(
                    provenance=DataProvenance(
                        source_datasets=[self.reader.manifest.dataset_version],
                        sample_count=len(samples),
                        filters={
                            "query": query.model_dump(),
                            "selection": self.reader.manifest.selection,
                            "manifest_sha256": self.reader.manifest_record.sha256,
                            "source_reports": {
                                k: v.model_dump()
                                for k, v in self.reader.manifest.source_reports.items()
                            },
                        },
                    ),
                    warnings=[
                        ResultWarning(
                            code="sample_mapping_unconfirmed",
                            message=(
                                "样本名来自定量文件；名称筛选不解释采样条件，"
                                "不证明 DNA/RNA 配对或 context 关联。"
                            ),
                        )
                    ],
                ),
            )
            self.reader.ensure_unchanged()
            observation.finish(ObservationUpdate(output_data={"total": result.total}))
            return result

    def function_profile(self, query: FunctionProfileQuery) -> FunctionSignalResult:
        self._check_taxon(query.taxon)
        signal = FunctionSignalQuery.model_validate(
            query.model_dump(exclude={"assay", "sample_name", "taxon"})
        )
        return self.analyze_sample(query.assay, query.sample_name, signal)

    def analyze_sample(
        self, assay: Literal["MetaG", "MetaT"], sample_name: str, query: FunctionSignalQuery
    ) -> FunctionSignalResult:
        manifest = self.reader.manifest
        artifact = self.reader.sample_artifact(assay, sample_name)
        context = FunctionSignalContext(
            assay=assay,
            sample_name=sample_name,
            taxon=manifest.selection["taxon"],
            dataset_version=manifest.dataset_version,
            occurrence_sha256=artifact.sha256,
            annotation_sha256=manifest.gene_pfam.sha256,
        )
        with observe(
            "计算 MATOU 单样本候选功能谱",
            ObservationKind.SERVICE,
            ObservationUpdate(
                input_data={"context": context.model_dump(), "query": query.model_dump()}
            ),
        ) as observation:
            occurrences = self.reader.load_sample(assay, sample_name)
            mapping = self.reader.load_gene_pfam()
            result = compute_function_signal(occurrences, mapping, context, query)
            self.reader.ensure_unchanged()
            result.metadata.provenance.filters.update(
                {
                    "selection": manifest.selection,
                    "manifest_sha256": self.reader.manifest_record.sha256,
                    "pipeline_version": manifest.pipeline_version,
                    "source_reports": {
                        k: v.model_dump() for k, v in manifest.source_reports.items()
                    },
                    "source_files": {k: v.model_dump() for k, v in manifest.source_files.items()},
                }
            )
            observation.finish(
                ObservationUpdate(
                    output_data={
                        "observed_genes": result.observed_gene_count,
                        "observed_families": result.observed_family_count,
                    }
                )
            )
            return result
