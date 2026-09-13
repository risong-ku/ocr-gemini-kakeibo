"""Validated monthly summary HTTP endpoint."""

from fastapi import APIRouter

from kakeibo.container import ContainerDependency
from kakeibo.models import Month, SummaryResponse

router = APIRouter()


@router.get("/api/summary", response_model=SummaryResponse)
def get_summary(month: Month, container: ContainerDependency) -> SummaryResponse:
    return container.summary.get_summary(month)
