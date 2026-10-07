"""通过既有SSH隧道独立验收三个科研任务；不调用模型，不读取本地科学数据。"""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from tara_agent.agent.charts import build_charts
from tara_agent.agent.gateway import AgentToolError
from tara_agent.agent.models import ToolName
from tara_agent.agent.multistep import validate_result
from tara_agent.config import Settings
from tara_agent.data_service.client import RemoteToolGateway
from tara_agent.observability.execution import bind_execution_trace


async def verify(gateway, pfam, sample_ids=None):
    tasks = [
        (ToolName.COMMUNITY_ANALYSIS, {"sample_ids": sample_ids}),
        (ToolName.FUNCTION_STUDY, {"normalization": "taxon_total"}),
        (ToolName.FUNCTION_ENVIRONMENT, {"pfam_accession": pfam}),
    ]
    available = {tool.name for tool in await gateway.list_tools()}
    outputs, report = {}, []
    for tool, query in tasks:
        if tool not in available:
            report.append({"tool": tool.value, "status": "blocked", "reason": "工具未注册"})
            continue
        events = []
        try:
            with bind_execution_trace(parent_id=str(uuid4()), writer=events.append):
                result = validate_result(tool, await gateway.call(tool, {"query": query}))
        except (AgentToolError, ValueError) as error:
            report.append({"tool": tool.value, "status": "blocked", "reason": str(error)})
            continue
        if not any(event.get("span_kind") == "data" for event in events):
            raise ValueError(tool.value + "缺少同树数据来源节点")
        outputs[tool.value] = {
            "result": result,
            "charts": [chart.model_dump(mode="json") for chart in build_charts(tool, result)],
            "observations": events,
        }
        report.append(
            {
                "tool": tool.value,
                "status": result["status"],
                "sections": result.get("sections"),
                "mapping_coverage": result.get("mapping_coverage"),
                "warnings": result["metadata"]["warnings"],
            }
        )
    return outputs, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pfam", default="PF03382")
    parser.add_argument(
        "--sample-id", action="append", help="可选任务一样本范围；不改变其他任务范围"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error("输出目录已存在，不覆盖")
    outputs, report = asyncio.run(verify(RemoteToolGateway(Settings()), args.pfam, args.sample_id))
    args.output_dir.mkdir(parents=True, exist_ok=False)
    for name, output in outputs.items():
        (args.output_dir / (name + ".json")).write_text(
            json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    (args.output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "completed"
                if all(r["status"] == "completed" for r in report)
                else "partial",
                "tasks": report,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
