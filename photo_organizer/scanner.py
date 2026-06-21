"""
扫描器模块
- 递归遍历目录
- 识别图片文件（按扩展名）
- 计算哈希和填充 EXIF
"""

import os
import time
from pathlib import Path
from typing import List, Set, Callable, Optional, Iterable

from .models import PhotoInfo
from .hashing import compute_content_hash, compute_dhash, compute_phash
from .exif_utils import populate_photo_info


DEFAULT_IMAGE_EXTENSIONS: Set[str] = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".webp", ".heic", ".heif", ".raw", ".cr2", ".cr3", ".nef",
    ".arw", ".dng", ".orf", ".rw2", ".pef", ".raf", ".sr2",
}


class Scanner:
    """照片目录扫描器"""

    def __init__(
        self,
        extensions: Optional[Iterable[str]] = None,
        use_phash: bool = False,
        skip_hidden: bool = True,
        min_size: int = 1024,
        progress_callback: Optional[Callable[[int, int, Path], None]] = None,
    ):
        self.extensions = {
            ext.lower() if ext.startswith(".") else "." + ext.lower()
            for ext in (extensions or DEFAULT_IMAGE_EXTENSIONS)
        }
        self.use_phash = use_phash
        self.skip_hidden = skip_hidden
        self.min_size = min_size
        self.progress_callback = progress_callback

    def is_image_file(self, path: Path) -> bool:
        """判断是否为图片文件"""
        if not path.is_file():
            return False
        if self.skip_hidden and (path.name.startswith(".") or self._is_hidden(path)):
            return False
        if path.suffix.lower() not in self.extensions:
            return False
        try:
            if path.stat().st_size < self.min_size:
                return False
        except OSError:
            return False
        return True

    def _is_hidden(self, path: Path) -> bool:
        """检查文件是否为隐藏（Windows下检查属性）"""
        try:
            if os.name == "nt":
                import ctypes

                attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
                return attrs != -1 and (attrs & 2) != 0
        except Exception:
            pass
        return False

    def collect_files(self, source_dirs: List[Path]) -> List[Path]:
        """递归收集所有图片文件路径"""
        files: List[Path] = []
        seen: Set[Path] = set()

        for src in source_dirs:
            src = src.resolve()
            if not src.exists():
                continue
            if src.is_file():
                if self.is_image_file(src) and src not in seen:
                    seen.add(src)
                    files.append(src)
                continue
            for root, dirs, filenames in os.walk(src):
                root_path = Path(root)
                if self.skip_hidden:
                    dirs[:] = [
                        d for d in dirs
                        if not d.startswith(".")
                        and not self._is_hidden(root_path / d)
                    ]
                for fname in filenames:
                    fpath = root_path / fname
                    if self.is_image_file(fpath):
                        resolved = fpath.resolve()
                        if resolved not in seen:
                            seen.add(resolved)
                            files.append(resolved)
        return files

    def scan(
        self,
        source_dirs: List[Path],
        compute_hashes: bool = True,
    ) -> List[PhotoInfo]:
        """完整扫描：收集文件 + 计算哈希 + 解析 EXIF"""
        file_paths = self.collect_files(source_dirs)
        total = len(file_paths)
        results: List[PhotoInfo] = []

        for idx, fpath in enumerate(file_paths, 1):
            if self.progress_callback:
                self.progress_callback(idx, total, fpath)

            try:
                photo = PhotoInfo(path=fpath)
                populate_photo_info(photo)

                if compute_hashes:
                    photo.content_hash = compute_content_hash(fpath)
                    if photo.is_valid:
                        photo.dhash = compute_dhash(fpath)
                        if self.use_phash:
                            photo.phash = compute_phash(fpath)

                results.append(photo)
            except Exception as e:
                err_photo = PhotoInfo(
                    path=fpath,
                    is_valid=False,
                    error=str(e),
                )
                results.append(err_photo)

        return results

    def scan_light(self, source_dirs: List[Path]) -> List[PhotoInfo]:
        """轻量扫描：只收集路径和基本信息，不计算哈希"""
        return self.scan(source_dirs, compute_hashes=False)
