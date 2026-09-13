"""FastAPI application factory and AWS Lambda entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from mangum import Mangum

from kakeibo.auth import AuthMiddleware
from kakeibo.config import Settings
from kakeibo.container import AppContainer
from kakeibo.routers import export, pages, receipts, summary

PACKAGE_DIRECTORY = Path(__file__).resolve().parent


def create_app(
    settings: Settings | None = None, *, container: AppContainer | None = None
) -> FastAPI:
    """Build without AWS or secrets; load configuration at application startup.

    Inject either settings or a caller-owned container. Mangum runs the lifespan
    on each invocation, so each lifespan receives fresh owned clients.
    """
    if settings is not None and container is not None:
        raise ValueError("Inject settings or container, not both")

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        dependencies = container if container is not None else AppContainer(settings)
        application.state.container = dependencies
        try:
            # Validate configuration without creating any external clients.
            _ = dependencies.auth
            yield
        finally:
            if container is None:
                dependencies.close()

    app = FastAPI(title="kakeibo", lifespan=lifespan)
    app.state.container = container if container is not None else AppContainer(settings)
    app.add_middleware(AuthMiddleware, auth=lambda: app.state.container.auth)
    app.mount(
        "/static", StaticFiles(directory=PACKAGE_DIRECTORY / "static"), name="static"
    )
    for router in (pages.router, receipts.router, summary.router, export.router):
        app.include_router(router)
    return app


app = create_app()
# Lazy dependencies guard module import from environment reads and AWS access.
handler = Mangum(app)
