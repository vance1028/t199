"""
哈希去重内核模块
- 内容哈希 SHA256：逐字节判定完全重复
- 感知哈希 dHash（差异哈希）+ pHash（感知哈希）：判定近似重复
- 相似分组：根据汉明距离自动分组
"""

import hashlib
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Callable
from collections import defaultdict

try:
    from PIL import Image
except ImportError:
    Image = None

from .models import PhotoInfo, DuplicateGroup


CHUNK_SIZE = 1024 * 1024
DEFAULT_HAMMING_THRESHOLD = 10


def compute_content_hash(file_path: Path) -> Optional[str]:
    """计算文件的 SHA256 内容哈希，逐字节判断完全重复"""
    sha256 = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                sha256.update(chunk)
        return sha256.hexdigest()
    except (OSError, IOError):
        return None


def _resize_gray(image, size: Tuple[int, int]) -> List[int]:
    """将图片缩放到指定尺寸并转为灰度像素列表"""
    resized = image.resize(size, Image.LANCZOS).convert("L")
    try:
        return list(resized.get_flattened_data())
    except AttributeError:
        return list(resized.getdata())


def compute_dhash(file_path: Path, hash_size: int = 8) -> Optional[str]:
    """
    计算差异哈希 (Difference Hash)
    算法：缩放到 (hash_size+1) x hash_size，比较相邻像素差异
    对分辨率变化、重新压缩、轻微处理鲁棒
    """
    if Image is None:
        return None
    try:
        with Image.open(file_path) as img:
            img.load()
            pixels = _resize_gray(img, (hash_size + 1, hash_size))
    except Exception:
        return None

    hash_bits = []
    for row in range(hash_size):
        row_start = row * (hash_size + 1)
        for col in range(hash_size):
            left = pixels[row_start + col]
            right = pixels[row_start + col + 1]
            hash_bits.append("1" if left > right else "0")

    bits_str = "".join(hash_bits)
    hex_str = ""
    for i in range(0, len(bits_str), 4):
        hex_str += "{:x}".format(int(bits_str[i : i + 4], 2))
    return hex_str


def compute_phash(file_path: Path, hash_size: int = 8) -> Optional[str]:
    """
    计算感知哈希 (Perceptual Hash) - 基于DCT的简化版
    算法：缩放到 32x32 → 灰度 → DCT → 取低频8x8 → 比较均值
    """
    if Image is None:
        return None
    try:
        with Image.open(file_path) as img:
            img.load()
            pixels = _resize_gray(img, (32, 32))
    except Exception:
        return None

    try:
        import numpy as np

        pixel_matrix = np.array(pixels, dtype=np.float64).reshape(32, 32)
        dct = _dct_2d(pixel_matrix)
        dct_low = dct[:hash_size, :hash_size]
        median = np.median(dct_low)
        diff = dct_low > median
        bits = diff.flatten()
        bits_str = "".join("1" if b else "0" for b in bits)
    except ImportError:
        avg = sum(pixels) / len(pixels)
        bits_str = "".join("1" if p > avg else "0" for p in pixels)
        bits_str = bits_str[: hash_size * hash_size]

    hex_str = ""
    for i in range(0, len(bits_str), 4):
        hex_str += "{:x}".format(int(bits_str[i : i + 4], 2))
    return hex_str


def _dct_2d(matrix):
    """简化的 2D DCT 实现（避免强依赖 scipy）"""
    try:
        import numpy as np
        from scipy.fftpack import dct

        return dct(dct(matrix, axis=0, norm="ortho"), axis=1, norm="ortho")
    except ImportError:
        import numpy as np

        n = matrix.shape[0]
        result = np.zeros_like(matrix, dtype=np.float64)
        for u in range(n):
            for v in range(n):
                cu = 1.0 / np.sqrt(2) if u == 0 else 1.0
                cv = 1.0 / np.sqrt(2) if v == 0 else 1.0
                s = 0.0
                for x in range(n):
                    for y in range(n):
                        s += (
                            matrix[x, y]
                            * np.cos((2 * x + 1) * u * np.pi / (2 * n))
                            * np.cos((2 * y + 1) * v * np.pi / (2 * n))
                        )
                result[u, v] = 0.25 * cu * cv * s
        return result


def hamming_distance(hash1: str, hash2: str) -> int:
    """计算两个十六进制哈希字符串的汉明距离"""
    if hash1 is None or hash2 is None:
        return 999
    if len(hash1) != len(hash2):
        min_len = min(len(hash1), len(hash2))
        hash1 = hash1[:min_len]
        hash2 = hash2[:min_len]
    bits1 = bin(int(hash1, 16))[2:].zfill(len(hash1) * 4)
    bits2 = bin(int(hash2, 16))[2:].zfill(len(hash2) * 4)
    return sum(b1 != b2 for b1, b2 in zip(bits1, bits2))


def group_exact_duplicates(photos: List[PhotoInfo]) -> List[DuplicateGroup]:
    """
    第一层去重：按内容哈希分组，完全相同的文件归为一组
    """
    hash_map: Dict[str, List[PhotoInfo]] = defaultdict(list)
    for photo in photos:
        if photo.content_hash:
            hash_map[photo.content_hash].append(photo)

    groups = []
    gid = 0
    for hash_val, group_photos in hash_map.items():
        if len(group_photos) > 1:
            sorted_photos = sorted(
                group_photos,
                key=lambda p: (p.resolution_score, p.size),
                reverse=True,
            )
            groups.append(
                DuplicateGroup(
                    group_id=gid,
                    photos=sorted_photos,
                    primary_index=0,
                    similarity_score=0,
                )
            )
            gid += 1
    return groups


def group_similar_photos(
    photos: List[PhotoInfo],
    threshold: int = DEFAULT_HAMMING_THRESHOLD,
    hash_type: str = "dhash",
) -> List[DuplicateGroup]:
    """
    第二层去重：感知哈希 + 汉明距离聚类，归为近似重复组
    使用贪心合并策略：两两比较，距离小于阈值则合并
    """
    if not photos:
        return []

    unassigned = list(photos)
    groups: List[List[PhotoInfo]] = []
    group_distances: List[List[int]] = []

    while unassigned:
        current = unassigned.pop(0)
        current_hash = getattr(current, hash_type)

        placed = False
        for gi, group in enumerate(groups):
            for member in group:
                member_hash = getattr(member, hash_type)
                dist = hamming_distance(current_hash, member_hash)
                if dist <= threshold:
                    group.append(current)
                    group_distances[gi].append(dist)
                    placed = True
                    break
            if placed:
                break

        if not placed:
            groups.append([current])
            group_distances.append([0])

    result = []
    gid = 0
    for group, dists in zip(groups, group_distances):
        if len(group) > 1:
            sorted_group = sorted(
                group,
                key=lambda p: (p.resolution_score, p.size),
                reverse=True,
            )
            avg_dist = sum(dists) // len(dists) if dists else 0
            result.append(
                DuplicateGroup(
                    group_id=gid,
                    photos=sorted_group,
                    primary_index=0,
                    similarity_score=avg_dist,
                )
            )
            gid += 1
    return result


def deduplicate_photos(
    photos: List[PhotoInfo],
    similarity_threshold: int = DEFAULT_HAMMING_THRESHOLD,
    use_phash: bool = False,
) -> Tuple[List[DuplicateGroup], List[PhotoInfo]]:
    """
    完整去重流程：
    1. 先按内容哈希找完全重复（exact group）
    2. exact group 的 primary 参与感知哈希分组，找近似重复
    3. 若 exact primary 被归入某个 similar 组，则合并该 exact 的 duplicate 到 similar 组
    返回 (重复组列表, 剩余唯一照片列表)
    """
    hash_attr = "phash" if use_phash else "dhash"

    exact_groups = group_exact_duplicates(photos)

    exact_primary_paths = {g.primary.path for g in exact_groups}
    exact_group_by_primary: Dict[Path, DuplicateGroup] = {}
    for g in exact_groups:
        exact_group_by_primary[g.primary.path] = g
    all_duplicate_paths = set()
    for g in exact_groups:
        for p in g.photos:
            all_duplicate_paths.add(p.path)

    remaining_for_similar = []
    for p in photos:
        if p.path in exact_primary_paths or p.path not in all_duplicate_paths:
            if getattr(p, hash_attr) is not None:
                if p.path not in {pp.path for pp in remaining_for_similar}:
                    remaining_for_similar.append(p)

    similar_groups = group_similar_photos(
        remaining_for_similar, threshold=similarity_threshold, hash_type=hash_attr
    )

    merged_similar_groups: List[DuplicateGroup] = []
    merged_primary_exact: set = set()

    for sg in similar_groups:
        extra_dups: List[PhotoInfo] = []
        for member in sg.photos:
            if member.path in exact_group_by_primary:
                eg = exact_group_by_primary[member.path]
                for dup in eg.duplicates:
                    if dup.path not in {x.path for x in sg.photos}:
                        extra_dups.append(dup)
                merged_primary_exact.add(member.path)

        merged_photos = list(sg.photos) + extra_dups
        sorted_merged = sorted(
            merged_photos,
            key=lambda p: (p.resolution_score, p.size),
            reverse=True,
        )
        new_sg = DuplicateGroup(
            group_id=0,
            photos=sorted_merged,
            primary_index=0,
            similarity_score=sg.similarity_score,
        )
        merged_similar_groups.append(new_sg)

    leftover_exact = [
        eg for eg in exact_groups if eg.primary.path not in merged_primary_exact
    ]

    all_groups = leftover_exact + merged_similar_groups

    for new_id, g in enumerate(all_groups):
        g.group_id = new_id

    unique_paths_from_groups: set = set()
    for g in all_groups:
        unique_paths_from_groups.add(g.primary.path)

    all_grouped_paths = set()
    for g in all_groups:
        for p in g.photos:
            all_grouped_paths.add(p.path)

    unique_photos = [g.primary for g in all_groups]
    seen_unique = {p.path for p in unique_photos}
    for p in photos:
        if p.path not in all_grouped_paths and p.path not in seen_unique:
            unique_photos.append(p)
            seen_unique.add(p.path)

    return all_groups, unique_photos
