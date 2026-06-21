from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List
from datetime import datetime


@dataclass
class PhotoInfo:
    path: Path
    size: int = 0
    width: int = 0
    height: int = 0
    content_hash: Optional[str] = None
    phash: Optional[str] = None
    dhash: Optional[str] = None
    capture_time: Optional[datetime] = None
    camera_model: Optional[str] = None
    exif_available: bool = False
    is_valid: bool = True
    error: Optional[str] = None

    @property
    def resolution_score(self) -> int:
        return self.width * self.height

    @property
    def megapixels(self) -> float:
        return (self.width * self.height) / 1_000_000


@dataclass
class DuplicateGroup:
    group_id: int
    photos: List[PhotoInfo] = field(default_factory=list)
    primary_index: int = 0
    similarity_score: int = 0

    @property
    def primary(self) -> PhotoInfo:
        return self.photos[self.primary_index]

    @property
    def duplicates(self) -> List[PhotoInfo]:
        return [p for i, p in enumerate(self.photos) if i != self.primary_index]

    @property
    def total_size(self) -> int:
        return sum(p.size for p in self.photos)

    @property
    def saved_size(self) -> int:
        return self.total_size - self.primary.size


@dataclass
class RenamePlan:
    source: Path
    target: Path
    reason: str = ""


@dataclass
class ActionPlan:
    duplicate_groups: List[DuplicateGroup] = field(default_factory=list)
    rename_plans: List[RenamePlan] = field(default_factory=list)
    skipped: List[PhotoInfo] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def total_duplicates(self) -> int:
        return sum(len(g.photos) - 1 for g in self.duplicate_groups)

    @property
    def total_saved_size(self) -> int:
        return sum(g.saved_size for g in self.duplicate_groups)

    @property
    def total_photos(self) -> int:
        return (
            sum(len(g.photos) for g in self.duplicate_groups)
            + len(self.rename_plans)
            + len(self.skipped)
        )
