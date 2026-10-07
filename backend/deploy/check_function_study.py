"""独立执行论文功能对照工具，导出完整JSON、CSV与图表数据供审核。"""

import argparse
import csv
import json
from pathlib import Path

from tara_agent.agent.charts import build_charts
from tara_agent.agent.models import ToolName
from tara_agent.analysis.function_study import FunctionStudyService
from tara_agent.analysis.function_study_models import FunctionStudyQuery
from tara_agent.data.function_reference import FunctionReference
from tara_agent.data.matou_reader import MatouDataReader


def write_csv(path, rows):
    if not rows:
        return
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(
            {
                k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
                for k, v in row.items()
            }
            for row in rows
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", choices=["current_data", "paper_reference"], default="current_data"
    )
    parser.add_argument("--matou-dir", type=Path)
    parser.add_argument(
        "--normalization", choices=["taxon_total", "author_script"], default="taxon_total"
    )
    parser.add_argument("--reference-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.source == "current_data" and args.matou_dir is None:
        parser.error("当前数据计算需要 --matou-dir")
    if args.matou_dir is None and args.reference_dir is None:
        parser.error("需要 --reference-dir 或 --matou-dir")
    if args.output_dir.exists():
        parser.error("输出目录已存在，不覆盖")
    service = FunctionStudyService(
        MatouDataReader(args.matou_dir) if args.matou_dir else None,
        FunctionReference(args.reference_dir) if args.reference_dir else None,
    )
    result = service.analyze(
        FunctionStudyQuery(source=args.source, normalization=args.normalization)
    )
    args.output_dir.mkdir(parents=True)
    (args.output_dir / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    data = result.model_dump(mode="json")
    for key in (
        "sections",
        "function_ranks",
        "size_distribution",
        "ocean_distribution",
        "target_signals",
        "excluded_samples",
    ):
        write_csv(args.output_dir / (key + ".csv"), data[key])
    for p in data["pls_results"]:
        for key in ("coordinates", "scores", "explained_variance", "input_rows"):
            write_csv(args.output_dir / f"figure_{p['figure']}_{key}.csv", p[key])
    (args.output_dir / "charts.json").write_text(
        json.dumps(
            [c.model_dump(mode="json") for c in build_charts(ToolName.FUNCTION_STUDY, data)],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": result.status,
                "functions": len(result.function_ranks),
                "top_contribution_sum": result.top_contribution_sum,
                "sections": data["sections"],
                "output": str(args.output_dir),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
