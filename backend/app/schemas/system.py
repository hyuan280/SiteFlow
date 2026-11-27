from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SystemSettings(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    show_system_resources: bool


class ComponentBuild(BaseModel):
    version: str | None = Field(max_length=128)
    commit: str | None = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class SystemVersions(BaseModel):
    api: ComponentBuild
    worker: ComponentBuild | None
    bitwarden: ComponentBuild | None


class SystemResources(BaseModel):
    model_config = ConfigDict(frozen=True)

    cpu_percent: float | None = None
    memory_percent: float | None = None
    memory_used_bytes: int | None = None
    memory_total_bytes: int | None = None
    scope: str
    sampled_at: str
    error: str | None = None
    docker_available: bool = Field(
        default=False,
        description="True only when matching running Compose services were sampled; "
        "no matching services returns false, count 0, null SiteFlow metrics and a fixed error.",
    )
    siteflow_cpu_percent: float | None = Field(
        default=None,
        description="Host-normalized complete CPU sum. Missing precpu counters trigger at most "
        "one extra one-shot sample after 0.1s within the same sampling budget; no cached "
        "counters are reused. Invalid or reset counters yield null independently of memory.",
    )
    siteflow_memory_percent: float | None = None
    siteflow_memory_used_bytes: int | None = None
    siteflow_container_count: int = 0
    docker_error: str | None = None
