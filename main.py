from __future__ import annotations

import argparse
import sys
from pathlib import Path

from jx3_analyzer import __version__
from jx3_analyzer.core import AnalyzerPaths, analyze_ids_file, discover_game_root
from jx3_analyzer.package_workflow import analyze_selected_dungeons, finalize_analysis_output, load_package_catalog


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="剑网3 V4 副本技能、NPC、交互物品与官方资料解析器")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--no-gui", action="store_true", help="使用命令行模式")
    parser.add_argument("--bin64", help="游戏 JX3ClientX64.exe 所在目录；启用自动解包模式")
    parser.add_argument("--dungeon", action="append", help="要分析的副本名，可重复指定")
    parser.add_argument("--map-id", action="append", help="MapList 地图ID，可重复指定或以逗号分隔")
    parser.add_argument("--list-dungeons", action="store_true", help="读取并列出 V4 副本、难度及地图ID")
    parser.add_argument("--root", help="解包后的游戏本地包根目录")
    parser.add_argument("--ids", help="技能ID文本文件")
    parser.add_argument("--output", help="结果存放位置（每次运行自动新建时间目录）")
    parser.add_argument("--skills-tab", help="自定义 skills.tab 路径")
    parser.add_argument("--buff-tab", help="自定义 buff.tab 路径")
    parser.add_argument("--skill-ui", help="自定义 UI skill.txt 路径")
    parser.add_argument("--buff-ui", help="自定义 UI buff.txt 路径")
    parser.add_argument("--decompiler", help="自定义 unluac.exe 路径")
    return parser


def run_cli(args: argparse.Namespace) -> int:
    def report(current: int, total: int, message: str) -> None:
        print(f"[{current}/{total}] {message}", flush=True)

    if args.bin64:
        dungeon_names = [
            name.strip()
            for group in (args.dungeon or [])
            for name in group.replace("，", ",").split(",")
            if name.strip()
        ]
        map_ids = [value.strip() for group in (args.map_id or []) for value in group.replace("，", ",").split(",") if value.strip()]
        if not args.list_dungeons and (not (dungeon_names or map_ids) or not args.output):
            raise ValueError("自动模式需要 --bin64、--output，以及 --dungeon 或 --map-id。")
        catalog = load_package_catalog(args.bin64, progress=report)
        if args.list_dungeons:
            for item in catalog.dungeons:
                print(f"{item.display_name}\t地图ID={','.join(item.map_ids) or '未关联'}\t技能目录={item.skill_folder}\t技能={item.skill_count}")
            return 0
        result = analyze_selected_dungeons(catalog, dungeon_names, args.output, report, selected_map_ids=map_ids)
        print(
            f"完成：成功 {result.successful_skills}，失败 {result.failed_skills}，"
            f"关联 Buff {result.buff_links}。汇总表：{getattr(result, 'workbook', result.skill_csv)}"
        )
        return 0 if result.failed_skills == 0 else 2

    discovered = discover_game_root()
    root = args.root or (str(discovered) if discovered else "")
    if not root or not args.ids or not args.output:
        raise ValueError("命令行模式必须提供 --root、--ids 和 --output（root 可在当前目录自动识别）。")
    paths = AnalyzerPaths.from_root(
        root,
        skills_tab=args.skills_tab,
        buff_tab=args.buff_tab,
        skill_ui=args.skill_ui,
        buff_ui=args.buff_ui,
        decompiler=args.decompiler,
    )

    result = analyze_ids_file(paths, Path(args.ids), Path(args.output), report)
    result = finalize_analysis_output(result)
    print(
        f"完成：成功 {result.successful_skills}，失败 {result.failed_skills}，"
        f"关联 Buff {result.buff_links}。汇总表：{getattr(result, 'workbook', result.skill_csv)}"
    )
    return 0 if result.failed_skills == 0 else 2


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    use_cli = args.no_gui or any((args.bin64, args.dungeon, args.map_id, args.list_dungeons, args.root, args.ids, args.output))
    if use_cli:
        try:
            return run_cli(args)
        except Exception as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
    from jx3_analyzer.gui import launch_gui

    launch_gui()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
