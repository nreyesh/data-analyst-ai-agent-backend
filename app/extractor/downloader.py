"""Downloader for Kastor building expense reports and unit billing notes."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Tuple

import httpx
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import storage

from backend.app.config import CONFIG

logger = logging.getLogger("backend.extractor.downloader")

KASTOR_BUILDING_URL = "https://dag-ms.kastor.cl/edificio_siplaza/minutas/pdf_mail/{year}-{month}"
KASTOR_UNIT_URL = "https://dag-ms.kastor.cl/edificio_siplaza/boletas/boletas/{kastor_id}/{year}/{month}/1"


class Downloader:
    """Fetches PDF documents from Kastor web endpoints and persists them to GCS or local disk."""

    def __init__(self, bucket_name: str | None = None, local_fallback_dir: str | Path = "data"):
        self.bucket_name = (
            bucket_name
            or os.getenv("STORAGE_BUCKET")
            or CONFIG.get("storage", {}).get("bucket", "building-analytics-data-dev")
        )
        self.local_dir = Path(local_fallback_dir)
        self._storage_client: storage.Client | None = None
        self._gcs_available: bool = True

    @property
    def storage_client(self) -> storage.Client | None:
        if self._storage_client is None and self._gcs_available:
            try:
                self._storage_client = storage.Client()
            except (DefaultCredentialsError, Exception) as exc:
                logger.warning("GCS client initialization failed (%s). Falling back to local storage.", exc)
                self._gcs_available = False
        return self._storage_client

    def save_pdf(self, pdf_bytes: bytes, destination_path: str) -> str:
        """Persist PDF bytes to GCS, falling back to local disk if GCS is unavailable."""
        client = self.storage_client
        if client and self._gcs_available:
            try:
                bucket = client.bucket(self.bucket_name)
                blob = bucket.blob(destination_path)
                blob.upload_from_string(pdf_bytes, content_type="application/pdf")
                gcs_uri = f"gs://{self.bucket_name}/{destination_path}"
                logger.info("Saved PDF to GCS: %s", gcs_uri)
                return gcs_uri
            except Exception as exc:
                logger.warning("Failed uploading to GCS (%s). Saving locally.", exc)

        local_target = self.local_dir / destination_path
        local_target.parent.mkdir(parents=True, exist_ok=True)
        local_target.write_bytes(pdf_bytes)
        logger.info("Saved PDF to local disk: %s", local_target)
        return str(local_target.resolve())

    async def download_building_pdf(
        self, year: int, month: int, client: httpx.AsyncClient | None = None
    ) -> Tuple[bytes, str]:
        """Download general building expenses PDF from Kastor."""
        url = KASTOR_BUILDING_URL.format(year=year, month=month)
        return await self._fetch_and_save(url, f"building/{year}-{month:02d}.pdf", client)

    async def download_unit_pdf(
        self,
        kastor_id: int,
        unit_code: str,
        year: int,
        month: int,
        client: httpx.AsyncClient | None = None,
    ) -> Tuple[bytes, str]:
        """Download unit billing statement ('Nota de Cobro') PDF from Kastor."""
        url = KASTOR_UNIT_URL.format(kastor_id=kastor_id, year=year, month=month)
        return await self._fetch_and_save(url, f"units/{unit_code}/{year}-{month:02d}.pdf", client)

    async def _fetch_and_save(
        self, url: str, destination_path: str, client: httpx.AsyncClient | None = None
    ) -> Tuple[bytes, str]:
        """Internal helper to stream from Kastor with retry and save to persistent storage."""
        if client is not None:
            return await self._do_fetch(client, url, destination_path)

        transport = httpx.AsyncHTTPTransport(retries=3)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True, timeout=30.0) as http_client:
            return await self._do_fetch(http_client, url, destination_path)

    async def _do_fetch(self, client: httpx.AsyncClient, url: str, destination_path: str) -> Tuple[bytes, str]:
        resp = await client.get(url)
        if resp.status_code == 404:
            raise FileNotFoundError(f"Document not found (404) for {destination_path}")
        resp.raise_for_status()

        if not resp.content.startswith(b"%PDF"):
            raise ValueError(
                f"Remote source returned non-PDF response (content-type={resp.headers.get('content-type')}) for {destination_path}"
            )

        saved_uri = self.save_pdf(resp.content, destination_path)
        return resp.content, saved_uri
