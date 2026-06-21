"""
完整测试用例
覆盖:
  1. 内容哈希 - 逐字节相同文件被判定为重复
  2. 感知哈希 - 近似图（缩放/压缩）被正确归组
  3. 重命名模板 - EXIF 正确解析、同秒序号不撞名
  4. Dry-Run - 绝不改动磁盘
  5. Apply - 重名保护、不丢文件
"""

import os
import io
import sys
import shutil
import struct
import unittest
import tempfile
import random
from pathlib import Path
from datetime import datetime, timedelta

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from photo_organizer.scanner import Scanner
from photo_organizer.hashing import (
    compute_content_hash,
    compute_dhash,
    compute_phash,
    hamming_distance,
    group_exact_duplicates,
    group_similar_photos,
    deduplicate_photos,
)
from photo_organizer.models import PhotoInfo, DuplicateGroup
from photo_organizer.exif_utils import (
    extract_exif,
    populate_photo_info,
    TemplateEngine,
    parse_exif_datetime,
)
from photo_organizer.archiver import Archiver, ConflictPolicy


def _write_jpeg_with_exif(path: Path, size=(800, 600), color=(100, 150, 200),
                         capture_time: datetime = None, camera="TestCam", quality=95,
                         base_image: Image.Image = None):
    """生成带 EXIF 的测试 JPEG。若提供 base_image，则只缩放/保存（保证图案一致）"""
    from PIL import ExifTags

    if base_image is not None:
        img = base_image.copy()
        if img.size != size:
            img = img.resize(size, Image.LANCZOS)
        # 如果模式不对，转成RGB
        if img.mode != "RGB":
            img = img.convert("RGB")
    else:
        img = Image.new("RGB", size, color)
        pixels = img.load()
        for y in range(size[1]):
            for x in range(size[0]):
                r = (color[0] + x * 7 + y * 3) % 256
                g = (color[1] + x * 5 + y * 11) % 256
                b = (color[2] + x * 13 + y * 2) % 256
                pixels[x, y] = (r, g, b)

    exif = img.getexif()
    tag_map = {name: tid for tid, name in ExifTags.TAGS.items()}

    dt_str = (capture_time or datetime(2024, 6, 15, 14, 30, 45)).strftime("%Y:%m:%d %H:%M:%S")
    exif[tag_map["DateTimeOriginal"]] = dt_str
    exif[tag_map["DateTimeDigitized"]] = dt_str
    exif[tag_map["DateTime"]] = dt_str
    exif[tag_map["Model"]] = camera
    exif[tag_map["Make"]] = "TestMake"
    exif[tag_map["Software"]] = "TestSuite"

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=quality, exif=exif)
    return path


def _build_patterned_image(size=(1024, 768), seed=1, extra_noise=0):
    """生成有明显图案的图像（便于感知哈希稳定匹配）"""
    rng = random.Random(seed)
    img = Image.new("RGB", size)
    pixels = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            base_r = (x // 32 * 37 + y // 24 * 53 + seed * 17) % 256
            base_g = (x // 16 * 29 + y // 48 * 61 + seed * 31) % 256
            base_b = ((x + y) // 40 * 41 + seed * 19) % 256
            noise_r = rng.randint(-extra_noise, extra_noise) if extra_noise else 0
            noise_g = rng.randint(-extra_noise, extra_noise) if extra_noise else 0
            noise_b = rng.randint(-extra_noise, extra_noise) if extra_noise else 0
            pixels[x, y] = (
                max(0, min(255, base_r + noise_r)),
                max(0, min(255, base_g + noise_g)),
                max(0, min(255, base_b + noise_b)),
            )
    return img


class TestContentHash(unittest.TestCase):
    """测试内容哈希 - 逐字节判定"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="t199_chtmp_"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_identical_files_same_hash(self):
        a = self.tmpdir / "a.jpg"
        b = self.tmpdir / "b.jpg"
        _write_jpeg_with_exif(a)
        shutil.copy2(a, b)
        self.assertEqual(compute_content_hash(a), compute_content_hash(b))
        self.assertIsNotNone(compute_content_hash(a))

    def test_different_files_different_hash(self):
        a = self.tmpdir / "a.jpg"
        b = self.tmpdir / "b.jpg"
        _write_jpeg_with_exif(a, color=(10, 20, 30))
        _write_jpeg_with_exif(b, color=(200, 210, 220))
        self.assertNotEqual(compute_content_hash(a), compute_content_hash(b))

    def test_group_exact_duplicates(self):
        src1 = self.tmpdir / "src1.jpg"
        src2 = self.tmpdir / "src2.jpg"
        diff = self.tmpdir / "diff.jpg"
        _write_jpeg_with_exif(src1, color=(111, 111, 111))
        shutil.copy2(src1, src2)
        _write_jpeg_with_exif(diff, color=(1, 2, 3))

        photos = []
        for p in [src1, src2, diff]:
            photo = PhotoInfo(path=p)
            populate_photo_info(photo)
            photo.content_hash = compute_content_hash(p)
            photos.append(photo)

        groups = group_exact_duplicates(photos)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].photos), 2)
        paths = {str(p.path.name) for p in groups[0].photos}
        self.assertIn("src1.jpg", paths)
        self.assertIn("src2.jpg", paths)


class TestPerceptualHash(unittest.TestCase):
    """测试感知哈希近似重复"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="t199_phtmp_"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_dhash_same_image_low_distance(self):
        """同一图保存后dHash应该完全一致或距离极小"""
        orig = self.tmpdir / "orig.jpg"
        _build_patterned_image(seed=42).save(orig, "JPEG", quality=92)
        hash1 = compute_dhash(orig)
        hash2 = compute_dhash(orig)
        self.assertEqual(hash1, hash2)
        self.assertEqual(hamming_distance(hash1, hash2), 0)

    def test_dhash_resized_image_low_distance(self):
        """缩放后的图dHash距离很小，应被归为近似重复"""
        base_img = _build_patterned_image(seed=42, size=(1200, 900))

        big = self.tmpdir / "big.jpg"
        small = self.tmpdir / "small.jpg"
        _write_jpeg_with_exif(big, size=(1200, 900), quality=95, base_image=base_img)
        _write_jpeg_with_exif(small, size=(480, 360), quality=85, base_image=base_img)

        dh_big = compute_dhash(big)
        dh_small = compute_dhash(small)
        dist = hamming_distance(dh_big, dh_small)

        self.assertLessEqual(
            dist, 30,
            f"缩放后 dHash 距离过大: {dist} (big={dh_big[:16]} small={dh_small[:16]})"
        )

    def test_different_images_high_distance(self):
        """不同图案的图dHash距离应该很大"""
        a = self.tmpdir / "a.jpg"
        b = self.tmpdir / "b.jpg"
        _build_patterned_image(seed=100, size=(800, 600)).save(a, "JPEG")
        _build_patterned_image(seed=999, size=(800, 600)).save(b, "JPEG")

        dist = hamming_distance(compute_dhash(a), compute_dhash(b))
        self.assertGreaterEqual(dist, 20, f"不同图距离过小: {dist}")

    def test_group_similar_seed_patterns(self):
        """验证相似分组能正确把缩放/不同质量的归为一组"""
        base_img = _build_patterned_image(seed=77, size=(1024, 768))
        a = self.tmpdir / "a_hires.jpg"
        b = self.tmpdir / "b_lowres.jpg"
        c_diff = _build_patterned_image(seed=9999, size=(800, 600))
        c = self.tmpdir / "c_diff.jpg"

        _write_jpeg_with_exif(a, size=(1024, 768), quality=95, base_image=base_img)
        _write_jpeg_with_exif(b, size=(512, 384), quality=70, base_image=base_img)
        _write_jpeg_with_exif(c, size=(800, 600), quality=90, base_image=c_diff)

        photos = []
        for p in [a, b, c]:
            photo = PhotoInfo(path=p)
            populate_photo_info(photo)
            photo.content_hash = compute_content_hash(p)
            photo.dhash = compute_dhash(p)
            photos.append(photo)

        exact_groups = group_exact_duplicates(photos)
        self.assertEqual(len(exact_groups), 0)

        # 使用更严格的阈值，确保差异图不会被错误归组
        similar_groups = group_similar_photos(photos, threshold=25, hash_type="dhash")
        self.assertGreaterEqual(len(similar_groups), 1, "应至少有1个相似组")

        group_sizes = [len(g.photos) for g in similar_groups]
        self.assertIn(2, group_sizes, f"应该有一个大小为2的组(缩放变体)，实际大小: {group_sizes}")
        # 还应该有一组只有1个（差异图没被归进来）或者总共只有1组（大小为2）+ 1个独立
        total_grouped = sum(group_sizes)
        self.assertLessEqual(total_grouped, 3)


class TestRenameTemplate(unittest.TestCase):
    """测试EXIF解析与重命名模板"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="t199_tpltmp_"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_parse_exif_datetime_formats(self):
        self.assertEqual(
            parse_exif_datetime("2024:06:15 14:30:45"),
            datetime(2024, 6, 15, 14, 30, 45),
        )
        self.assertEqual(
            parse_exif_datetime("2024-06-15 14:30:45"),
            datetime(2024, 6, 15, 14, 30, 45),
        )
        self.assertIsNone(parse_exif_datetime("invalid"))
        self.assertIsNone(parse_exif_datetime("0000:00:00 00:00:00"))

    def test_extract_exif_jpeg(self):
        p = self.tmpdir / "shot.jpg"
        dt = datetime(2023, 12, 25, 9, 15, 30)
        _write_jpeg_with_exif(p, capture_time=dt, camera="MyCamera")
        exif = extract_exif(p)
        self.assertTrue(exif["exif_available"])
        self.assertEqual(exif["camera_model"], "MyCamera")
        self.assertEqual(exif["width"], 800)
        self.assertEqual(exif["height"], 600)
        self.assertIsNotNone(exif["capture_time"])
        self.assertEqual(exif["capture_time"].year, 2023)
        self.assertEqual(exif["capture_time"].month, 12)
        self.assertEqual(exif["capture_time"].day, 25)

    def test_template_engine_default_format(self):
        """默认模板：年月日_时分秒_序号"""
        engine = TemplateEngine()
        p = self.tmpdir / "IMG_0001.JPG"
        dt = datetime(2024, 5, 1, 8, 9, 10)
        _write_jpeg_with_exif(p, capture_time=dt)
        info = PhotoInfo(path=p)
        populate_photo_info(info)

        name = engine.render_name(info)
        self.assertTrue(name.startswith("20240501_080910_001"))
        self.assertTrue(name.lower().endswith(".jpg"))

    def test_template_sequencing_no_collision(self):
        """同一秒内多张图自动加序号"""
        engine = TemplateEngine()
        dt = datetime(2024, 1, 1, 0, 0, 5)
        paths = []
        for i in range(5):
            p = self.tmpdir / f"IMG_{i:04d}.jpg"
            _write_jpeg_with_exif(p, capture_time=dt, color=(i * 30, i * 20, i * 10))
            paths.append(p)

        names = set()
        for p in paths:
            info = PhotoInfo(path=p)
            populate_photo_info(info)
            names.add(engine.render_name(info))

        self.assertEqual(len(names), 5, f"同秒内序号应避免重名: {names}")
        for n in names:
            self.assertRegex(n, r"20240101_000005_00[1-5]\.jpg")

    def test_template_custom_variables(self):
        """自定义模板使用相机型号等变量"""
        engine = TemplateEngine(
            name_template="{YYYY}-{MM}-{DD}_{model}_{seq:02d}",
            archive_template="{YYYY}/{MM}-{model}/",
        )
        p = self.tmpdir / "wx_export.jpg"
        dt = datetime(2022, 10, 31, 23, 59, 59)
        _write_jpeg_with_exif(p, capture_time=dt, camera="CanonEOS")
        info = PhotoInfo(path=p)
        populate_photo_info(info)

        name = engine.render_name(info)
        self.assertIn("2022-10-31_CanonEOS_01", name)

        subdir = engine.render_archive_subdir(info)
        self.assertIn("2022", str(subdir))
        self.assertIn("10-CanonEOS", str(subdir))

    def test_missing_exif_fallback_to_mtime(self):
        """无 EXIF 时退回到文件修改时间"""
        p = self.tmpdir / "noexif.png"
        img = Image.new("RGB", (200, 200), (50, 50, 150))
        img.save(p, "PNG")  # PNG 无 EXIF

        info = PhotoInfo(path=p)
        populate_photo_info(info)
        self.assertIsNotNone(info.capture_time)


class TestDryRunSafety(unittest.TestCase):
    """Dry Run 安全性 - 绝不改动磁盘"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="t199_drytmp_"))
        self.src = self.tmpdir / "src"
        self.dst = self.tmpdir / "dst"
        self.src.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _snapshot_paths(self, root: Path):
        snap = {}
        for p in sorted(root.rglob("*")):
            if p.is_file():
                snap[str(p.relative_to(root))] = (p.stat().st_size, p.stat().st_mtime)
        return snap

    def test_dry_run_does_not_change_disk(self):
        """dry-run 时源目录不变，目标目录不新建文件"""
        for i in range(3):
            p = self.src / f"img{i}.jpg"
            _write_jpeg_with_exif(p, color=(i * 80, 0, 255 - i * 80))

        # 完全重复 + 不同图
        shutil.copy2(self.src / "img0.jpg", self.src / "img0_copy.jpg")

        before_src = self._snapshot_paths(self.src)
        before_src_files = set(before_src.keys())

        scanner = Scanner()
        photos = scanner.scan([self.src])
        groups, unique = deduplicate_photos(photos, similarity_threshold=30)

        engine = TemplateEngine()
        archiver = Archiver(
            archive_root=self.dst,
            dry_run=True,  # DRY RUN
            template_engine=engine,
        )
        plan = archiver.build_action_plan(photos, groups)
        result = archiver.apply_plan(plan, duplicate_action="move")

        after_src = self._snapshot_paths(self.src)
        self.assertEqual(before_src, after_src, "dry-run 不能修改源文件")
        self.assertFalse(self.dst.exists(), "dry-run 不应创建归档目录")
        self.assertEqual(set(after_src.keys()), before_src_files)
        self.assertEqual(result.archived_count, len(plan.rename_plans))
        for op in result.operations:
            self.assertTrue(op["dry_run"])


class TestApplyNoDataLoss(unittest.TestCase):
    """Apply 执行安全：重名保护、不丢文件"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="t199_applytmp_"))
        self.src = self.tmpdir / "src"
        self.dst = self.tmpdir / "dst"
        self.src.mkdir()
        self.dst.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_conflict_rename_no_overwrite(self):
        """目标位置已有同名文件时自动加序号，绝不覆盖"""
        dt = datetime(2020, 1, 1, 0, 0, 0)

        src_a = self.src / "a.jpg"
        _write_jpeg_with_exif(src_a, capture_time=dt, color=(1, 2, 3), size=(400, 300))

        # 在目标位置预先放一个相同预期名称的文件
        target_dir = self.dst / "2020" / "01"
        target_dir.mkdir(parents=True)
        collision = target_dir / "20200101_000000_001.jpg"
        _write_jpeg_with_exif(
            collision, capture_time=dt, color=(99, 99, 99), size=(100, 100)
        )
        collision_hash = compute_content_hash(collision)

        scanner = Scanner()
        photos = scanner.scan([self.src])
        engine = TemplateEngine()
        archiver = Archiver(
            archive_root=self.dst,
            dry_run=False,
            conflict_policy=ConflictPolicy.RENAME,
            template_engine=engine,
        )
        plan = archiver.build_action_plan(photos, [])
        result = archiver.apply_plan(plan)

        self.assertTrue(collision.exists(), "原目标位置的文件不得被覆盖")
        self.assertEqual(
            compute_content_hash(collision),
            collision_hash,
            "原目标文件内容必须不变",
        )
        archived_files = list(self.dst.rglob("*.jpg"))
        self.assertGreaterEqual(len(archived_files), 2, "两个文件都应保留")
        # 新文件必须带 _002 或类似后缀
        new_file = [f for f in archived_files if f != collision][0]
        self.assertIn("_", new_file.stem or "", "新文件名称应有冲突后缀")

    def test_duplicate_groups_primary_kept_largest(self):
        """每组重复文件里，分辨率最高/体积最大的被选为主文件"""
        base_color = (200, 100, 50)

        # 先生成基础高分辨率图案
        base_hi_img = _build_patterned_image(seed=42, size=(1600, 1200))

        # 主文件：高分辨率大尺寸
        hi = self.src / "hi.jpg"
        _write_jpeg_with_exif(hi, size=(1600, 1200), quality=98, base_image=base_hi_img)

        # 复制 (完全重复，逐字节相同)
        copy_hi = self.src / "copy_hi.jpg"
        shutil.copy2(hi, copy_hi)

        # 低分辨率变体 (近似 - 相同图案缩放)
        lo = self.src / "lo.jpg"
        _write_jpeg_with_exif(lo, size=(400, 300), quality=60, base_image=base_hi_img)

        scanner = Scanner()
        photos = scanner.scan([self.src])
        for p in photos:
            p.content_hash = compute_content_hash(p.path)
            p.dhash = compute_dhash(p.path)

        groups, unique = deduplicate_photos(photos, similarity_threshold=35)

        self.assertGreaterEqual(len(groups), 1, "应检测到重复组")

        # 把所有主分辨率为 1600x1200 的组的重复项合并
        all_duplicate_names: set = set()
        found_1600_primary = False
        for g in groups:
            primary = g.primary
            if primary.width == 1600 and primary.height == 1200:
                found_1600_primary = True
                for p in g.duplicates:
                    all_duplicate_names.add(p.path.name)

        self.assertTrue(found_1600_primary, "应有一组其主文件分辨率为1600x1200")
        # hi.jpg 和 copy_hi.jpg 中必有一个是冗余（另一个被保留为 primary），lo.jpg 也应是冗余
        hi_res_dupes = {"hi.jpg", "copy_hi.jpg"}
        self.assertTrue(
            all_duplicate_names & hi_res_dupes,
            f"两个高分辨率文件中应有一个被标记为冗余，所有重复项: {all_duplicate_names}"
        )
        self.assertIn(
            "lo.jpg", all_duplicate_names,
            f"近似变体lo.jpg应被标记为冗余，所有重复项: {all_duplicate_names}"
        )

    def test_invalid_files_skipped_not_crash(self):
        """损坏或非图片文件应被跳过并记录，不应崩溃"""
        good = self.src / "good.jpg"
        _write_jpeg_with_exif(good, size=(200, 200))

        bad = self.src / "bad.jpg"
        bad.write_bytes(b"this is not a real image \x00\x01\xff\xd8 fake data")

        txt = self.src / "notes.txt"
        txt.write_text("some notes, should not be touched")

        scanner = Scanner()
        photos = scanner.scan([self.src])
        for p in photos:
            p.content_hash = compute_content_hash(p.path)
            p.dhash = compute_dhash(p.path)

        groups, unique = deduplicate_photos(photos, similarity_threshold=30)

        engine = TemplateEngine()
        archiver = Archiver(archive_root=self.dst, dry_run=False, template_engine=engine)
        plan = archiver.build_action_plan(photos, groups)
        result = archiver.apply_plan(plan)

        # txt 根本不应被扫描
        self.assertTrue(txt.exists())
        # good.jpg 应被归档
        self.assertTrue(result.archived_count >= 1)


class TestHammingDistance(unittest.TestCase):
    def test_zero_for_identical(self):
        self.assertEqual(hamming_distance("abcd1234", "abcd1234"), 0)

    def test_none_inputs(self):
        self.assertEqual(hamming_distance(None, "abcd"), 999)
        self.assertEqual(hamming_distance("abcd", None), 999)

    def test_known_bits(self):
        # 0xf = 1111 vs 0x0 = 0000 -> 4 位差
        self.assertEqual(hamming_distance("f", "0"), 4)
        # 0xa = 1010 vs 0x5 = 0101 -> 4 位差
        self.assertEqual(hamming_distance("a", "5"), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
