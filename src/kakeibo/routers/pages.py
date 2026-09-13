"""Minimal HTML pages and public health check."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from kakeibo.container import ContainerDependency

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    return templates.TemplateResponse(request=request, name="login.html")


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request, passcode: Annotated[str, Form()], container: ContainerDependency
) -> Response:
    if not container.auth.verify_passcode(passcode):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "パスコードが違います"},
        )
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    container.auth.set_cookie(response)
    return response


@router.get("/", response_class=HTMLResponse)
def upload_page(request: Request) -> Response:
    return templates.TemplateResponse(request=request, name="upload.html")


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request) -> Response:
    return templates.TemplateResponse(request=request, name="dashboard.html")
