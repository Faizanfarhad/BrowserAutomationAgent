from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.async_api import Page


async def download_url(
    page: Page,
    url: str,
    download_dir: str | Path = "downloads",
) -> Path:
    """Fetch a direct file URL and save its response body."""
    parsed_url = urlsplit(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("Download URL must be an absolute HTTP or HTTPS URL")

    response = await page.request.get(url)
    try:
        if not response.ok:
            raise RuntimeError(
                f"Download request failed with HTTP {response.status}: {url}"
            )
        
        content_type = response.headers.get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type == "text/html":
            raise ValueError("Download URL returned an HTML page, not a file")

        filename = None
        content_disposition = response.headers.get("content-disposition")
        if content_disposition:
            disposition = Message()
            disposition["content-disposition"] = content_disposition
            filename = disposition.get_filename()

        if not filename:
            filename = Path(unquote(parsed_url.path)).name
        filename = Path(filename.replace("\\", "/")).name
        if not filename or filename in {".", ".."}:
            filename = "download"
        if media_type == "application/pdf" and Path(filename).suffix.lower() != ".pdf":
            filename = f"{filename}.pdf"

        destination = Path(download_dir)
        destination.mkdir(parents=True, exist_ok=True)
        output_path = destination / filename
        output_path.write_bytes(await response.body())
        return output_path
    finally:
        await response.dispose()