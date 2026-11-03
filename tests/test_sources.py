import pymupdf

from kaznabot.sources import pdf_text_or_image


def _pdf(text: str | None) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    if text:
        page.insert_text((50, 72), text)
    else:
        page.draw_rect(pymupdf.Rect(50, 50, 200, 200), fill=(0, 0, 0))
    return bytes(doc.tobytes())


def test_text_pdf_returns_text() -> None:
    text, image = pdf_text_or_image(_pdf("KASSOVYI CHEK Moloko 89.99 Hleb 54.00 ITOGO 143.99"))
    assert text is not None
    assert "Moloko" in text
    assert image == b""


def test_scanned_pdf_returns_png() -> None:
    text, image = pdf_text_or_image(_pdf(None))
    assert text is None
    assert image.startswith(b"\x89PNG")
