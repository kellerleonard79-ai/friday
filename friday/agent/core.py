"""
agent/core.py
What is left of the agent once the model call moved out.

The transport seam is gone from this file. Every LLM call in Friday now goes
through llm/dispatch.py, and the only file that imports google.genai is
llm/providers/gemini.py. FridayAgent.complete() is deleted, not deprecated —
a second door to the model is a door something eventually walks through.

What remains is the media intake path: PDF rasterization and the byte/mime
plumbing that the EXTRACT profile will need in step 2. It reaches no model
today and says so.
"""

import logging

logger = logging.getLogger("friday.core")


# Only the first few PDF pages are rasterized for the vision model — flyers
# and schedules front-load their content, and each page adds latency + payload.
_PDF_MAX_PAGES = 3


def _pdf_to_png_pages(pdf_bytes: bytes) -> list[bytes]:
    """Rasterize a PDF (from bytes) into one PNG per page, capped at
    _PDF_MAX_PAGES. Raises on unreadable/corrupt input."""
    import fitz  # PyMuPDF

    pages = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for i in range(min(doc.page_count, _PDF_MAX_PAGES)):
            pages.append(doc[i].get_pixmap(dpi=150).tobytes("png"))
    return pages


class FridayAgent:
    """Media intake, and the late-bound Telegram handle it replies through.

    No model call lives here any more. When extraction comes back in step 2 it
    builds an LLMRequest and calls llm.dispatch — it does not resurrect a
    provider client on this class.
    """

    def __init__(self, config: dict, conn=None):
        self._config = config
        self._conn   = conn
        self.telegram_handler = None  # bound by friday.py after construction

    # ── Media → calendar event extraction ─────────────────────────────────

    def on_media(self, file_bytes: bytes, mime_type: str,
                 caption: str | None = None) -> None:
        """Accept a photo or PDF and tell the user extraction is offline.

        TORN DOWN: the prompt that asked the model for a JSON event, and
        _parse_media_event which read it back, are gone. What survives is
        everything below the prompt line — PDF rasterization, the byte/mime
        plumbing, and the corrupt/empty-file branches — because the rewrite
        needs all of it unchanged and because a corrupt PDF should still be
        named as a corrupt PDF rather than as an offline feature.

        Reports plainly rather than dropping the file silently: a user who
        sends a flyer and gets nothing back has no way to tell a broken
        pipeline from a flyer Friday judged eventless.

        Synchronous — the Telegram handler runs it in an executor.
        """
        telegram = getattr(self, "telegram_handler", None)
        if telegram is None:
            logger.error("on_media: telegram handler not bound — dropping media")
            return

        is_pdf = mime_type == "application/pdf"
        if is_pdf:
            try:
                images = [(png, "image/png") for png in _pdf_to_png_pages(file_bytes)]
            except Exception as e:
                logger.error(f"on_media: PDF rasterization failed: {e}")
                telegram.send("Couldn't read that PDF, sir — the file may be corrupted.")
                return
            if not images:
                telegram.send("That PDF appears to have no pages, sir.")
                return
        else:
            images = [(bytes(file_bytes), mime_type)]

        kind = "PDF" if is_pdf else "image"
        logger.info(
            f"on_media: extraction offline — {kind}, {len(images)} page(s)/frame(s), "
            f"caption={caption!r}"
        )
        telegram.send(
            "Event extraction from images and PDFs is offline, sir — I'm being "
            "rebuilt. Tell me the title, date, and time and I'll take it from there."
        )
