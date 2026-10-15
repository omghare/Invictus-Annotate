"""
Pydantic models for request/response validation.
Updated for multi-project support.
"""
from typing import List, Literal, Optional
from pydantic import BaseModel, Field, field_validator

from app.config import MIN_PASSWORD_LEN


def check_password(v: str) -> str:
    """Shared rule for every new password (bcrypt only reads the first 72 bytes)."""
    if len(v) < MIN_PASSWORD_LEN:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LEN} characters")
    if len(v.encode("utf-8")) > 72:
        raise ValueError("Password is too long (max 72 bytes)")
    return v


# ── Auth ─────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    username: str
    is_admin: bool
    must_change_password: bool = False

class UserOut(BaseModel):
    id: str
    username: str
    is_admin: bool
    created_at: str
    must_change_password: bool = False

class ChangeOwnPasswordRequest(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_valid(cls, v: str) -> str:
        return check_password(v)

class SetPasswordRequest(BaseModel):
    password: str

    @field_validator("password")
    @classmethod
    def password_valid(cls, v: str) -> str:
        return check_password(v)

class CreateUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False

    @field_validator("username")
    @classmethod
    def username_valid(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Username must be at least 2 characters")
        return v

    @field_validator("password")
    @classmethod
    def password_valid(cls, v: str) -> str:
        return check_password(v)


# ── Project ──────────────────────────────────────────────────────────────────

class CreateProjectRequest(BaseModel):
    name: str
    classes: List[str]

    @field_validator("name")
    @classmethod
    def name_valid(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Project name must be at least 2 characters")
        return v

    @field_validator("classes")
    @classmethod
    def classes_valid(cls, v: List[str]) -> List[str]:
        v = [c.strip() for c in v if c.strip()]
        if len(v) == 0:
            raise ValueError("At least one class required")
        if len(v) != len(set(v)):
            raise ValueError("Class names must be unique")
        return v

class UpdateClassesRequest(BaseModel):
    classes: List[str]
    # {old_name: new_name} — existing boxes follow the rename instead of becoming orphans
    renames: dict[str, str] = {}

    @field_validator("classes")
    @classmethod
    def classes_valid(cls, v: List[str]) -> List[str]:
        v = [c.strip() for c in v if c.strip()]
        if len(v) == 0:
            raise ValueError("At least one class required")
        if len(v) != len(set(v)):
            raise ValueError("Class names must be unique")
        return v

class ProjectOut(BaseModel):
    """Full project detail (used for dashboard)."""
    id: str
    name: str
    classes: List[str]
    source_folder: Optional[str]
    thumbnail_image_id: Optional[str]
    created_at: str
    # Computed stats (populated by the API)
    image_count: int = 0
    annotated_count: int = 0
    ready_count: int = 0
    exported_count: int = 0

class ProjectListItem(BaseModel):
    """Lightweight project card (used for Home page)."""
    id: str
    name: str
    classes: List[str]
    source_folder: Optional[str]
    thumbnail_image_id: Optional[str]
    created_at: str
    created_by: str
    image_count: int
    annotated_count: int
    ready_count: int
    exported_count: int
    unassigned_count: int
    stats: dict = {}          # raw image count per state (for the stacked progress bar)

class ImportResult(BaseModel):
    imported: int
    skipped: int
    folder: str


class ProjectHistoryItem(BaseModel):
    project_id: str
    name: str
    date_created: str
    dataset_size: int
    date_deleted: Optional[str] = None


# ── Images ───────────────────────────────────────────────────────────────────

class ImageOut(BaseModel):
    id: str
    filename: str
    width: int
    height: int
    state: str
    assigned_to: Optional[str]
    assigned_username: Optional[str]
    seq: Optional[int] = None
    seq_group: Optional[str] = None
    box_count: int = 0
    is_background: bool = False
    review_status: Optional[str] = None

class PaginatedImages(BaseModel):
    """Paginated image list response."""
    items: List[ImageOut]
    total: int
    page: int
    per_page: int
    total_pages: int

class ImageStats(BaseModel):
    total: int
    unassigned: int
    assigned: int
    unannotated: int
    annotated: int
    ready_for_export: int
    exported: int

class AssignRequest(BaseModel):
    image_ids: List[str]
    user_id: str

class AutoAssignRequest(BaseModel):
    user_ids: List[str]
    image_ids: Optional[List[str]] = None
    # contiguous = consecutive frame ranges per user (keeps sequences together)
    strategy: Literal["contiguous", "round_robin"] = "contiguous"

class UnassignRequest(BaseModel):
    image_ids: List[str]

class RemoveUserFromProjectRequest(BaseModel):
    keep_annotated: bool


# ── Annotations ──────────────────────────────────────────────────────────────

AnnotationSource = Literal["manual", "carried", "propagated", "interpolated", "tracked", "model"]

class BoundingBox(BaseModel):
    class_id: int = -1
    class_name: str
    x: float
    y: float
    w: float
    h: float
    source: AnnotationSource = "manual"
    track_id: Optional[str] = Field(None, max_length=32)
    confidence: Optional[float] = Field(None, ge=0, le=1)

    @field_validator("w", "h")
    @classmethod
    def positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Width and height must be positive")
        return v

class SaveAnnotationsRequest(BaseModel):
    annotations: List[BoundingBox]
    # True = "this frame intentionally has no objects" (a YOLO background image)
    is_background: bool = False
    # The frame version the client loaded; a newer one on the server means someone else saved → 409
    base_version: Optional[int] = None

class AnnotationOut(BaseModel):
    id: str
    image_id: str
    class_id: int
    class_name: str
    x: int
    y: int
    w: int
    h: int
    source: str = "manual"
    track_id: Optional[str] = None
    confidence: Optional[float] = None

class ImageAnnotationsResponse(BaseModel):
    image_id: str
    filename: str
    width: int
    height: int
    state: str
    is_background: bool = False
    dropped: int = 0
    file_missing: bool = False
    filepath: Optional[str] = None      # only sent to admins (it is a path on the server)
    version: int = 0
    review_status: Optional[str] = None
    review_note: Optional[str] = None
    reviewed_by: Optional[str] = None   # username
    annotations: List[AnnotationOut]


# ── Export ───────────────────────────────────────────────────────────────────

class ExportRequest(BaseModel):
    train_ratio: float
    valid_ratio: float
    test_ratio: float
    include_test: bool = True
    # Keep runs of consecutive frames in the same split (prevents train/val leakage)
    group_sequences: bool = True
    seed: Optional[int] = None
    # Only frames a reviewer approved (see the Review section)
    only_approved: bool = False

    @field_validator("train_ratio", "valid_ratio", "test_ratio")
    @classmethod
    def ratio_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("Ratio must be between 0 and 1")
        return round(v, 4)

class ExportPreview(BaseModel):
    total_ready: int
    train_count: int
    valid_count: int
    test_count: int

class ExportResult(BaseModel):
    exported: int
    train: int
    valid: int
    test: int
    orphan_warnings: List[str]
    output_path: str
    seed: Optional[int] = None
    linked: int = 0          # images hard-linked instead of copied (no extra disk space)
