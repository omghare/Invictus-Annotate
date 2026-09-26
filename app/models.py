"""
Pydantic models for request/response validation.
Updated for multi-project support.
"""
from typing import List, Optional
from pydantic import BaseModel, field_validator


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

class UserOut(BaseModel):
    id: str
    username: str
    is_admin: bool
    created_at: str

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
        if len(v) < 4:
            raise ValueError("Password must be at least 4 characters")
        return v


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

    @field_validator("classes")
    @classmethod
    def classes_valid(cls, v: List[str]) -> List[str]:
        v = [c.strip() for c in v if c.strip()]
        if len(v) == 0:
            raise ValueError("At least one class required")
        if len(v) != len(set(v)):\
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

class RemoveUserFromProjectRequest(BaseModel):
    keep_annotated: bool


# ── Annotations ──────────────────────────────────────────────────────────────

class BoundingBox(BaseModel):
    class_id: int
    class_name: str
    x: int
    y: int
    w: int
    h: int

    @field_validator("w", "h")
    @classmethod
    def positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("Width and height must be positive")
        return v

class SaveAnnotationsRequest(BaseModel):
    annotations: List[BoundingBox]

class AnnotationOut(BaseModel):
    id: str
    image_id: str
    class_id: int
    class_name: str
    x: int
    y: int
    w: int
    h: int

class ImageAnnotationsResponse(BaseModel):
    image_id: str
    filename: str
    width: int
    height: int
    state: str
    annotations: List[AnnotationOut]


# ── Export ───────────────────────────────────────────────────────────────────

class ExportRequest(BaseModel):
    train_ratio: float
    valid_ratio: float
    test_ratio: float
    include_test: bool = True

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
