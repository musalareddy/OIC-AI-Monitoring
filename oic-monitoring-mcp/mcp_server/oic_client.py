from __future__ import annotations
import asyncio
import time
import logging
from typing import Any, Dict, Optional

import httpx
import base64
import zipfile
import io

from .settings import settings

# Setup logging for OIC client
logger = logging.getLogger(__name__)

class OAuth2Token:
    def __init__(self, access_token: str, expires_in: int, token_type: str = "Bearer") -> None:
        self.access_token = access_token
        self.token_type = token_type
        # refresh a bit before actual expiry
        self.expires_at_epoch = int(time.time()) + max(expires_in - 30, 0)

    def is_expired(self) -> bool:
        return time.time() >= self.expires_at_epoch


class OICClient:
    def __init__(self) -> None:
        self._token: Optional[OAuth2Token] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    def _create_client(self) -> httpx.AsyncClient:
        limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
        return httpx.AsyncClient(
            timeout=settings.http_timeout_secs,
            limits=limits,
            # Do NOT auto-follow redirects here. OIC's design-time gateway
            # 307-redirects requests to a shared "design.integration...*"
            # host (a different origin than oic_base_url). httpx's built-in
            # redirect handling strips the Authorization header on any
            # cross-host redirect (a standard security default), which
            # silently turns the follow-up request into an unauthenticated
            # one and produces a 401 that looks like a credentials/role
            # problem but isn't. We follow redirects manually below so the
            # bearer token is preserved for this known, trusted hop.
            follow_redirects=False,
        )

    def _ensure_client(self) -> None:
        if self._client is None:
            self._client = self._create_client()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ensure_token(self) -> str:
        self._ensure_client()
        async with self._lock:
            if self._token is not None and not self._token.is_expired():
                logger.debug("Using existing OAuth token")
                return self._token.access_token
            
            logger.info("Fetching new OAuth token")
            token_start_time = time.time()
            
            data = {
                "grant_type": "client_credentials",
                "client_id": settings.oauth_client_id,
                "client_secret": settings.oauth_client_secret,
            }
            if settings.oauth_scope:
                data["scope"] = settings.oauth_scope
            assert self._client is not None
            resp = await self._client.post(
                settings.oauth_token_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            payload = resp.json()
            access_token = payload.get("access_token")
            token_type = payload.get("token_type", "Bearer")
            expires_in = int(payload.get("expires_in", 3600))
            if not access_token:
                raise RuntimeError("OAuth token endpoint did not return access_token")
            self._token = OAuth2Token(access_token=access_token, expires_in=expires_in, token_type=token_type)
            
            token_time = time.time() - token_start_time
            logger.info(f"OAuth token fetched successfully in {token_time:.2f}s")
            return self._token.access_token

    def _with_instance_param(self, params: Optional[dict[str, Any]], include_instance: bool = True) -> dict[str, Any] | None:
        params = dict(params or {})
        from .settings import settings as _s
        if include_instance and _s.oic_instance_name:
            existing = params.get("integrationInstance")
            if existing is None:
                params["integrationInstance"] = _s.oic_instance_name
            elif isinstance(existing, list):
                if _s.oic_instance_name not in existing:
                    params["integrationInstance"] = [*existing, _s.oic_instance_name]
            elif existing != _s.oic_instance_name:
                params["integrationInstance"] = [existing, _s.oic_instance_name]
        return params or None

    async def _request(self, path: str, params: Optional[dict[str, Any]] = None, headers_override: Optional[dict[str, str]] = None, include_instance: bool = True) -> httpx.Response:
        self._ensure_client()
        token = await self._ensure_token()
        url = f"{settings.oic_base_url}{path}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if headers_override:
            headers.update(headers_override)
        
        request_start_time = time.time()
        logger.debug(f"Making HTTP request to: {url}")
        logger.debug(f"Request params: {params}")
        
        assert self._client is not None
        resp = await self._client.get(url, headers=headers, params=self._with_instance_param(params, include_instance=include_instance))

        # Manually follow same-tenant redirects (see follow_redirects=False
        # note in _create_client) so the Authorization header survives the
        # hop to OIC's design-time gateway host.
        redirect_hops = 0
        while resp.status_code in (301, 302, 303, 307, 308) and redirect_hops < 5:
            location = resp.headers.get("Location")
            if not location:
                break
            next_url = httpx.URL(location)
            if not next_url.is_absolute_url:
                next_url = resp.url.join(next_url)
            logger.debug(f"Following redirect ({resp.status_code}) to: {next_url}")
            resp = await self._client.get(str(next_url), headers=headers)
            redirect_hops += 1

        request_time = time.time() - request_start_time
        logger.info(f"HTTP request to {path} completed in {request_time:.2f}s (Status: {resp.status_code})")
        
        resp.raise_for_status()
        return resp

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None, include_instance: bool = True) -> Dict[str, Any] | Any:
        resp = await self._request(path, params, include_instance=include_instance)
        if resp.headers.get("Content-Type", "").startswith("application/json"):
            return resp.json()
        return resp.text

    async def _post(self, path: str, params: Optional[dict[str, Any]] = None, json_body: Optional[dict[str, Any]] = None, include_instance: bool = True) -> Dict[str, Any] | Any:
        self._ensure_client()
        token = await self._ensure_token()
        url = f"{settings.oic_base_url}{path}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        request_start_time = time.time()
        logger.debug(f"Making POST request to: {url}")
        logger.debug(f"POST params: {params}")
        logger.debug(f"POST body: {json_body}")

        assert self._client is not None
        resp = await self._client.post(url, headers=headers, params=self._with_instance_param(params, include_instance=include_instance), json=json_body)

        redirect_hops = 0
        while resp.status_code in (301, 302, 303, 307, 308) and redirect_hops < 5:
            location = resp.headers.get("Location")
            if not location:
                break
            next_url = httpx.URL(location)
            if not next_url.is_absolute_url:
                next_url = resp.url.join(next_url)
            logger.debug(f"Following redirect ({resp.status_code}) to: {next_url}")
            resp = await self._client.post(str(next_url), headers=headers, params=self._with_instance_param(params), json=json_body)
            redirect_hops += 1

        request_time = time.time() - request_start_time
        logger.info(f"HTTP POST to {path} completed in {request_time:.2f}s (Status: {resp.status_code})")
        resp.raise_for_status()

        if resp.headers.get("Content-Type", "").startswith("application/json"):
            return resp.json()
        return resp.text

    async def get_raw_path(self, path: str, params: Optional[dict[str, Any]] = None) -> Dict[str, Any]:
        resp = await self._request(path, params)
        content_type = resp.headers.get("Content-Type", "")
        body: Dict[str, Any] = {"contentType": content_type}
        if content_type.startswith("application/json"):
            body["json"] = resp.json()
        else:
            # Return text safely for non-JSON (XML, etc.)
            body["text"] = resp.text
        return body

    def _safe_items(self, payload: Any) -> list[Dict[str, Any]]:
        """Normalize OIC payloads that may wrap items in several different shapes."""
        if not isinstance(payload, dict):
            return []

        for key in ("items", "integrations", "projects"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

        for key in ("data", "content"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                nested_items = self._safe_items(nested)
                if nested_items:
                    return nested_items

        return []

    def _dedupe_integrations(self, integrations: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        seen: set[str] = set()
        result: list[Dict[str, Any]] = []
        for item in integrations:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or item.get("id") or item.get("name") or "")
            version = str(item.get("version") or "")
            key = f"{code}|{version}" if code else str(item)
            if not code or key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    async def list_projects(self, limit: int | None = None, page: int | None = None) -> Any:
        """List OIC projects first, then use their IDs to enumerate project-scoped integrations."""
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if page is not None:
            params["page"] = page

        project_paths = [
            "/ic/api/integration/v1/projects",
            "/ic/api/design/v1/projects",
        ]
        for path in project_paths:
            try:
                return await self._get(path, params=params or None)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (404, 405):
                    continue
                raise
        return {"items": [], "count": 0}

    async def get_project(self, project_id: str) -> Any:
        project_paths = [
            f"/ic/api/integration/v1/projects/{project_id}",
            f"/ic/api/design/v1/projects/{project_id}",
        ]
        for path in project_paths:
            try:
                return await self._get(path)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (404, 405):
                    continue
                raise
        return {"id": project_id, "items": []}

    async def list_project_connections(self, project_id: str, limit: int | None = None) -> Any:
        """Project-scoped connections list, when the tenant exposes per-project connection collections."""
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        paths = [
            f"/ic/api/integration/v1/projects/{project_id}/connections",
            f"/ic/api/design/v1/projects/{project_id}/connections",
        ]
        for path in paths:
            try:
                return await self._get(path, params=params or None)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (404, 405):
                    continue
                raise
        return {"items": [], "count": 0}

    async def get_project_connection(self, project_id: str, connection_id: str) -> Any:
        paths = [
            f"/ic/api/integration/v1/projects/{project_id}/connections/{connection_id}",
            f"/ic/api/design/v1/projects/{project_id}/connections/{connection_id}",
        ]
        for path in paths:
            try:
                return await self._get(path)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (404, 405):
                    continue
                raise
        return {"id": connection_id, "projectId": project_id, "items": []}

    async def list_project_packages(self, project_id: str, limit: int | None = None) -> Any:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        paths = [
            f"/ic/api/integration/v1/projects/{project_id}/packages",
            f"/ic/api/design/v1/projects/{project_id}/packages",
        ]
        for path in paths:
            try:
                return await self._get(path, params=params or None)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (404, 405):
                    continue
                raise
        return {"items": [], "count": 0}

    async def get_project_package(self, project_id: str, package_name: str) -> Any:
        paths = [
            f"/ic/api/integration/v1/projects/{project_id}/packages/{package_name}",
            f"/ic/api/design/v1/projects/{project_id}/packages/{package_name}",
        ]
        for path in paths:
            try:
                return await self._get(path)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (404, 405):
                    continue
                raise
        return {"projectId": project_id, "name": package_name, "items": []}

    async def list_project_lookups(self, project_id: str, limit: int | None = None) -> Any:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        paths = [
            f"/ic/api/integration/v1/projects/{project_id}/lookups",
            f"/ic/api/design/v1/projects/{project_id}/lookups",
        ]
        for path in paths:
            try:
                return await self._get(path, params=params or None)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (404, 405):
                    continue
                raise
        return {"items": [], "count": 0}

    async def list_project_libraries(self, project_id: str, limit: int | None = None) -> Any:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        paths = [
            f"/ic/api/integration/v1/projects/{project_id}/libraries",
            f"/ic/api/design/v1/projects/{project_id}/libraries",
        ]
        for path in paths:
            try:
                return await self._get(path, params=params or None)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (404, 405):
                    continue
                raise
        return {"items": [], "count": 0}

    async def list_project_instances(
        self,
        project_id: str,
        integration_id: Optional[str] = None,
        status: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        timewindow: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        """Project-scoped runtime instance listing using Oracle's monitoring contract.

        Oracle monitoring APIs are tenant-scoped; project selection is expressed
        within the q object as projectCode instead of via a project-specific path.
        """
        code, version = (integration_id.split("|", 1) + [None])[:2] if integration_id else (None, None)
        q = self._build_q_filter(
            projectCode=project_id,
            code=code,
            version=version,
            status=status,
            startdate=start_time,
            enddate=end_time,
            timewindow=timewindow or ("RETENTIONPERIOD" if (start_time or end_time or code) else "1h"),
        )
        params: dict[str, Any] = {}
        if q:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        return await self._get("/ic/api/integration/v1/monitoring/instances", params=params or None)

    async def get_project_instance(self, project_id: str, instance_id: str) -> Any:
        paths = [
            f"/ic/api/integration/v1/projects/{project_id}/monitoring/instances/{instance_id}",
            f"/ic/api/integration/v1/projects/{project_id}/instances/{instance_id}",
            f"/ic/api/integration/v1/monitoring/instances/{instance_id}",
        ]
        for path in paths:
            try:
                return await self._get(path)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (404, 405):
                    continue
                raise
        return {"id": instance_id, "projectId": project_id, "items": []}

    async def list_project_monitoring_instances(
        self,
        project_id: str,
        integration_id: Optional[str] = None,
        status: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        timewindow: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        """Project-aware monitoring list aligned with Oracle's q.projectCode contract."""
        code, version = (integration_id.split("|", 1) + [None])[:2] if integration_id else (None, None)
        q = self._build_q_filter(
            projectCode=project_id,
            code=code,
            version=version,
            status=status,
            startdate=start_time,
            enddate=end_time,
            timewindow=timewindow or ("RETENTIONPERIOD" if (start_time or end_time or code) else "1h"),
        )
        params: dict[str, Any] = {}
        if q:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        return await self._get("/ic/api/integration/v1/monitoring/instances", params=params or None)

    async def get_project_instance_monitoring(self, project_id: str, instance_id: str) -> Any:
        return await self._get(f"/ic/api/integration/v1/projects/{project_id}/monitoring/instances/{instance_id}", include_instance=False)

    async def list_project_integration_monitoring(
        self,
        project_id: str,
        integration_id: Optional[str] = None,
        status: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        timewindow: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        code, version = (integration_id.split("|", 1) + [None])[:2] if integration_id else (None, None)
        q = self._build_q_filter(
            projectCode=project_id,
            code=code,
            version=version,
            status=status,
            startdate=start_time,
            enddate=end_time,
            timewindow=timewindow or ("RETENTIONPERIOD" if (start_time or end_time or code) else "1h"),
        )
        params: dict[str, Any] = {}
        if q:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        return await self._get("/ic/api/integration/v1/monitoring/integrations", params=params or None)

    async def get_project_integration_monitoring(self, project_id: str, integration_id: str, version: Optional[str] = None) -> Any:
        code = integration_id
        ver = version or ""
        if "|" in integration_id and not version:
            code, ver = integration_id.split("|", 1)
        if ver:
            encoded = f"{code}%7C{ver}"
            return await self._get(f"/ic/api/integration/v1/projects/{project_id}/monitoring/integrations/{encoded}", include_instance=False)
        return await self._get(f"/ic/api/integration/v1/projects/{project_id}/monitoring/integrations/{code}", include_instance=False)

    async def get_project_message_count_summary(
        self,
        project_id: str,
        integration_id: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        timewindow: Optional[str] = None,
    ) -> Any:
        code, version = (integration_id.split("|", 1) + [None])[:2] if integration_id else (None, None)
        q = self._build_q_filter(
            projectCode=project_id,
            code=code,
            version=version,
            startdate=start_time,
            enddate=end_time,
            timewindow=timewindow or ("RETENTIONPERIOD" if (start_time or end_time or code) else "1h"),
        )
        params: dict[str, Any] = {}
        if q:
            params["q"] = q
        return await self._get("/ic/api/integration/v1/monitoring/integrations/messages/summary", params=params or None)

    async def list_project_audit_records(
        self,
        project_id: str,
        integration_id: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        timewindow: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        code, version = (integration_id.split("|", 1) + [None])[:2] if integration_id else (None, None)
        q = self._build_q_filter(
            projectCode=project_id,
            code=code,
            version=version,
            startdate=start_time,
            enddate=end_time,
            timewindow=timewindow or ("RETENTIONPERIOD" if (start_time or end_time or code) else "1h"),
        )
        params: dict[str, Any] = {}
        if q:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        return await self._get("/ic/api/integration/v1/monitoring/auditRecords", params=params or None)

    async def list_project_errors(
        self,
        project_id: str,
        integration_id: Optional[str] = None,
        timewindow: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        code, version = (integration_id.split("|", 1) + [None])[:2] if integration_id else (None, None)
        q = self._build_q_filter(
            projectCode=project_id,
            code=code,
            version=version,
            timewindow=timewindow or ("RETENTIONPERIOD" if code else "1h"),
        )
        params: dict[str, Any] = {}
        if q:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        return await self._get("/ic/api/integration/v1/monitoring/errors", params=params or None)

    async def get_project_error(self, project_id: str, error_id: str) -> Any:
        return await self._get(f"/ic/api/integration/v1/projects/{project_id}/monitoring/errors/{error_id}", include_instance=False)

    async def abort_project_instance(self, project_id: str, instance_id: str) -> Any:
        return await self._post(f"/ic/api/integration/v1/projects/{project_id}/monitoring/instances/{instance_id}/abort", include_instance=False)

    async def discard_project_error(self, project_id: str, error_id: str) -> Any:
        return await self._post(f"/ic/api/integration/v1/projects/{project_id}/monitoring/errors/{error_id}/discard", include_instance=False)

    async def discard_project_errors(
        self,
        project_id: str,
        integration_id: Optional[str] = None,
        timewindow: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        code, version = (integration_id.split("|", 1) + [None])[:2] if integration_id else (None, None)
        q = self._build_q_filter(projectCode=project_id, code=code, version=version, timewindow=timewindow or ("RETENTIONPERIOD" if code else "1h"))
        params: dict[str, Any] = {}
        if q:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        return await self._post("/ic/api/integration/v1/monitoring/errors/discard", params=params or None)

    async def resubmit_project_error(self, project_id: str, error_id: str) -> Any:
        return await self._post(f"/ic/api/integration/v1/projects/{project_id}/monitoring/errors/{error_id}/resubmit", include_instance=False)

    async def resubmit_project_errors(
        self,
        project_id: str,
        integration_id: Optional[str] = None,
        timewindow: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        code, version = (integration_id.split("|", 1) + [None])[:2] if integration_id else (None, None)
        q = self._build_q_filter(projectCode=project_id, code=code, version=version, timewindow=timewindow or ("RETENTIONPERIOD" if code else "1h"))
        params: dict[str, Any] = {}
        if q:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        return await self._post("/ic/api/integration/v1/monitoring/errors/resubmit", params=params or None)

    async def list_project_error_recovery_jobs(
        self,
        project_id: str,
        integration_id: Optional[str] = None,
        timewindow: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        code, version = (integration_id.split("|", 1) + [None])[:2] if integration_id else (None, None)
        q = self._build_q_filter(projectCode=project_id, code=code, version=version, timewindow=timewindow or ("RETENTIONPERIOD" if code else "1h"))
        params: dict[str, Any] = {}
        if q:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        return await self._get("/ic/api/integration/v1/monitoring/errors/recoveryJobs", params=params or None)

    async def get_project_error_recovery_job(self, project_id: str, recovery_job_id: str) -> Any:
        return await self._get(f"/ic/api/integration/v1/projects/{project_id}/monitoring/errors/recoveryJobs/{recovery_job_id}", include_instance=False)

    async def list_project_integrations(self, project_id: str, limit: int | None = None, page: int | None = None) -> Any:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if page is not None:
            params["page"] = page

        project_paths = [
            f"/ic/api/integration/v1/projects/{project_id}/integrations",
            f"/ic/api/design/v1/projects/{project_id}/integrations",
        ]
        for path in project_paths:
            try:
                return await self._get(path, params=params or None)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (404, 405):
                    continue
                raise
        return {"items": [], "count": 0}

    async def get_project_integration(self, project_id: str, integration_id: str, version: Optional[str] = None) -> Any:
        """Get a specific integration by code within a project.

        Oracle's project-scoped detail API uses the project path and the encoded
        integration identifier, e.g.:
          /ic/api/integration/v1/projects/{projectId}/integrations/{code}%7C{version}
        with the tenant instance preserved as the integrationInstance query param.
        """
        code = integration_id
        ver = version or ""
        if "|" in integration_id and not version:
            code, ver = integration_id.split("|", 1)
        if not ver:
            ver = await self.resolve_latest_version(code) or ""
            if not ver:
                raise httpx.HTTPStatusError("Version not found for code", request=None, response=httpx.Response(404))
        encoded = f"{code}%7C{ver}"
        return await self._get(f"/ic/api/integration/v1/projects/{project_id}/integrations/{encoded}", include_instance=True)

    async def _list_project_ids(self) -> list[str]:
        """Best-effort discovery of project IDs in OIC."""
        payload = await self.list_projects(limit=100)
        project_ids: list[str] = []
        for item in self._safe_items(payload):
            project_id = item.get("id") or item.get("projectId") or item.get("code") or item.get("name")
            if project_id and str(project_id) not in project_ids:
                project_ids.append(str(project_id))
        return project_ids

    async def _list_integrations_for_project(self, project_id: str) -> list[Dict[str, Any]]:
        payload = await self.list_project_integrations(project_id, limit=100)
        return self._safe_items(payload)

    async def list_integrations(self, only_activated: bool | None = None, limit: int | None = None, page: int | None = None) -> Any:
        logger.info(f"Calling list_integrations - only_activated: {only_activated}, limit: {limit}, page: {page}")
        
        params: dict[str, Any] = {}
        if only_activated is not None:
            params["onlyActivated"] = str(only_activated).lower()
        if limit is not None:
            params["limit"] = limit
        if page is not None:
            params["page"] = page
        
        start_time = time.time()
        result = await self._get("/ic/api/integration/v1/integrations", params=params or None)
        execution_time = time.time() - start_time
        
        # Log response details
        if isinstance(result, dict):
            items_count = len(result.get("items", [])) if "items" in result else 0
            total_results = result.get("totalResults", 0)
            has_more = result.get("hasMore", False)
            logger.info(f"list_integrations returned {items_count} items, total: {total_results}, hasMore: {has_more} in {execution_time:.2f}s")
        else:
            logger.warning(f"list_integrations returned non-dict result: {type(result)} in {execution_time:.2f}s")
        
        return result

    async def list_integrations_including_projects(self, only_activated: bool | None = None, limit: int | None = None, page: int | None = None) -> list[Dict[str, Any]]:
        """Explicit project-first flow: discover projects, then fetch each project's integrations, then merge with the root catalog."""
        merged_items: list[Dict[str, Any]] = []

        try:
            project_payload = await self.list_projects(limit=100)
            project_items = self._safe_items(project_payload)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Unable to enumerate projects: {e}")
            project_items = []

        project_ids: list[str] = []
        for project in project_items:
            project_id = project.get("id") or project.get("projectId") or project.get("code") or project.get("name")
            if project_id and str(project_id) not in project_ids:
                project_ids.append(str(project_id))

        for project_id in project_ids:
            try:
                project_payload = await self.list_project_integrations(project_id, limit=limit or 100)
                merged_items.extend(self._safe_items(project_payload))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Unable to read integrations for project {project_id}: {e}")

        root_integration_payload = await self.list_integrations(only_activated=only_activated, limit=limit, page=page)
        merged_items.extend(self._safe_items(root_integration_payload))
        merged_items = self._dedupe_integrations(merged_items)

        if limit is not None and page is None:
            return merged_items[:limit]
        if page is not None and limit is not None:
            start_index = (page - 1) * limit
            return merged_items[start_index:start_index + limit]
        return merged_items

    async def list_all_integrations(self, only_activated: bool | None = None, max_pages: int = 100, per_page: int = 100) -> list[Dict[str, Any]]:
        """
        Fetch all integrations across all pages, handling pagination issues.
        Returns a deduplicated list of all integrations found.
        """
        logger.info(f"Starting list_all_integrations - only_activated: {only_activated}, max_pages: {max_pages}, per_page: {per_page}")

        all_integrations = []
        seen_codes = set()  # Track seen integration codes to avoid duplicates
        page = 1
        total_start_time = time.time()
        consecutive_empty_pages = 0  # Track consecutive pages with no new integrations
        max_consecutive_empty = 3  # Stop after 3 consecutive pages with no new integrations

        try:
            project_items = await self.list_integrations_including_projects(only_activated=only_activated)
            for integration in project_items:
                code = integration.get("code") or integration.get("id")
                if code and code not in seen_codes:
                    seen_codes.add(code)
                    all_integrations.append(integration)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Project-aware integration merge failed; falling back to root instance listing only: {e}")

        while page <= max_pages:
            page_start_time = time.time()
            logger.info(f"Fetching page {page}/{max_pages}")
            
            try:
                data = await self.list_integrations(only_activated=only_activated, limit=per_page, page=page)
                
                # Handle different response structures
                items = []
                total_results = 0
                has_more = False
                
                if isinstance(data, dict):
                    # Handle OIC API response structure
                    if "items" in data:
                        items = data["items"]
                        total_results = data.get("totalResults", 0)
                        has_more = data.get("hasMore", False)
                    elif "content" in data and isinstance(data["content"], dict):
                        content = data["content"]
                        items = content.get("items", [])
                        total_results = content.get("totalResults", 0)
                        has_more = content.get("hasMore", False)
                    elif "data" in data and isinstance(data["data"], dict):
                        data_content = data["data"]
                        items = data_content.get("items", [])
                        total_results = data_content.get("totalResults", 0)
                        has_more = data_content.get("hasMore", False)
                
                logger.info(f"Page {page}: API returned {len(items)} items, totalResults: {total_results}, hasMore: {has_more}")
                
                if not items:
                    logger.info(f"No items found on page {page}, stopping pagination")
                    break  # No more items to fetch
                
                # Add only unique integrations (by code)
                new_integrations = []
                for item in items:
                    code = item.get("code")
                    if code and code not in seen_codes:
                        seen_codes.add(code)
                        new_integrations.append(item)
                
                all_integrations.extend(new_integrations)
                
                page_time = time.time() - page_start_time
                logger.info(f"Page {page}: found {len(items)} items, {len(new_integrations)} new unique, total so far: {len(all_integrations)} in {page_time:.2f}s")
                
                # Check if we got any new integrations
                if len(new_integrations) == 0:
                    consecutive_empty_pages += 1
                    logger.info(f"No new unique integrations on page {page}. Consecutive empty pages: {consecutive_empty_pages}")
                    
                    if consecutive_empty_pages >= max_consecutive_empty:
                        logger.info(f"Stopping pagination after {consecutive_empty_pages} consecutive pages with no new integrations")
                        break
                else:
                    consecutive_empty_pages = 0  # Reset counter when we find new integrations
                
                # Check if we've reached the total number of results
                if total_results > 0 and len(all_integrations) >= total_results:
                    logger.info(f"Reached total results limit: {len(all_integrations)} >= {total_results}")
                    break
                
                # Check if there are more pages
                if not has_more:
                    logger.info(f"No more pages indicated by API response (hasMore: {has_more})")
                    break
                
                page += 1
                
            except Exception as e:
                # Log error and break to avoid infinite loops
                page_time = time.time() - page_start_time
                logger.error(f"Error fetching page {page} after {page_time:.2f}s: {e}")
                break
        
        total_time = time.time() - total_start_time
        logger.info(f"list_all_integrations completed: fetched {len(all_integrations)} unique integrations from {page-1} pages in {total_time:.2f}s")
        
        return all_integrations

    async def resolve_latest_version(self, code: str, max_pages: int = 50, per_page: int = 100) -> Optional[str]:
        logger.info(f"Resolving latest version for code: {code}")
        start_time = time.time()
        
        # Use the new list_all_integrations method for better pagination handling
        all_integrations = await self.list_all_integrations(max_pages=max_pages, per_page=per_page)
        
        for integration in all_integrations:
            if integration.get("code") == code:
                version = integration.get("version")
                execution_time = time.time() - start_time
                logger.info(f"Found latest version for {code}: {version} in {execution_time:.2f}s")
                return version
        
        execution_time = time.time() - start_time
        logger.warning(f"No version found for code {code} in {execution_time:.2f}s")
        return None

    async def get_integration(self, identifier: str, version: str | None) -> Any:
        logger.info(f"Getting integration: {identifier}, version: {version}")
        start_time = time.time()
        
        # Accept 'CODE|VERSION' in identifier, or separate code + version; auto-resolve latest if version is empty
        code = identifier
        ver = version or ""
        if "|" in identifier and not version:
            code, ver = identifier.split("|", 1)
        if not ver:
            ver = await self.resolve_latest_version(code) or ""
            if not ver:
                raise httpx.HTTPStatusError("Version not found for code", request=None, response=httpx.Response(404))
        encoded = f"{code}%7C{ver}"
        result = await self._get(f"/ic/api/integration/v1/integrations/{encoded}")
        
        execution_time = time.time() - start_time
        logger.info(f"get_integration completed for {identifier} in {execution_time:.2f}s")
        return result

    async def export_integration(self, identifier: str, version: str | None, list_only: bool = False, max_preview_bytes: int = 8192) -> Dict[str, Any]:
        # Export archive (zip) of the integration
        code = identifier
        ver = version or ""
        if "|" in identifier and not version:
            code, ver = identifier.split("|", 1)
        if not ver:
            ver = await self.resolve_latest_version(code) or ""
            if not ver:
                raise httpx.HTTPStatusError("Version not found for code", request=None, response=httpx.Response(404))
        encoded = f"{code}%7C{ver}"
        resp = await self._request(
            f"/ic/api/integration/v1/integrations/{encoded}/archive",
            headers_override={"Accept": "application/zip"},
        )
        cd = resp.headers.get("Content-Disposition", "")
        file_name = None
        if "filename=" in cd:
            file_name = cd.split("filename=", 1)[-1].strip('"')
        content_b64 = base64.b64encode(resp.content).decode("ascii")
        result: Dict[str, Any] = {
            "contentType": resp.headers.get("Content-Type", ""),
            "fileName": file_name or f"{code}_{ver}.zip",
            "size": len(resp.content),
        }
        # Optionally list entries
        try:
            zf = zipfile.ZipFile(io.BytesIO(resp.content))
            entries = []
            for zi in zf.infolist():
                ent: Dict[str, Any] = {"name": zi.filename, "size": zi.file_size}
                if not list_only and zi.file_size <= max_preview_bytes and not zi.is_dir():
                    data = zf.read(zi)
                    # Try to decode as text for JSON/XML readability
                    try:
                        ent["textPreview"] = data.decode("utf-8", errors="replace")
                    except Exception:  # noqa: BLE001
                        pass
                entries.append(ent)
            result["entries"] = entries
        except Exception:  # noqa: BLE001
            # Keep just the archive
            pass
        if not list_only:
            result["archiveBase64"] = content_b64
        return result

    async def list_packages(self, limit: int | None = None) -> Any:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        # Some tenants expose packages under integration v1, others under design v1
        try:
            return await self._get("/ic/api/integration/v1/packages", params=params or None)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return await self._get("/ic/api/design/v1/packages", params=params or None)
            raise

    async def get_package(self, name: str) -> Any:
        try:
            return await self._get(f"/ic/api/integration/v1/packages/{name}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return await self._get(f"/ic/api/design/v1/packages/{name}")
            raise

    async def list_connections(self, limit: int | None = None) -> Any:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        return await self._get("/ic/api/integration/v1/connections", params=params or None)

    async def get_connection(self, identifier: str) -> Any:
        return await self._get(f"/ic/api/integration/v1/connections/{identifier}")

    def get_supported_api_catalog(self) -> Dict[str, list[str]]:
        """Return a compact project vs non-project API catalog for the OIC client."""
        return {
            "project": [
                "list_projects",
                "get_project",
                "list_project_integrations",
                "list_project_connections",
                "get_project_connection",
                "list_project_packages",
                "get_project_package",
                "list_project_lookups",
                "list_project_libraries",
                "list_project_instances",
                "get_project_instance",
                "list_project_monitoring_instances",
                "get_project_instance_monitoring",
                "list_project_integration_monitoring",
                "get_project_integration_monitoring",
                "get_project_message_count_summary",
                "list_project_audit_records",
                "list_project_errors",
                "get_project_error",
                "abort_project_instance",
                "discard_project_error",
                "discard_project_errors",
                "resubmit_project_error",
                "resubmit_project_errors",
                "list_project_error_recovery_jobs",
                "get_project_error_recovery_job",
                "list_integrations_including_projects",
            ],
            "non_project": [
                "list_integrations",
                "get_integration",
                "list_integration_monitoring",
                "get_integration_monitoring",
                "get_message_count_summary",
                "list_audit_records",
                "list_connections",
                "get_connection",
                "list_packages",
                "get_package",
                "list_lookups",
                "get_lookup",
                "list_libraries",
                "get_library",
                "list_adapters",
                "get_adapter",
                "list_agents",
                "list_agent_groups",
                "list_instances",
                "get_instance",
                "abort_instance",
                "get_error",
                "discard_error",
                "discard_errors",
                "resubmit_error",
                "resubmit_errors",
                "list_error_recovery_jobs",
                "get_error_recovery_job",
                "list_errors",
                "list_metrics",
                "list_schedules",
                "get_schedule",
                "search_integrations_by_pattern",
                "search_integration_by_name",
            ],
        }

    # New simple integration functions (similar to connections)
    async def list_integrations_simple(self, limit: int | None = None) -> Any:
        """Simple function to list integrations with optional limit, similar to list_connections"""
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        return await self._get("/ic/api/integration/v1/integrations", params=params or None)

    async def get_integration_simple(self, identifier: str) -> Any:
        """Simple function to get a specific integration by identifier, similar to get_connection"""
        return await self._get(f"/ic/api/integration/v1/integrations/{identifier}")

    async def search_integration_by_name(self, name: str, max_results: int | None = None) -> Any:
        """Search integrations by exact or partial name/code match, scanning the
        full catalogue (not just the first page).

        Always returns a list - [] when nothing matches, never None, so a
        caller can't mistake "found nothing" for "call failed". Exact
        matches are sorted first, but ALL matches are collected before
        returning: a match on page 2 is not lost because page 1 also had one.

        `max_results` caps how many matches are returned (default 50) - it
        does NOT limit how much of the catalogue gets searched. The OIC
        fetch page size is fixed at 100 regardless of max_results, so a
        small max_results can no longer silently shrink the search window.
        """
        logger.info(f"Searching for integration by name: {name}")
        name_lower = name.lower()
        cap = max_results or 50

        page = 1
        page_size = 100
        max_pages = 50  # safety cap: 50 * 100 = 5000 integrations
        exact_matches: list[Dict[str, Any]] = []
        partial_matches: list[Dict[str, Any]] = []

        while page <= max_pages:
            params = {"limit": page_size, "page": page}
            result = await self._get("/ic/api/integration/v1/integrations", params=params)

            if not (isinstance(result, dict) and "items" in result):
                logger.warning(f"Unexpected response format on page {page}")
                break

            integrations = result["items"]
            has_more = result.get("hasMore", False)
            logger.info(f"Page {page}: {len(integrations)} integrations, hasMore: {has_more}")

            if not integrations:
                break

            for integration in integrations:
                code = integration.get("code", "")
                integration_name = integration.get("name", "")
                if name_lower == code.lower() or name_lower == integration_name.lower():
                    exact_matches.append(integration)
                elif name_lower in code.lower() or name_lower in integration_name.lower():
                    partial_matches.append(integration)

            if not has_more:
                break
            page += 1

        all_matches = exact_matches + partial_matches
        logger.info(f"Found {len(exact_matches)} exact and {len(partial_matches)} partial matches for '{name}' across {page} page(s)")
        return all_matches[:cap]

    async def get_schedule(self, identifier: str, version: str | None = None) -> Any:
        """Get the schedule for a single integration.

        Current OIC REST API has no flat '/ic/api/integration/v1/schedules'
        endpoint. Schedules are nested per integration:
        GET /ic/api/integration/v1/integrations/{code}%7C{version}/schedule
        """
        code = identifier
        ver = version or ""
        if "|" in identifier and not version:
            code, ver = identifier.split("|", 1)
        if not ver:
            ver = await self.resolve_latest_version(code) or ""
            if not ver:
                raise httpx.HTTPStatusError("Version not found for code", request=None, response=httpx.Response(404))
        encoded = f"{code}%7C{ver}"
        return await self._get(f"/ic/api/integration/v1/integrations/{encoded}/schedule")

    async def list_schedules(self, only_activated: bool | None = True, limit: int | None = None) -> Any:
        """Best-effort aggregation of schedules across integrations.

        There is no single-call flat endpoint for this in the current OIC
        REST API, so this fetches the integration list, then calls
        get_schedule() per integration (capped by `limit`), skipping any
        integration that has no schedule (404) or errors.
        """
        max_integrations = limit or 50
        integrations = await self.list_all_integrations(only_activated=only_activated, max_pages=(max_integrations // 100) + 1, per_page=min(max_integrations, 100))
        integrations = integrations[:max_integrations]

        results: list[Dict[str, Any]] = []
        errors: list[Dict[str, Any]] = []
        for integ in integrations:
            code = integ.get("code")
            ver = integ.get("version")
            if not code or not ver:
                continue
            try:
                sched = await self.get_schedule(code, ver)
                results.append({"code": code, "version": ver, "schedule": sched})
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    continue  # not a scheduled integration
                errors.append({"code": code, "version": ver, "error": str(e)})
            except Exception as e:  # noqa: BLE001
                errors.append({"code": code, "version": ver, "error": str(e)})

        return {
            "items": results,
            "count": len(results),
            "checked": len(integrations),
            "errors": errors,
        }

    async def list_lookups(self, limit: int | None = None) -> Any:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        return await self._get("/ic/api/integration/v1/lookups", params=params or None)

    async def get_lookup(self, name: str) -> Any:
        try:
            return await self._get(f"/ic/api/integration/v1/lookups/{name}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return await self._get(f"/ic/api/design/v1/lookups/{name}")
            raise

    async def list_libraries(self, limit: int | None = None) -> Any:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        try:
            return await self._get("/ic/api/integration/v1/libraries", params=params or None)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return await self._get("/ic/api/design/v1/libraries", params=params or None)
            raise

    async def get_library(self, name: str) -> Any:
        try:
            return await self._get(f"/ic/api/integration/v1/libraries/{name}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return await self._get(f"/ic/api/design/v1/libraries/{name}")
            raise

    async def get_adapter(self, name: str) -> Any:
        # Some tenants expose adapter detail under adapters/{name}
        return await self._get(f"/ic/api/integration/v1/adapters/{name}")

    async def list_adapters(self) -> Any:
        return await self._get("/ic/api/integration/v1/adapters")

    async def list_agents(self) -> Any:
        """List connectivity agents.

        There is no flat '/ic/api/integration/v1/agents' listing endpoint
        in the current OIC REST API - only agent-group-scoped monitoring
        endpoints exist. Aggregate agents across all agent groups instead.
        """
        groups_resp = await self.list_agent_groups()
        group_items = groups_resp.get("items", []) if isinstance(groups_resp, dict) else []
        all_agents: list[Dict[str, Any]] = []
        for group in group_items:
            group_id = group.get("id") or group.get("agentGroupCode")
            if not group_id:
                continue
            try:
                agents_resp = await self._get(f"/ic/api/integration/v1/monitoring/agentgroups/{group_id}/agents")
                for agent in (agents_resp.get("items", []) if isinstance(agents_resp, dict) else []):
                    agent = dict(agent)
                    agent["agentGroupId"] = group_id
                    all_agents.append(agent)
            except httpx.HTTPStatusError:
                continue
        return {"items": all_agents, "count": len(all_agents)}

    async def list_agent_groups(self) -> Any:
        return await self._get("/ic/api/integration/v1/monitoring/agentgroups")

    def _build_q_filter(self, **kwargs: Any) -> Optional[str]:
        """Build OIC's `q` filter param, e.g. q={code : 'SC2RNSYNC', timewindow : '3d'}.

        Oracle's monitoring endpoints (instances, errors, metrics) do NOT
        accept flat query params like integrationId/status/startTime for
        filtering. They require a single `q` param holding a loosely-JSON
        object with unquoted keys and single-quoted string values. Passing
        those as flat params is silently ignored by OIC (no error - it just
        returns the unfiltered default-timewindow result set), which made
        this look like it "worked" while actually returning everything.
        """
        parts = []
        for key, value in kwargs.items():
            if value is None:
                continue
            parts.append(f"{key} : '{value}'")
        if not parts:
            return None
        return "{" + ", ".join(parts) + "}"

    # Monitoring endpoints (names can vary; try common patterns)
    async def list_instances(
        self,
        integration_id: Optional[str] = None,
        status: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        timewindow: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        # integration_id may be 'CODE' or 'CODE|VERSION'
        code, version = (integration_id.split("|", 1) + [None])[:2] if integration_id else (None, None)
        q = self._build_q_filter(
            code=code,
            version=version,
            status=status,
            startdate=start_time,
            enddate=end_time,
            # OIC defaults to a 1h window; without an explicit filter that
            # silently limits results to "whatever ran in the last hour"
            # even when you asked for a specific integration/date range.
            timewindow=timewindow or ("RETENTIONPERIOD" if (start_time or end_time or code) else "1h"),
        )
        params: dict[str, Any] = {}
        if q:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        try:
            return await self._get("/ic/api/integration/v1/monitoring/instances", params=params or None)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return await self._get("/ic/api/monitoring/v1/instances", params=params or None)
            raise

    async def get_instance(self, instance_id: str) -> Any:
        try:
            return await self._get(f"/ic/api/integration/v1/monitoring/instances/{instance_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return await self._get(f"/ic/api/monitoring/v1/instances/{instance_id}")
            raise

    async def get_instance_activity_stream(self, instance_id: str) -> Any:
        """Flow/activity log for a single runtime instance (the step-by-step
        execution trace shown in the OIC Monitoring UI for an instance)."""
        return await self._get(f"/ic/api/integration/v1/monitoring/instances/{instance_id}/activityStream")

    async def get_instance_activity_stream_details(
        self,
        instance_id: str,
        timezone: Optional[str] = None,
        integration_instance: Optional[str] = None,
    ) -> Any:
        """Details for the activity stream of a single instance, including the
        timezone and integrationInstance parameters used by the OIC UI and API.
        """
        params: dict[str, Any] = {}
        if timezone:
            params["timezone"] = timezone
        if integration_instance:
            params["integrationInstance"] = integration_instance
        return await self._get(
            f"/ic/api/integration/v1/monitoring/instances/{instance_id}/activityStreamDetails",
            params=params or None,
        )

    async def list_integration_monitoring(
        self,
        integration_id: Optional[str] = None,
        status: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        timewindow: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        """Retrieve monitoring data for integrations (documented under /monitoring/integrations)."""
        code, version = (integration_id.split("|", 1) + [None])[:2] if integration_id else (None, None)
        q = self._build_q_filter(
            code=code,
            version=version,
            status=status,
            startdate=start_time,
            enddate=end_time,
            timewindow=timewindow or ("RETENTIONPERIOD" if (start_time or end_time or code) else "1h"),
        )
        params: dict[str, Any] = {}
        if q:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        return await self._get("/ic/api/integration/v1/monitoring/integrations", params=params or None)

    async def get_integration_monitoring(self, integration_id: str, version: Optional[str] = None) -> Any:
        code = integration_id
        ver = version or ""
        if "|" in integration_id and not version:
            code, ver = integration_id.split("|", 1)
        if not ver:
            ver = await self.resolve_latest_version(code) or ""
        if ver:
            encoded = f"{code}%7C{ver}"
            return await self._get(f"/ic/api/integration/v1/monitoring/integrations/{encoded}")
        return await self._get(f"/ic/api/integration/v1/monitoring/integrations/{code}")

    async def get_message_count_summary(
        self,
        integration_id: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        timewindow: Optional[str] = None,
    ) -> Any:
        code, version = (integration_id.split("|", 1) + [None])[:2] if integration_id else (None, None)
        q = self._build_q_filter(
            code=code,
            version=version,
            startdate=start_time,
            enddate=end_time,
            timewindow=timewindow or ("RETENTIONPERIOD" if (start_time or end_time or code) else "1h"),
        )
        params: dict[str, Any] = {}
        if q:
            params["q"] = q
        return await self._get("/ic/api/integration/v1/monitoring/integrations/messages/summary", params=params or None)

    async def list_audit_records(
        self,
        integration_id: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        timewindow: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        code, version = (integration_id.split("|", 1) + [None])[:2] if integration_id else (None, None)
        q = self._build_q_filter(
            code=code,
            version=version,
            startdate=start_time,
            enddate=end_time,
            timewindow=timewindow or ("RETENTIONPERIOD" if (start_time or end_time or code) else "1h"),
        )
        params: dict[str, Any] = {}
        if q:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        return await self._get("/ic/api/integration/v1/monitoring/auditRecords", params=params or None)

    async def abort_instance(self, instance_id: str) -> Any:
        return await self._post(f"/ic/api/integration/v1/monitoring/instances/{instance_id}/abort")

    async def get_error(self, error_id: str) -> Any:
        return await self._get(f"/ic/api/integration/v1/monitoring/errors/{error_id}")

    async def discard_error(self, error_id: str) -> Any:
        return await self._post(f"/ic/api/integration/v1/monitoring/errors/{error_id}/discard")

    async def discard_errors(self, integration_id: Optional[str] = None, timewindow: Optional[str] = None, limit: Optional[int] = None) -> Any:
        code, version = (integration_id.split("|", 1) + [None])[:2] if integration_id else (None, None)
        q = self._build_q_filter(code=code, version=version, timewindow=timewindow or ("RETENTIONPERIOD" if code else "1h"))
        params: dict[str, Any] = {}
        if q:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        return await self._post("/ic/api/integration/v1/monitoring/errors/discard", params=params or None)

    async def resubmit_error(self, error_id: str) -> Any:
        return await self._post(f"/ic/api/integration/v1/monitoring/errors/{error_id}/resubmit")

    async def resubmit_errors(self, integration_id: Optional[str] = None, timewindow: Optional[str] = None, limit: Optional[int] = None) -> Any:
        code, version = (integration_id.split("|", 1) + [None])[:2] if integration_id else (None, None)
        q = self._build_q_filter(code=code, version=version, timewindow=timewindow or ("RETENTIONPERIOD" if code else "1h"))
        params: dict[str, Any] = {}
        if q:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        return await self._post("/ic/api/integration/v1/monitoring/errors/resubmit", params=params or None)

    async def list_error_recovery_jobs(
        self,
        integration_id: Optional[str] = None,
        timewindow: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        code, version = (integration_id.split("|", 1) + [None])[:2] if integration_id else (None, None)
        q = self._build_q_filter(code=code, version=version, timewindow=timewindow or ("RETENTIONPERIOD" if code else "1h"))
        params: dict[str, Any] = {}
        if q:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        return await self._get("/ic/api/integration/v1/monitoring/errors/recoveryJobs", params=params or None)

    async def get_error_recovery_job(self, recovery_job_id: str) -> Any:
        return await self._get(f"/ic/api/integration/v1/monitoring/errors/recoveryJobs/{recovery_job_id}")

    async def list_errors(self, integration_id: Optional[str] = None, timewindow: Optional[str] = None, limit: Optional[int] = None) -> Any:
        code, version = (integration_id.split("|", 1) + [None])[:2] if integration_id else (None, None)
        q = self._build_q_filter(
            code=code,
            version=version,
            timewindow=timewindow or ("RETENTIONPERIOD" if code else "1h"),
        )
        params: dict[str, Any] = {}
        if q:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        try:
            return await self._get("/ic/api/integration/v1/monitoring/errors", params=params or None)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return await self._get("/ic/api/monitoring/v1/errors", params=params or None)
            raise

    async def list_metrics(self, frequency: str = "hourly", start_time: Optional[str] = None, end_time: Optional[str] = None, timewindow: Optional[str] = None) -> Any:
        """Historical tracking metrics (message counts, success rate, etc.).

        NOTE: there is no '/ic/api/integration/v1/monitoring/metrics'
        endpoint in the current OIC REST API - that was a made-up path.
        The real endpoint is '/ic/api/integration/v1/monitoring/history',
        which takes `frequency` ('hourly' or 'daily') as a flat param and
        startdate/enddate/timewindow via the same `q` filter object used
        by the other monitoring endpoints - not a `metric` param and not
        flat startTime/endTime params.
        """
        q = self._build_q_filter(startdate=start_time, enddate=end_time, timewindow=timewindow)
        params: dict[str, Any] = {"frequency": frequency}
        if q:
            params["q"] = q
        return await self._get("/ic/api/integration/v1/monitoring/history", params=params)

    async def list_integrations_comprehensive(self, only_activated: bool | None = None, max_pages: int = 200, per_page: int = 100) -> list[Dict[str, Any]]:
        """
        Comprehensive method to fetch all integrations using multiple approaches.
        Tries different API parameters and endpoints to ensure complete coverage.
        """
        logger.info(f"Starting comprehensive integration fetch - only_activated: {only_activated}, max_pages: {max_pages}, per_page: {per_page}")
        
        all_integrations = []
        seen_codes = set()
        
        # Try different approaches to fetch integrations
        approaches = [
            {"only_activated": only_activated, "max_pages": max_pages, "per_page": per_page},
            {"only_activated": None, "max_pages": max_pages, "per_page": per_page},  # Try without activation filter
            {"only_activated": only_activated, "max_pages": max_pages, "per_page": 50},  # Try smaller page size
            {"only_activated": None, "max_pages": max_pages, "per_page": 50},  # Try without filter and smaller page size
        ]
        
        for i, approach in enumerate(approaches):
            logger.info(f"Trying approach {i+1}/{len(approaches)}: {approach}")
            
            try:
                integrations = await self.list_all_integrations(**approach)
                
                # Add new integrations
                new_count = 0
                for integration in integrations:
                    code = integration.get("code")
                    if code and code not in seen_codes:
                        seen_codes.add(code)
                        all_integrations.append(integration)
                        new_count += 1
                
                logger.info(f"Approach {i+1} found {len(integrations)} integrations, {new_count} new unique")
                
                # If we found a significant number of integrations, we might have good coverage
                if len(integrations) > 100:
                    logger.info(f"Found substantial number of integrations ({len(integrations)}), this approach seems effective")
                
            except Exception as e:
                logger.error(f"Approach {i+1} failed: {e}")
                continue
        
        logger.info(f"Comprehensive fetch completed: {len(all_integrations)} total unique integrations found")
        return all_integrations

    async def search_integrations_by_pattern(self, pattern: str, max_pages: int = 50, per_page: int = 100) -> list[Dict[str, Any]]:
        """
        Search for integrations by pattern in code, name, or description.
        This is a client-side search after fetching integrations.
        """
        logger.info(f"Searching integrations by pattern: {pattern}")
        
        # First get all integrations
        all_integrations = await self.list_all_integrations(max_pages=max_pages, per_page=per_page)
        
        # Filter by pattern
        pattern_lower = pattern.lower()
        matched_integrations = []
        
        for integration in all_integrations:
            code = integration.get("code", "").lower()
            name = integration.get("name", "").lower()
            description = integration.get("description", "").lower()
            
            if (pattern_lower in code or 
                pattern_lower in name or 
                pattern_lower in description):
                matched_integrations.append(integration)
        
        logger.info(f"Pattern search '{pattern}' found {len(matched_integrations)} matches out of {len(all_integrations)} total integrations")
        return matched_integrations


oic_client_singleton = OICClient() 