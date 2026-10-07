"""离线准备论文环境资料与有证据的跨库映射；应用请求不允许指定文件或下载地址。"""

import argparse
import json
from pathlib import Path
from urllib.request import urlopen

from tara_agent.data.function_reference import AUTHOR_COMMIT
from tara_agent.data.research_context import prepare_research


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processed-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--environment", type=Path)
    parser.add_argument("--environment-source")
    parser.add_argument("--units-json", type=Path)
    parser.add_argument("--download-paper-environment", action="store_true")
    parser.add_argument("--download-dir", type=Path)
    parser.add_argument("--sample-mapping", type=Path)
    parser.add_argument("--mapping-source")
    parser.add_argument("--mapping-version")
    parser.add_argument("--matou-dir", type=Path)
    args = parser.parse_args()
    units = json.loads(args.units_json.read_text(encoding="utf-8")) if args.units_json else None
    if args.download_paper_environment:
        if args.environment or args.units_json or args.download_dir is None:
            parser.error("论文下载需要独立--download-dir，不与自定义环境表和单位文件混用")
        args.download_dir.mkdir(parents=True, exist_ok=False)
        url = f"https://raw.githubusercontent.com/JJPierellaKarlusich/Diatom_patters/{AUTHOR_COMMIT}/metaB/datasets/physicochemistry.tsv"
        with urlopen(url, timeout=120) as response:
            data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("环境资料超过下载保护上限")
        args.environment = args.download_dir / "physicochemistry.tsv"
        args.environment.write_bytes(data)
        args.environment_source = url
        # 作者表未逐列附单位时显式保留不确定性，不把模型值等同于实测营养盐。
        columns = data.decode("utf-8-sig").splitlines()[0].split("\t")
        units = {
            c: "not_reported_in_author_table; see PANGAEA.875582 and source methods"
            for c in columns
            if c not in {"station", "depth"}
        }
        units.update(
            Temperature="degree_Celsius",
            abslat="degree",
            ChlorophyllA="mg_m-3",
            **{"NH4toDIN.5m": "dimensionless"},
        )
    prepare_research(
        args.processed_dir,
        args.output_dir,
        environment=args.environment,
        environment_source=args.environment_source,
        units=units,
        sample_mapping=args.sample_mapping,
        mapping_source=args.mapping_source,
        mapping_version=args.mapping_version,
        matou_dir=args.matou_dir,
    )
    print(
        json.dumps({"status": "prepared", "output_dir": str(args.output_dir)}, ensure_ascii=False)
    )


if __name__ == "__main__":
    main()
