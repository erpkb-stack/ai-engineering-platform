"""GENERATED from services/service-catalog/openapi.yaml by `make catalog-contract` - do not edit.

The drift test (test_catalog_contract.py) fails when this file is stale."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ServiceItem(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    key: str
    name: str
    type: str
    tier: int = Field(..., ge=1, le=9)
    team: str
    language: str | None = None
    repository: str | None = None
    depends_on: list[str] = Field(..., max_length=100)
    dependents: list[str] = Field(
        ...,
        description="direct dependents (services whose depends_on contains this key), sorted",
        max_length=200,
    )


class Problem(BaseModel):
    type: str | None = None
    title: str
    status: int
    detail: str | None = None


class ServiceList(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    items: list[ServiceItem] = Field(..., max_length=200)
    truncated: bool
