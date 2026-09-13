"""Per-application dependencies, lazily created and explicitly owned."""

from contextlib import ExitStack
from functools import cached_property
from typing import Annotated, cast

import httpx
from fastapi import Depends, Request

from kakeibo.auth import SessionAuth
from kakeibo.config import Settings, load_settings
from kakeibo.repositories.dynamo import DynamoRepository
from kakeibo.repositories.s3 import S3Repository
from kakeibo.services.gemini import GeminiService
from kakeibo.services.receipts import ReceiptService
from kakeibo.services.summary import SummaryService


class AppContainer:
    """Reuse existing services; an injected HTTP client remains caller-owned.

    Construction performs no environment reads or AWS calls. Authentication and
    public routes never need to instantiate the persistence or Gemini clients.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings
        self._http_client = http_client
        self._resources = ExitStack()

    @cached_property
    def settings(self) -> Settings:
        return self._settings if self._settings is not None else load_settings()

    @cached_property
    def auth(self) -> SessionAuth:
        return SessionAuth(self.settings)

    @cached_property
    def dynamo(self) -> DynamoRepository:
        repository = DynamoRepository(
            self.settings.table_name, self.settings.aws_region
        )
        self._resources.callback(repository.table.meta.client.close)
        return repository

    @cached_property
    def s3(self) -> S3Repository:
        repository = S3Repository(self.settings.bucket_name, self.settings.aws_region)
        self._resources.callback(repository.client.close)
        return repository

    @cached_property
    def gemini(self) -> GeminiService:
        client = self._http_client
        if client is None:
            client = self._resources.enter_context(httpx.Client())
        return GeminiService(self.settings.gemini_api_key, client)

    @cached_property
    def receipts(self) -> ReceiptService:
        return ReceiptService(self.dynamo, self.s3)

    @cached_property
    def summary(self) -> SummaryService:
        return SummaryService(self.dynamo)

    def close(self) -> None:
        """Close only the clients constructed by this container."""
        self._resources.close()


def get_container(request: Request) -> AppContainer:
    return cast(AppContainer, request.app.state.container)


ContainerDependency = Annotated[AppContainer, Depends(get_container)]
