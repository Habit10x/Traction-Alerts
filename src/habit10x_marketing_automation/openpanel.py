"""Thin client for the OpenPanel read API (Insights + Export).

Auth is two headers (`openpanel-client-id`, `openpanel-client-secret`) and needs a
client of type `read` or `root`. Endpoint parameters/response shapes are returned as raw
JSON on purpose: OpenPanel's public docs don't pin them down, so we inspect real responses
(`probe` / `discover-openpanel` commands) before building detection on top of them.
"""

from typing import Any

import httpx

from .config import MissingSettingError, Settings


class OpenPanelError(RuntimeError):
    pass


class OpenPanel:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        settings.require("openpanel_client_id", "openpanel_client_secret")
        self._project_id = settings.openpanel_project_id
        self._http = httpx.Client(
            base_url=settings.openpanel_api_url.rstrip("/"),
            headers={
                "openpanel-client-id": settings.openpanel_client_id.get_secret_value(),  # type: ignore[union-attr]
                "openpanel-client-secret": settings.openpanel_client_secret.get_secret_value(),  # type: ignore[union-attr]
            },
            timeout=30,
            transport=transport or httpx.HTTPTransport(retries=2),
        )

    @property
    def project_id(self) -> str:
        """Required only by project-scoped calls, so tools like `probe /manage/projects` work without it."""
        if not self._project_id:
            raise MissingSettingError("Missing in .env: OPENPANEL_PROJECT_ID")
        return self._project_id

    def get(self, path: str, **params: Any) -> Any:
        """GET `path` with query params (None values dropped, lists sent as repeated keys)."""
        clean = {k: v for k, v in params.items() if v is not None}
        response = self._http.get(path, params=clean)
        if response.status_code in (401, 403):
            raise OpenPanelError(
                f"{response.status_code} from OpenPanel: check the client id/secret and that the "
                "client type is 'read' or 'root' (a 'write' client cannot use the read API)."
            )
        if response.status_code >= 400:
            raise OpenPanelError(f"{response.status_code} for GET {path}: {response.text[:300]}")
        return response.json()

    def insights(self, endpoint: str, **params: Any) -> Any:
        """GET /insights/{projectId}/{endpoint}, e.g. insights("live"), insights("pages", range=...)."""
        return self.get(f"/insights/{self.project_id}/{endpoint.lstrip('/')}", **params)

    def export_events(self, **params: Any) -> Any:
        """GET /export/events (raw events). Pass includes=["properties"] to get event properties."""
        return self.get("/export/events", projectId=self.project_id, **params)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "OpenPanel":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
