"""样本组成与站点描述统计；复用已校验矩阵，不从名称猜样本或分类层级。"""

import re
from collections import defaultdict

from tara_agent.analysis.compute_models import (
    StationAbundanceSummary,
    TaxonAbundanceQuery,
    TaxonCompositionObservation,
    TaxonCompositionSummary,
)
from tara_agent.analysis.taxonomy import taxonomy_predicate

TAXONOMY_LAYOUT = "Root;kingdom;supergroup;division;class;order;family;genus;species"


def _classification(taxonomy, amplicon, rank):
    if rank == "asv":
        return amplicon, "asv", taxonomy
    levels = taxonomy.split(";")
    position = {"genus": 7, "species": 8}[rank]
    # 当前 Tara 文件为 Root 加 PR2 八级结构。截短、占位和其他结构不能冒充属或种。
    supported = levels[0] == "Root" and len(levels) <= 9
    label = levels[position] if supported and len(levels) > position else ""
    unresolved = (
        not label
        or label.lower().startswith(("unclassified", "unknown", "uncultured"))
        or re.search(r"_X+$|_sp\.?$", label, re.IGNORECASE)
    )
    path = ";".join(levels[: position + 1]) if supported else taxonomy
    if unresolved or not supported:
        return f"未鉴定到{'属' if rank == 'genus' else '种'}（{path}）", "unresolved", path
    return label, "assigned", path


def sample_composition(reader, query: TaxonAbundanceQuery, observations):
    samples = [o.sample_id for o in observations]
    if not samples:
        return [], []
    frame = reader.collect(
        reader.scan_asv_metadata(query.marker)
        .filter(taxonomy_predicate(query.taxon, query.match_mode))
        .select("amplicon", "taxonomy")
        .join(reader.scan_abundance(query.marker, samples), on="amplicon", validate="1:1"),
        name="读取样本内类群组成",
        artifacts=[f"{query.marker.value}_metadata", f"{query.marker.value}_abundance"],
        filters=query.model_dump(mode="json"),
        marker=query.marker,
        streaming=True,
    )
    # 同名但不同祖先路径保留为不同分类，未鉴定记录保留其实际分类范围。
    groups = defaultdict(lambda: {"counts": dict.fromkeys(samples, 0), "asvs": defaultdict(int)})
    for row in frame.iter_rows(named=True):
        label, status, path = _classification(
            row["taxonomy"], row["amplicon"], query.taxonomic_rank
        )
        group = groups[(label, status, path)]
        for sample in samples:
            count = row[sample]
            if count > 0:
                group["counts"][sample] += count
                group["asvs"][sample] += 1
    rows, summaries = [], []
    for observation in observations:
        start = len(rows)
        sample = observation.sample_id
        ordered = sorted(
            [(key, value) for key, value in groups.items() if value["counts"][sample] > 0],
            key=lambda item: (-item[1]["counts"][sample], item[0]),
        )
        cumulative = 0
        unresolved = 0
        for index, ((label, status, path), value) in enumerate(ordered, 1):
            count = value["counts"][sample]
            cumulative += count
            if status == "unresolved":
                unresolved += count
            if query.offset <= index - 1 < query.offset + query.limit:
                rows.append(
                    TaxonCompositionObservation(
                        sample_id=sample,
                        rank=index,
                        taxon_label=label,
                        classification_status=status,
                        read_count=count,
                        relative_abundance=count / observation.sample_total_read_count,
                        fraction_within_selected_taxon=count / observation.taxon_read_count,
                        cumulative_fraction=cumulative / observation.taxon_read_count,
                        asv_count=value["asvs"][sample],
                        taxonomy=path,
                    )
                )
        summaries.append(
            TaxonCompositionSummary(
                sample_id=sample,
                selected_taxon_read_count=observation.taxon_read_count,
                sample_total_read_count=observation.sample_total_read_count,
                total_groups=len(ordered),
                returned_groups=len(rows) - start,
                unresolved_read_count=unresolved,
                dominant_taxon=ordered[0][0][0] if ordered else None,
                dominant_fraction=ordered[0][1]["counts"][sample] / observation.taxon_read_count
                if ordered
                else None,
            )
        )
    return rows, summaries


def station_abundance(reader, observations, aggregation):
    context = (
        reader.load_sample_context([o.sample_id for o in observations]) if observations else None
    )
    stations = (
        {}
        if context is None
        else {row["sample_id_pangaea"]: row["station"] for row in context.iter_rows(named=True)}
    )
    groups = defaultdict(list)
    excluded = []
    for observation in observations:
        station = stations.get(observation.sample_id)
        if not station or not station.strip():
            excluded.append(observation.sample_id)
        else:
            groups[station].append(observation)
    rows = []
    for station, items in groups.items():
        valid = [o.relative_abundance for o in items if o.relative_abundance is not None]
        rows.append(
            StationAbundanceSummary(
                station=station,
                mean_rank=None,
                max_rank=None,
                mean_relative_abundance=sum(valid) / len(valid) if valid else None,
                max_relative_abundance=max(valid) if valid else None,
                sample_count=len(items),
                valid_sample_count=len(valid),
                sample_ids=[o.sample_id for o in items],
            )
        )
    for statistic in ("mean", "max"):
        key = f"{statistic}_relative_abundance"
        ordered = sorted(
            rows, key=lambda r: (getattr(r, key) is None, -(getattr(r, key) or 0), r.station)
        )
        previous, rank = None, 0
        for index, row in enumerate(ordered, 1):
            value = getattr(row, key)
            if value is not None:
                if value != previous:
                    rank = index
                setattr(row, f"{statistic}_rank", rank)
                previous = value
    key = f"{aggregation}_relative_abundance"
    rows.sort(key=lambda r: (getattr(r, key) is None, -(getattr(r, key) or 0), r.station))
    return rows, excluded
