"""One quality-first MP4 preference shared by extraction, download and estimates."""

# Preserve language/site quality, dimensions, frame rate and dynamic range before
# preferring codecs we can copy into the final H.264/AAC MP4. Do not force past
# an extractor's priority flags. Non-compatible sources remain downloadable.
MP4_FORMAT_SORT = ("lang", "quality", "res", "fps", "hdr:12",
                   "vcodec:h264", "channels", "acodec:aac")


def format_selector(height: int | None) -> str:
    limit = f"[height<={height}]" if height else ""
    # Include combined sources in the first choice too: an equally good H.264
    # MP4 must not lose to video-only AV1 merely because it already has audio.
    return f"bv*{limit}+ba/b{limit}/bv{limit}"
