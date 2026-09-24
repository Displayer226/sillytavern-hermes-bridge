from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelListResponse(BaseModel):
    object: str = "list"
    data: list[dict[str, Any]]
    cache_age: float = Field(ge=0)
    cache_hit: bool
    cache_ttl: int = Field(ge=0)


class ToolCallsPage(BaseModel):
    tool_calls: list[dict[str, Any]]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)
    has_more: bool


class SetModelRequest(BaseModel):
    model: str = Field(min_length=1)


class SetProfileRequest(BaseModel):
    profile: str = Field(min_length=1, max_length=64)


class ProcessMetrics(BaseModel):
    pid: int
    uptime_seconds: float = Field(ge=0)
    rss_bytes: int = Field(ge=0)
    cpu_seconds: float = Field(ge=0)
    cpu_percent_avg: float = Field(ge=0)
    started_at: float


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    service: str
    process: ProcessMetrics | None = None


class ReadinessResponse(HealthResponse):
    ready: bool
    backends: dict[str, dict[str, Any]]
