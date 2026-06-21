"""
EXIF 解析与重命名模板引擎
- 读取 EXIF：拍摄时间、相机型号、GPS 等
- 模板引擎：支持自定义变量，生成文件名和归档路径
"""

import re
import string
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, Any, List
from collections import defaultdict

try:
    from PIL import Image, ExifTags
except ImportError:
    Image = None
    ExifTags = None

from .models import PhotoInfo


EXIF_TAGS = {
    "DateTimeOriginal": None,
    "DateTimeDigitized": None,
    "DateTime": None,
    "Model": None,
    "Make": None,
    "ImageWidth": None,
    "ImageLength": None,
    "Orientation": None,
    "GPSInfo": None,
    "FocalLength": None,
    "ISO": None,
    "FNumber": None,
    "ExposureTime": None,
}

if ExifTags:
    TAG_NAME_TO_ID = {v: k for k, v in ExifTags.TAGS.items()}
    for name in list(EXIF_TAGS.keys()):
        EXIF_TAGS[name] = TAG_NAME_TO_ID.get(name)
else:
    TAG_NAME_TO_ID = {}


DATE_FORMATS = [
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y:%m:%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y:%m:%d",
]


def parse_exif_datetime(value: str) -> Optional[datetime]:
    """尝试多种格式解析 EXIF 时间字符串"""
    if not value:
        return None
    value = str(value).strip()
    if not value or value in ("0000:00:00 00:00:00", "0000-00-00 00:00:00"):
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def get_image_dimensions(file_path: Path) -> tuple[int, int]:
    """获取图片尺寸，优先从文件头快速读取，失败则用 PIL"""
    if Image is None:
        return 0, 0
    try:
        with Image.open(file_path) as img:
            return img.width, img.height
    except Exception:
        return 0, 0


def extract_exif(file_path: Path) -> Dict[str, Any]:
    """提取图片的 EXIF 信息"""
    result: Dict[str, Any] = {
        "capture_time": None,
        "camera_model": None,
        "camera_make": None,
        "width": 0,
        "height": 0,
        "exif_available": False,
        "raw_exif": {},
    }

    if Image is None:
        w, h = 0, 0
    else:
        w, h = get_image_dimensions(file_path)
    result["width"] = w
    result["height"] = h

    if ExifTags is None or Image is None:
        return result

    try:
        with Image.open(file_path) as img:
            exif_data = img._getexif()
            if not exif_data:
                return result

            result["exif_available"] = True
            decoded = {}
            for tag_id, value in exif_data.items():
                tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
                decoded[tag_name] = value
            result["raw_exif"] = decoded

            for key in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
                if key in decoded:
                    dt = parse_exif_datetime(str(decoded[key]))
                    if dt:
                        result["capture_time"] = dt
                        break

            if "Model" in decoded:
                result["camera_model"] = str(decoded["Model"]).strip()
            if "Make" in decoded:
                result["camera_make"] = str(decoded["Make"]).strip()

            if result["width"] == 0 and "ImageWidth" in decoded:
                try:
                    result["width"] = int(decoded["ImageWidth"])
                except (ValueError, TypeError):
                    pass
            if result["height"] == 0 and "ImageLength" in decoded:
                try:
                    result["height"] = int(decoded["ImageLength"])
                except (ValueError, TypeError):
                    pass

    except Exception:
        pass

    return result


def populate_photo_info(photo: PhotoInfo) -> PhotoInfo:
    """填充 PhotoInfo 的 EXIF 相关字段"""
    try:
        photo.size = photo.path.stat().st_size
    except OSError:
        photo.size = 0

    exif = extract_exif(photo.path)
    photo.width = exif["width"]
    photo.height = exif["height"]
    photo.capture_time = exif["capture_time"]
    photo.camera_model = exif["camera_model"]
    photo.exif_available = exif["exif_available"]

    if photo.capture_time is None:
        try:
            mtime = photo.path.stat().st_mtime
            photo.capture_time = datetime.fromtimestamp(mtime)
        except OSError:
            photo.capture_time = datetime.now()

    return photo


DEFAULT_NAME_TEMPLATE = "{YYYY}{MM}{DD}_{hh}{mm}{ss}_{seq:03d}"
DEFAULT_ARCHIVE_TEMPLATE = "{YYYY}/{MM}/"

TEMPLATE_VARS = {
    "YYYY": "年 (4位)",
    "YY": "年 (2位)",
    "MM": "月 (2位)",
    "DD": "日 (2位)",
    "hh": "时 (24小时制, 2位)",
    "mm": "分 (2位)",
    "ss": "秒 (2位)",
    "seq": "序号 (同秒内多张时使用)",
    "model": "相机型号",
    "make": "相机品牌",
    "orig_name": "原始文件名(不含扩展名)",
    "ext": "扩展名(含点)",
}


def _sanitize_filename(name: str) -> str:
    """清理文件名中的非法字符"""
    invalid_chars = r'[<>:"/\\|?*\x00-\x1f]'
    cleaned = re.sub(invalid_chars, "_", name)
    cleaned = cleaned.strip(" .")
    return cleaned or "unnamed"


def _get_template_vars(
    photo: PhotoInfo,
    seq: int = 1,
) -> Dict[str, str]:
    """从 PhotoInfo 生成模板变量字典"""
    dt = photo.capture_time or datetime.now()
    model = (photo.camera_model or "UnknownModel").replace(" ", "_")
    make = ""
    orig_stem = photo.path.stem
    ext = photo.path.suffix.lower()

    return {
        "YYYY": f"{dt.year:04d}",
        "YY": f"{dt.year % 100:02d}",
        "MM": f"{dt.month:02d}",
        "DD": f"{dt.day:02d}",
        "hh": f"{dt.hour:02d}",
        "mm": f"{dt.minute:02d}",
        "ss": f"{dt.second:02d}",
        "seq": f"{seq:03d}",
        "seq:02d": f"{seq:02d}",
        "seq:03d": f"{seq:03d}",
        "seq:04d": f"{seq:04d}",
        "model": model,
        "make": make,
        "orig_name": orig_stem,
        "ext": ext,
    }


class TemplateEngine:
    """文件名和归档路径的模板引擎"""

    def __init__(
        self,
        name_template: str = DEFAULT_NAME_TEMPLATE,
        archive_template: str = DEFAULT_ARCHIVE_TEMPLATE,
    ):
        self.name_template = name_template
        self.archive_template = archive_template
        self._counter: Dict[str, int] = defaultdict(int)

    def reset_counters(self):
        """重置序号计数器"""
        self._counter.clear()

    def render_name(self, photo: PhotoInfo) -> str:
        """生成文件名（不含目录路径）"""
        base = self._render_without_seq(self.name_template, photo)
        counter_key = base
        self._counter[counter_key] += 1
        seq = self._counter[counter_key]

        vars_dict = _get_template_vars(photo, seq)
        rendered = self._apply_template(self.name_template, vars_dict)

        if "{ext}" not in self.name_template and not rendered.lower().endswith(
            photo.path.suffix.lower()
        ):
            rendered = rendered + photo.path.suffix.lower()

        return _sanitize_filename(rendered)

    def render_archive_subdir(self, photo: PhotoInfo) -> Path:
        """生成归档的子目录部分"""
        vars_dict = _get_template_vars(photo, 1)
        rendered = self._apply_template(self.archive_template, vars_dict)
        parts = [_sanitize_filename(p) for p in rendered.replace("\\", "/").split("/") if p]
        return Path(*parts) if parts else Path(".")

    def render_full_path(self, photo: PhotoInfo, archive_root: Path) -> Path:
        """生成完整的目标路径"""
        subdir = self.render_archive_subdir(photo)
        filename = self.render_name(photo)
        return archive_root / subdir / filename

    def _render_without_seq(self, template: str, photo: PhotoInfo) -> str:
        """渲染但将 {seq...} 占位符保留原样，用于生成计数key"""
        vars_dict = _get_template_vars(photo, 0)
        result = template
        for key, value in vars_dict.items():
            if key.startswith("seq"):
                continue
            result = result.replace("{" + key + "}", str(value))
        return result

    def _apply_template(self, template: str, vars_dict: Dict[str, str]) -> str:
        """应用模板变量，支持 seq:03d 这类格式"""
        result = template
        for key in sorted(vars_dict.keys(), key=len, reverse=True):
            placeholder = "{" + key + "}"
            if placeholder in result:
                result = result.replace(placeholder, str(vars_dict[key]))

        for match in re.finditer(r"\{seq:(\d+)d\}", result):
            width = int(match.group(1))
            seq_val = int(vars_dict.get("seq", "1"))
            result = result.replace(match.group(0), f"{seq_val:0{width}d}")

        return result
