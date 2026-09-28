"""OCR images with the local Tesseract executable."""

from __future__ import annotations

import io
import subprocess
from typing import Callable

from PIL import Image, UnidentifiedImageError

from .config import Settings


class OcrError(RuntimeError):
    pass


def recognize_image(data: bytes | Image.Image, settings: Settings, *,
                    check_deadline: Callable[[], None] | None = None) -> str:
    if not settings.ocr_enabled:
        return ""
    try:
        image = Image.open(io.BytesIO(data)) if isinstance(data, bytes) else data
        if image.width * image.height > settings.max_page_pixels:
            raise OcrError("LIMIT_EXCEEDED: image exceeds the configured pixel limit")
        image.load()
        with io.BytesIO() as buffer:
            image.convert("RGB").save(buffer, format="PNG")
            payload = buffer.getvalue()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise OcrError(f"OCR_IMAGE_ERROR: {exc}") from exc

    def recognize(languages: str) -> str:
        if check_deadline:
            check_deadline()
        args = [
            str(settings.tesseract), "stdin", "stdout", "--tessdata-dir",
            str(settings.tessdata_dir), "-l", languages,
        ]
        try:
            result = subprocess.run(
                args,
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=settings.ocr_timeout,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise OcrError("TIMEOUT: Tesseract exceeded the page timeout") from exc
        except OSError as exc:
            raise OcrError(f"DEPENDENCY_UNAVAILABLE: {exc}") from exc
        if result.returncode:
            message = result.stderr.decode("utf-8", errors="replace")[-500:]
            raise OcrError(f"OCR_ERROR: {message.strip()}")
        return result.stdout.decode("utf-8", errors="replace").strip()

    primary = recognize("+".join(settings.ocr_languages))
    if image.height > image.width * 1.4 and len(primary.replace(" ", "").replace("\n", "")) < 10:
        vertical = recognize(settings.vertical_language)
        if len(vertical) > len(primary):
            return vertical
    return primary
