"""从本地作者文件建立论文参考资料包；可选下载固定版本的公开文件。"""

import argparse
from pathlib import Path

import httpx

from tara_agent.data.function_reference import AUTHOR_COMMIT, prepare_reference

FILES = [
    "scripts/contextual_data/PfamA.list",
    "scripts/contextual_data/station_ocean.tsv",
    "scripts/contextual_data/physicochemistry.for.metaT.tsv",
    "scripts/contextual_data/station_Lat_Long_uniq.withTaraPrefix.tsv",
    "datasets/Pfam_sums/metaG/Bacillariophyta.MATOU-v1.5.Pfam.metaG.tsv.gz",
    "datasets/Pfam_sums/metaT/Bacillariophyta.MATOU-v1.5.Pfam.metaT.tsv.gz",
    "datasets/LHC/LHC.diatoms.MATOUv1.5.seqs.function.tsv.gz",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    parser.add_argument(
        "--matou-dir",
        type=Path,
        help="可选：用当前MATOU逐基因数据预计算LHC亚家族；仅在独立准备阶段扫描",
    )
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error("输出目录已存在，不覆盖")
    if args.download:
        args.source_dir.mkdir(parents=True, exist_ok=False)
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            for name in FILES:
                url = f"https://raw.githubusercontent.com/JJPierellaKarlusich/Diatom_patters/{AUTHOR_COMMIT}/metaT/{name}"
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    total = 0
                    with (args.source_dir / Path(name).name).open("xb") as stream:
                        for block in response.iter_bytes():
                            total += len(block)
                            if total > 128 * 1024 * 1024:
                                raise ValueError("参考文件超过128MiB保护上限")
                            stream.write(block)
                print(Path(name).name, flush=True)
    prepare_reference(args.source_dir, args.output_dir, matou_dir=args.matou_dir)
    print(args.output_dir / "manifest.json")


if __name__ == "__main__":
    main()
