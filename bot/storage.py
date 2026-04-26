from __future__ import annotations
import os
import uuid
import aiohttp


async def upload_attachment(url: str, content_type: str | None = None) -> str:
    """Download Discord attachment and re-upload to Supabase Storage. Returns public URL."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as r:
            r.raise_for_status()
            data = await r.read()
            ct = content_type or r.headers.get("content-type", "application/octet-stream")

    ext = (url.rsplit(".", 1)[-1].split("?")[0] or "jpg").lower()
    if len(ext) > 6:
        ext = "jpg"
    key = f"{uuid.uuid4().hex}.{ext}"

    bucket = os.environ["SUPABASE_BUCKET"]
    sb_url = os.environ["SUPABASE_URL"]
    sb_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{sb_url}/storage/v1/object/{bucket}/{key}",
            data=data,
            headers={
                "Authorization": f"Bearer {sb_key}",
                "Content-Type": ct,
                "x-upsert": "false",
            },
        ) as r:
            r.raise_for_status()

    return f"{sb_url}/storage/v1/object/public/{bucket}/{key}"
