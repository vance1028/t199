"""
操作报告生成模块
- 扫描报告：扫描了多少张
- 重复报告：多少组、省了多少空间
- 重命名归档报告
- 跳过/错误清单
- 控制台友好格式 + JSON
"""

import sys
import json
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime

from .models import PhotoInfo, DuplicateGroup, RenamePlan, ActionPlan
from .archiver import ExecutionResult


def _fmt_size(num_bytes: int) -> str:
    """格式化字节大小为可读字符串"""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    for unit in ["KB", "MB", "GB", "TB"]:
        num_bytes /= 1024
        if num_bytes < 1024:
            return f"{num_bytes:.2f} {unit}"
    return f"{num_bytes:.2f} PB"


def print_scan_report(
    photos: List[PhotoInfo],
    file=None,
):
    """打印扫描阶段报告"""
    f = file or sys.stdout
    total = len(photos)
    valid = [p for p in photos if p.is_valid]
    invalid = [p for p in photos if not p.is_valid]
    total_size = sum(p.size for p in valid)

    print("=" * 60, file=f)
    print("  扫描报告", file=f)
    print("=" * 60, file=f)
    print(f"  扫描目录中文件总数: {total}", file=f)
    print(f"  有效图片数:        {len(valid)}", file=f)
    print(f"  无效/损坏:         {len(invalid)}", file=f)
    print(f"  总占用空间:        {_fmt_size(total_size)}", file=f)

    ext_stats: Dict[str, int] = {}
    for p in valid:
        ext = p.path.suffix.lower()
        ext_stats[ext] = ext_stats.get(ext, 0) + 1
    if ext_stats:
        print("", file=f)
        print("  按扩展名分布:", file=f)
        for ext, cnt in sorted(ext_stats.items()):
            print(f"    {ext:10s} : {cnt}", file=f)

    print("", file=f)
    print("=" * 60, file=f)


def print_dedupe_report(
    groups: List[DuplicateGroup], unique: List[PhotoInfo], file=None
):
    """打印去重阶段报告"""
    f = file or sys.stdout
    total_groups = len(groups)
    total_dupes = sum(len(g.photos) - 1 for g in groups)
    saved_bytes = sum(g.saved_size for g in groups)

    print("=" * 60, file=f)
    print("  去重报告", file=f)
    print("=" * 60, file=f)
    print(f"  发现重复组:        {total_groups}", file=f)
    print(f"  冗余文件数:        {total_dupes}", file=f)
    print(f"  预计节省空间:      {_fmt_size(saved_bytes)}", file=f)
    print(f"  唯一保留文件数:    {len(unique)}", file=f)

    if groups:
        print("", file=f)
        print("  重复组详情 (前20组):", file=f)
        for g in groups[:20]:
            p = g.primary
            tag = "完全相同" if g.similarity_score == 0 else f"近似(~{g.similarity_score})"
            header = "组{} [{}]".format(g.group_id, tag)
            print(
                f"  {header} "
                f"主文件: {p.path.name} "
                f"({p.width}x{p.height} {_fmt_size(p.size)}) "
                f"+{len(g.duplicates)}张重复",
                file=f,
            )
        if len(groups) > 20:
            print(f"  ... 以及另外 {len(groups) - 20} 组省略", file=f)

    print("", file=f)
    print("=" * 60, file=f)


def print_plan_report(
    plan: ActionPlan,
    file=None,
):
    """打印操作计划报告 (dry-run)"""
    f = file or sys.stdout

    print("=" * 60, file=f)
    print("  操作计划报告 (DRY RUN - 不执行)", file=f)
    print("=" * 60, file=f)

    print(f"  [归档]", file=f)
    print(f"    将归档文件数:    {len(plan.rename_plans)}", file=f)

    print("", file=f)
    print(f"  [去重]", file=f)
    print(f"    重复组数量:      {len(plan.duplicate_groups)}", file=f)
    print(f"    冗余文件数:    {plan.total_duplicates}", file=f)
    print(f"    预计节省:       {_fmt_size(plan.total_saved_size)}", file=f)

    if plan.skipped:
        print("", file=f)
        print(f"  [跳过]", file=f)
        print(f"    跳过/损坏文件数:  {len(plan.skipped)}", file=f)

    if plan.rename_plans:
        print("", file=f)
        print("  重命名归档计划 (前20项):", file=f)
        for rp in plan.rename_plans[:20]:
            print(f"    {rp.source.name}", file=f)
            print(f"      -> {rp.target}", file=f)
        if len(plan.rename_plans) > 20:
            print(f"    ... 另外 {len(plan.rename_plans) - 20} 项省略", file=f)

    if plan.skipped:
        print("", file=f)
        print("  跳过/损坏清单 (前20项):", file=f)
        for s in plan.skipped[:20]:
            err = s.error or "未知原因"
            print(f"    {s.path}: {err}", file=f)
        if len(plan.skipped) > 20:
            print(f"    ... 另外 {len(plan.skipped) - 20} 项省略", file=f)

    print("", file=f)
    print("=" * 60, file=f)


def print_execution_report(
    result: ExecutionResult,
    file=None,
):
    """打印执行结果报告"""
    f = file or sys.stdout

    mode_tag = " [DRY RUN]" if result.operations and result.operations[0].get("dry_run") else ""
    print("=" * 60, file=f)
    print(f"  执行结果报告{mode_tag}", file=f)
    print("=" * 60, file=f)
    print(f"  归档文件数:      {result.archived_count}", file=f)
    print(f"  处理重复文件数:    {result.duplicated_moved_count}", file=f)
    print(f"  跳过文件数:        {result.skipped_count}", file=f)
    print(f"  释放/节省空间:    {_fmt_size(result.bytes_saved)}", file=f)
    if result.archive_root:
        print(f"  归档目录:         {result.archive_root}", file=f)
    if result.duplicates_root:
        print(f"  重复文件目录:     {result.duplicates_root}", file=f)

    ops_by_type: Dict[str, int] = {}
    for op in result.operations:
        t = op.get("type", "?")
        ops_by_type[t] = ops_by_type.get(t, 0) + 1
    if ops_by_type:
        print("", file=f)
        print("  操作统计:", file=f)
        for t, c in ops_by_type.items():
            print(f"    {t}: {c}", file=f)

    if result.errors:
        print("", file=f)
        print(f"  警告/错误: 共 {len(result.errors)} 项 (前20条):", file=f)
        for err in result.errors[:20]:
            print(f"    - {err}", file=f)
        if len(result.errors) > 20:
            print(f"    ... 另外 {len(result.errors) - 20} 条省略", file=f)

    print("", file=f)
    print("=" * 60, file=f)
