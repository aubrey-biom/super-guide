"""Fetch Google Sheets as .xlsx with the service account (for GitHub Actions and the CLI).

In a Claude Code session the Drive connector does this interactively; in the
shared runner the read-only BigQuery service account does it, once the sheets
are shared with its e-mail and the Drive API is enabled on the project:

    gcloud services enable drive.googleapis.com sheets.googleapis.com --project biom-reporting-s26
    # then share each sheet (Viewer) and the output folder (Editor) with
    # claude-code-bq-readonly@biom-reporting-s26.iam.gserviceaccount.com

Always export as .xlsx: the plain-text rendering truncates long tabs.
Requires `google-api-python-client` and `google-auth` (optional dependency
group `drive`).
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from shipcast import bq

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
WRITE_SCOPES = ["https://www.googleapis.com/auth/drive"]

KNOWN_SHEETS: dict[str, str] = {
    "rdz_inventory": "1y3-daeuMeQVkLYRP89go9KfXMjBwfT-_VyzeMqwczD8",
    "inbound_freight_tracker": "1va-c_gGmH6DwDrqqE7oy28xmjVlcDk5R70Kq7AiKpdI",
    "bm_master_forecast": "1mua2RZHEjf-pFh7I8pV2PhvGDw8HsHfI",
}


def _service(scopes: list[str]) -> Any:
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as e:  # pragma: no cover - optional dependency
        raise RuntimeError("install the `drive` extra: uv sync --extra drive") from e
    path, _ = bq.resolve_credentials()
    if path is None:
        raise RuntimeError(
            "Drive export needs a service-account key file (GOOGLE_APPLICATION_CREDENTIALS or GCP_SA_KEY_B64)"
        )
    creds = service_account.Credentials.from_service_account_file(str(path), scopes=scopes)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def export_xlsx(file_id: str, dest: str | Path) -> Path:
    """Export a Google Sheet (or download an uploaded .xlsx) to `dest`."""
    svc = _service(SCOPES)
    meta = svc.files().get(fileId=file_id, fields="mimeType,name,modifiedTime").execute()
    if meta["mimeType"] == "application/vnd.google-apps.spreadsheet":
        data = svc.files().export(fileId=file_id, mimeType=XLSX).execute()
    else:
        data = svc.files().get_media(fileId=file_id).execute()
    out = Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data if isinstance(data, bytes) else io.BytesIO(data).getvalue())
    (out.with_suffix(out.suffix + ".meta.txt")).write_text(
        f"{meta['name']}\nmodifiedTime={meta['modifiedTime']}\n"
    )
    return out


def fetch_known(
    dest_dir: str | Path, names: tuple[str, ...] = tuple(KNOWN_SHEETS)
) -> dict[str, Path]:
    """Export every known sheet into `dest_dir/<name>.xlsx`."""
    d = Path(dest_dir)
    return {n: export_xlsx(KNOWN_SHEETS[n], d / f"{n}.xlsx") for n in names}


def upload_to_folder(local: str | Path, folder_id: str, *, name: str | None = None) -> str:
    """Upload a file into a Drive folder the service account can edit; returns the file id.

    Service accounts have no storage quota of their own: the folder must be in a
    Shared Drive (add the account as Content manager) or the upload fails with
    `storageQuotaExceeded`. See README.
    """
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("install the `drive` extra: uv sync --extra drive") from e
    svc = _service(WRITE_SCOPES)
    p = Path(local)
    media = MediaFileUpload(str(p), mimetype=XLSX, resumable=False)
    body = {"name": name or p.name, "parents": [folder_id]}
    created = (
        svc.files()
        .create(body=body, media_body=media, fields="id", supportsAllDrives=True)
        .execute()
    )
    return str(created["id"])


__all__ = ["KNOWN_SHEETS", "XLSX", "export_xlsx", "fetch_known", "upload_to_folder"]
