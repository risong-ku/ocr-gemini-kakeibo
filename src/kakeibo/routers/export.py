"""BOM-prefixed CSV download; middleware treats this URL as a page."""

from fastapi import APIRouter, Response

from kakeibo.container import ContainerDependency
from kakeibo.models import ExportMonth
from kakeibo.services.summary import encode_csv

router = APIRouter()


@router.get("/export.csv")
def export_csv(month: ExportMonth, container: ContainerDependency) -> Response:
    content = encode_csv(container.summary.csv_rows(month))
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="kakeibo_{month}.csv"'},
    )
