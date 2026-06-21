"""
CLI 子命令入口
子命令:
  scan   - 扫描目录并展示统计
  plan   - 生成完整操作计划 (默认 dry-run)
  apply  - 执行计划并生成报告
"""

import argparse
import sys
from pathlib import Path
from typing import List

from .scanner import Scanner
from .hashing import deduplicate_photos
from .exif_utils import (
    TemplateEngine,
    DEFAULT_NAME_TEMPLATE,
    DEFAULT_ARCHIVE_TEMPLATE,
)
from .archiver import Archiver, ConflictPolicy
from .reports import (
    print_scan_report,
    print_dedupe_report,
    print_plan_report,
    print_execution_report,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="photo-organizer",
        description="照片整理自动化工具：去重、重命名、归档",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="详细输出"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="扫描目录并展示统计")
    _add_source_args(scan_parser)
    _add_hash_args(scan_parser)

    plan_parser = subparsers.add_parser(
        "plan", help="生成操作计划 (dry-run，不改动磁盘"
    )
    _add_source_args(plan_parser)
    _add_hash_args(plan_parser)
    _add_template_args(plan_parser)
    _add_dedupe_args(plan_parser)
    plan_parser.add_argument(
        "--output", "-o", type=Path, default=None, help="JSON 报告输出路径")

    apply_parser = subparsers.add_parser("apply", help="执行整理操作")
    _add_source_args(apply_parser)
    _add_hash_args(apply_parser)
    _add_template_args(apply_parser)
    _add_dedupe_args(apply_parser)
    apply_parser.add_argument(
        "--archive-dir", "-a", type=Path, required=True, help="归档目标目录")
    apply_parser.add_argument(
        "--duplicates-dir", type=Path, default=None,
        help="重复文件放置目录 (默认: 归档目录同级 + _duplicates)")
    apply_parser.add_argument(
        "--duplicate-action", choices=["move", "keep", "delete"], default="move",
        help="重复文件处理方式 (默认: move 移动到重复目录)")
    apply_parser.add_argument(
        "--confirm", action="store_true", help="确认执行（不加此参数则只预览）")
    apply_parser.add_argument(
        "--report", type=Path, default=None, help="执行报告 JSON 路径")
    apply_parser.add_argument(
        "--conflict", choices=["rename", "skip", "fail"], default="rename",
        help="重名处理策略 (默认: rename 自动加序号)")
    apply_parser.add_argument(
        "--no-dry-run", action="store_true", help="（与--confirm等价）")

    return parser


def _add_source_args(p: argparse.ArgumentParser):
    p.add_argument("sources", nargs="+", type=Path, help="要扫描的源目录或文件")
    p.add_argument(
        "--extensions", nargs="*", default=None, help="图片扩展名 (默认 jpg/jpeg/png/...)")
    p.add_argument("--include-hidden", action="store_true", help="包含隐藏文件/目录")


def _add_hash_args(p: argparse.ArgumentParser):
    p.add_argument(
        "--use-phash", action="store_true", help="使用 pHash 代替 dHash 作为感知哈希")


def _add_template_args(p: argparse.ArgumentParser):
    p.add_argument(
        "--name-template", default=DEFAULT_NAME_TEMPLATE,
        help=f"文件名模板 (默认: {DEFAULT_NAME_TEMPLATE})")
    p.add_argument(
        "--archive-template", default=DEFAULT_ARCHIVE_TEMPLATE,
        help=f"归档子目录模板 (默认: {DEFAULT_ARCHIVE_TEMPLATE})")


def _add_dedupe_args(p: argparse.ArgumentParser):
    p.add_argument(
        "--no-dedupe", action="store_true", help="跳过去重，只做重命名归档")
    p.add_argument(
        "--similarity-threshold", type=int, default=10,
        help="感知哈希汉明距离阈值，越小越严格 (默认 10)")


def _progress(step, current, total, path):
    pct = (current / total * 100) if total > 0 else 0
    bar_len = 30
    filled = int(bar_len * current / total) if total > 0 else 0
    bar = "█" * filled + "░" * (bar_len - filled)
    name = path.name if hasattr(path, "name") else str(path)
    sys.stderr.write(
        f"\r  [{step:8s}] |{bar}| {current}/{total} ({pct:5.1f}%) {name[:40]:40s}"
    )
    if current >= total:
        sys.stderr.write("\n")
    sys.stderr.flush()


def cmd_scan(args) -> int:
    extensions = None
    if args.extensions:
        extensions = set(args.extensions)
    scanner = Scanner(
        extensions=extensions,
        skip_hidden=not args.include_hidden,
        use_phash=args.use_phash,
        progress_callback=_progress if args.verbose else None,
    )
    print(f"正在扫描: {', '.join(str(s) for s in args.sources)}")
    photos = scanner.scan(args.sources, compute_hashes=True)
    print_scan_report(photos)
    return 0


def cmd_plan(args) -> int:
    extensions = None
    if args.extensions:
        extensions = set(args.extensions)
    scanner = Scanner(
        extensions=extensions,
        skip_hidden=not args.include_hidden,
        use_phash=args.use_phash,
        progress_callback=_progress if args.verbose else None,
    )
    photos = scanner.scan(args.sources, compute_hashes=not args.no_dedupe)
    print_scan_report(photos)

    if not args.no_dedupe:
        groups, unique = deduplicate_photos(
            photos,
            similarity_threshold=args.similarity_threshold,
            use_phash=args.use_phash,
        )
        print_dedupe_report(groups, unique)
    else:
        groups, unique = [], [p for p in photos if p.is_valid]

    template = TemplateEngine(
        name_template=args.name_template,
        archive_template=args.archive_template,
    )
    archiver = Archiver(
        archive_root=Path("./.plan_preview"),
        dry_run=True,
        template_engine=template,
        progress_callback=_progress if args.verbose else None,
    )
    plan = archiver.build_action_plan(photos, groups)
    print_plan_report(plan)

    if args.output:
        from .models import ActionPlan
        import json

        def to_jsonable(obj):
            if hasattr(obj, "__dict__"):
                return obj.__dict__
            if isinstance(obj, Path):
                return str(obj)
            return str(obj)

        serializable = {
            "duplicate_groups": [],
            "rename_plans": [],
            "skipped": [],
        }
        for g in plan.duplicate_groups:
            serializable["duplicate_groups"].append({
                "group_id": g.group_id,
                "similarity": g.similarity_score,
                "primary": str(g.primary.path),
                "duplicates": [str(p.path) for p in g.duplicates],
            })
        for rp in plan.rename_plans:
            serializable["rename_plans"].append({
                "source": str(rp.source),
                "target": str(rp.target),
            })
        for s in plan.skipped:
            serializable["skipped"].append({
                "path": str(s.path),
                "error": s.error,
            })

        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
        print(f"\n计划 JSON 已保存到: {args.output}")
    return 0


def cmd_apply(args) -> int:
    dry_run = not (args.confirm or args.no_dry_run)

    if dry_run:
        print("! 注意：未指定 --confirm，本次为预览模式（dry-run）", file=sys.stderr)
        print("! 确认无误后加上 --confirm 才会真正执行\n", file=sys.stderr)

    extensions = None
    if args.extensions:
        extensions = set(args.extensions)
    scanner = Scanner(
        extensions=extensions,
        skip_hidden=not args.include_hidden,
        use_phash=args.use_phash,
        progress_callback=_progress if args.verbose else None,
    )
    print("步骤 1/4: 扫描文件...")
    photos = scanner.scan(args.sources, compute_hashes=not args.no_dedupe)
    print_scan_report(photos)

    groups: list = []
    unique_photos = [p for p in photos if p.is_valid]
    if not args.no_dedupe:
        print("\n步骤 2/4: 分析重复照片...")
        groups, unique_photos = deduplicate_photos(
            photos,
            similarity_threshold=args.similarity_threshold,
            use_phash=args.use_phash,
        )
        print_dedupe_report(groups, unique_photos)

    print("\n步骤 3/4: 生成归档计划...")
    template = TemplateEngine(
        name_template=args.name_template,
        archive_template=args.archive_template,
    )

    conflict_map = {
        "rename": ConflictPolicy.RENAME,
        "skip": ConflictPolicy.SKIP,
        "fail": ConflictPolicy.FAIL,
    }

    archiver = Archiver(
        archive_root=args.archive_dir,
        duplicates_dir=args.duplicates_dir,
        conflict_policy=conflict_map.get(args.conflict, ConflictPolicy.RENAME),
        dry_run=dry_run,
        template_engine=template,
        progress_callback=_progress if args.verbose else None,
    )
    plan = archiver.build_action_plan(photos, groups)
    print_plan_report(plan)

    if dry_run:
        print("\n! 以上为预览。确认无误后再次运行加上 --confirm 执行实际操作。")
        return 0

    print("\n步骤 4/4: 执行归档...")
    result = archiver.apply_plan(plan, duplicate_action=args.duplicate_action)

    report_path = args.report or (
        args.archive_dir.parent
        if args.archive_dir.parent.exists()
        else Path(".")
    ) / f"photo_organizer_report_{__import__('datetime').datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    archiver.save_report(result, report_path, plan)

    print_execution_report(result)
    print(f"\n详细报告已保存到: {report_path}")
    return 0


def main(argv: List[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "scan": cmd_scan,
        "plan": cmd_plan,
        "apply": cmd_apply,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 1
    try:
        return handler(args)
    except KeyboardInterrupt:
        print("\n操作已取消。", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"\n错误: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
