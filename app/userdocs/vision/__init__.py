"""Image captions and scanned-page OCR for User Document Search.

Only the profile's **Specialized Vision Model** (Settings → LLM Providers →
Image Understanding) is ever used — never the main model, even though the
model-group resolver would silently fall back to it (see :mod:`.resolver`).
Captioning also needs the user's recorded consent for that exact provider and
model, because it sends their photos and scans to a third party, and it is
bounded by a per-profile daily cap.
"""
