"""
归档执行模块
- 安全操作：dry-run / 移动 / 复制
- 重名自动处理：加序号绝不覆盖
- 原始文件保护：先归档再清理，移动而非删除
"""

import shutil
import json
from pathlib import Path
from typing import List, Dict, Optional, Callable, Tuple
from dataclasses import dataclass, asdict, field

from .models import PhotoInfo, DuplicateGroup, RenamePlan, ActionPlan
from .exif_utils import TemplateEngine


class ConflictPolicy:
    SKIP = "skip"
    RENAME = "rename"
    FAIL = "fail"


@dataclass
class ExecutionResult:
    archive_root: Path
    duplicates_root: Optional[Path]
    operations: List[Dict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    archived_count: int = 0
    duplicated_moved_count: int = 0
    skipped_count: int = 0
    bytes_saved: int = 0

    def to_dict(self) -> Dict:
        return {
            "archive_root": str(self.archive_root),
            "duplicates_root": str(self.duplicates_root) if self.duplicates_root else None,
            "operations": self.operations,
            "errors": self.errors,
            "archived_count": self.archived_count,
            "duplicated_moved_count": self.duplicated_moved_count,
            "skipped_count": self.skipped_count,
            "bytes_saved": self.bytes_saved,
        }


class Archiver:
    """照片归档执行器"""

    def __init__(
        self,
        archive_root: Path,
        duplicates_dir: Optional[Path] = None,
        move_duplicates: bool = True,
        conflict_policy: str = ConflictPolicy.RENAME,
        dry_run: bool = True,
        template_engine: Optional[TemplateEngine] = None,
        progress_callback: Optional[Callable[[str, int, int, Path], None]] = None,
    ):
        self.archive_root = Path(archive_root).resolve()
        if duplicates_dir:
            self.duplicates_root = Path(duplicates_dir).resolve()
        else:
            self.duplicates_root = self.archive_root.parent / (
                self.archive_root.name + "_duplicates"
            )
        self.move_duplicates = move_duplicates
        self.conflict_policy = conflict_policy
        self.dry_run = dry_run
        self.template_engine = template_engine or TemplateEngine()
        self.progress_callback = progress_callback
        self._reserved_paths: set = set()

    def generate_rename_plans(
        self, photos: List[PhotoInfo]
    ) -> List[RenamePlan]:
        """为唯一照片生成重命名+归档计划"""
        self.template_engine.reset_counters()
        plans: List[RenamePlan] = []

        for photo in photos:
            if not photo.is_valid:
                continue
            target = self.template_engine.render_full_path(photo, self.archive_root)
            target = self._resolve_conflict(target)
            plans.append(
                RenamePlan(
                    source=photo.path,
                    target=target,
                    reason="rename_and_archive",
                )
            )
        return plans

    def build_action_plan(
        self,
        photos: List[PhotoInfo],
        dedupe_groups: List[DuplicateGroup],
    ) -> ActionPlan:
        """构建完整操作计划：去重分组 + 重命名归档"""
        plan = ActionPlan()
        plan.duplicate_groups = list(dedupe_groups)

        primary_paths: set = set()
        for g in dedupe_groups:
            primary_paths.add(g.primary.path)

        unique_photos: List[PhotoInfo] = []
        grouped_paths: set = set()
        for g in dedupe_groups:
            for p in g.photos:
                grouped_paths.add(p.path)

        for g in dedupe_groups:
            if g.primary.path not in unique_photos:
                unique_photos.append(g.primary)

        for p in photos:
            if p.path not in grouped_paths:
                if p.is_valid:
                    unique_photos.append(p)
                else:
                    plan.skipped.append(p)
            elif p.path in primary_paths and not p.is_valid:
                plan.skipped.append(p)

        plan.rename_plans = self.generate_rename_plans(unique_photos)

        for p in photos:
            if not p.is_valid and p not in plan.skipped:
                plan.skipped.append(p)

        return plan

    def apply_plan(
        self,
        plan: ActionPlan,
        duplicate_action: str = "move",
    ) -> ExecutionResult:
        """执行操作计划。duplicate_action: move/keep/delete"""
        result = ExecutionResult(
            archive_root=self.archive_root,
            duplicates_root=self.duplicates_root if duplicate_action == "move" else None,
        )
        self._reserved_paths.clear()

        total = len(plan.rename_plans) + sum(
            len(g.photos) - 1 for g in plan.duplicate_groups
        )
        current = 0

        for idx, rename_plan in enumerate(plan.rename_plans, 1):
            current += 1
            if self.progress_callback:
                self.progress_callback("archive", current, total, rename_plan.source)

            try:
                if self.dry_run:
                    op = self._op_record(
                        "archive", rename_plan.source, rename_plan.target
                    )
                    result.operations.append(op)
                    result.archived_count += 1
                    self._reserved_paths.add(str(rename_plan.target.resolve()))
                else:
                    self._ensure_dir(rename_plan.target.parent)
                    self._safe_move_or_copy(rename_plan.source, rename_plan.target)
                    op = self._op_record(
                        "archive", rename_plan.source, rename_plan.target
                    )
                    result.operations.append(op)
                    result.archived_count += 1
            except Exception as e:
                result.errors.append(
                    f"归档失败 {rename_plan.source} -> {rename_plan.target}: {e}"
                )

        if duplicate_action != "keep":
            for gi, group in enumerate(plan.duplicate_groups):
                for dup in group.duplicates:
                    current += 1
                    if self.progress_callback:
                        self.progress_callback(
                            "duplicate", current, total, dup.path
                        )

                    try:
                        if duplicate_action == "move":
                            dup_target = self._duplicate_target(dup, group.group_id, gi)
                            if self.dry_run:
                                op = self._op_record(
                                    "move_duplicate", dup.path, dup_target
                                )
                                result.operations.append(op)
                                result.duplicated_moved_count += 1
                                result.bytes_saved += dup.size
                            else:
                                self._ensure_dir(dup_target.parent)
                                self._safe_move_or_copy(dup.path, dup_target)
                                op = self._op_record(
                                    "move_duplicate", dup.path, dup_target
                                )
                                result.operations.append(op)
                                result.duplicated_moved_count += 1
                                result.bytes_saved += dup.size
                        elif duplicate_action == "delete":
                            if self.dry_run:
                                op = self._op_record("delete", dup.path, None)
                                result.operations.append(op)
                                result.bytes_saved += dup.size
                            else:
                                dup.path.unlink()
                                op = self._op_record("delete", dup.path, None)
                                result.operations.append(op)
                                result.bytes_saved += dup.size
                    except Exception as e:
                        result.errors.append(
                            f"处理重复文件失败 {dup.path}: {e}"
                        )

        for skip_p in plan.skipped:
            result.skipped_count += 1
            err_msg = skip_p.error or "无效或损坏的图片"
            result.errors.append(f"跳过 {skip_p.path}: {err_msg}")

        return result

    def save_report(
        self,
        result: ExecutionResult,
        report_path: Path,
        plan: Optional[ActionPlan] = None,
    ) -> Path:
        """保存操作报告为 JSON"""
        report = {
            "dry_run": self.dry_run,
            "execution": result.to_dict(),
        }
        if plan:
            report["plan_summary"] = {
                "duplicate_groups_count": len(plan.duplicate_groups),
                "total_duplicates_found": plan.total_duplicates,
                "total_saved_size_estimate": plan.total_saved_size,
                "rename_count": len(plan.rename_plans),
                "skipped_count": len(plan.skipped),
                "errors_in_plan": plan.errors,
            }
            dup_details = []
            for g in plan.duplicate_groups:
                dup_details.append({
                    "group_id": g.group_id,
                    "primary": str(g.primary.path),
                    "primary_resolution": f"{g.primary.width}x{g.primary.height}",
                    "primary_size": g.primary.size,
                    "similarity": g.similarity_score,
                    "duplicates": [
                        {
                            "path": str(p.path),
                            "size": p.size,
                            "resolution": f"{p.width}x{p.height}",
                        }
                        for p in g.duplicates
                    ],
                })
            report["duplicate_groups"] = dup_details

            rename_details = [
                {
                    "source": str(rp.source),
                    "target": str(rp.target),
                }
                for rp in plan.rename_plans
            ]
            report["rename_plans"] = rename_details

        report_path = Path(report_path)
        self._ensure_dir(report_path.parent)
        if not self.dry_run or report_path.parent.exists():
            try:
                with open(report_path, "w", encoding="utf-8") as f:
                    json.dump(report, f, ensure_ascii=False, indent=2)
            except Exception:
                if not self.dry_run:
                    raise
        return report_path

    def _resolve_conflict(self, target: Path) -> Path:
        """解决目标路径重名问题，按策略加序号"""
        candidate = target
        counter = 1

        while True:
            key = str(candidate.resolve())
            if key in self._reserved_paths:
                pass
            elif not candidate.exists():
                return candidate
            else:
                pass

            if self.conflict_policy == ConflictPolicy.SKIP:
                return candidate
            elif self.conflict_policy == ConflictPolicy.FAIL:
                raise FileExistsError(f"目标文件已存在: {candidate}")

            stem = candidate.stem
            suffix = candidate.suffix
            parent = candidate.parent
            counter += 1
            candidate = parent / f"{stem}_{counter:03d}{suffix}"

    def _duplicate_target(
        self, photo: PhotoInfo, group_id: int, dup_idx: int
    ) -> Path:
        """生成重复文件的归档目标路径"""
        sub = self.template_engine.render_archive_subdir(photo)
        target_dir = self.duplicates_root / sub / f"group_{group_id:05d}"
        safe_name = f"dup_{dup_idx:03d}_{photo.path.name}"
        target = target_dir / safe_name
        return self._resolve_conflict(target)

    def _ensure_dir(self, directory: Path):
        """确保目录存在"""
        if not self.dry_run:
            directory.mkdir(parents=True, exist_ok=True)

    def _safe_move_or_copy(self, source: Path, target: Path):
        """安全地移动或复制文件，绝不覆盖"""
        if target.exists():
            target = self._resolve_conflict(target)
        try:
            if source.resolve() == target.resolve():
                return
            shutil.move(str(source), str(target))
        except Exception as e:
            if not target.exists():
                try:
                    shutil.copy2(str(source), str(target))
                    source.unlink()
                except Exception as e2:
                    raise RuntimeError(f"移动/复制失败: {e}, {e2}") from e

    def _op_record(self, kind: str, src: Path, dst: Optional[Path]) -> Dict:
        return {
            "type": kind,
            "source": str(src),
            "target": str(dst) if dst else None,
            "dry_run": self.dry_run,
        }
