"""Generate the seed invoice PDFs.

The PDF is the authoritative document in this environment: it is the only place
the *total payable* appears. Run once via ``python -m acmeops.make_pdfs`` (the
run harness does this automatically).
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from . import db

OUT = Path(__file__).parent / "seed"

VENDOR_ADDRESS = {
    "Northwind Traders": ["Northwind Traders Pvt. Ltd.", "14 Harbour Road, Singapore 099253"],
    "Globex Corporation": ["Globex Corporation", "900 Industrial Parkway, Springfield, IL 62701"],
    "Initech Supplies": ["Initech Supplies LLC", "2200 Cedar Avenue, Austin, TX 78701"],
}


def draw_invoice(inv: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{inv['number']}.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    y = height - 30 * mm

    c.setFont("Helvetica-Bold", 20)
    c.drawString(20 * mm, y, "INVOICE")
    c.setFont("Helvetica", 10)
    c.drawRightString(width - 20 * mm, y, f"Invoice No: {inv['number']}")
    y -= 7 * mm
    c.drawRightString(width - 20 * mm, y, f"Issued: {inv['issued_date']}")

    y -= 16 * mm
    c.setFont("Helvetica-Bold", 11)
    c.drawString(20 * mm, y, "From")
    c.setFont("Helvetica", 10)
    for line in VENDOR_ADDRESS.get(inv["vendor"], [inv["vendor"]]):
        y -= 5.5 * mm
        c.drawString(20 * mm, y, line)

    y -= 12 * mm
    c.setFont("Helvetica-Bold", 11)
    c.drawString(20 * mm, y, "Bill to")
    c.setFont("Helvetica", 10)
    for line in ["Acme Corporation", "Accounts Payable", "1 Acme Plaza, Wilmington, DE 19801"]:
        y -= 5.5 * mm
        c.drawString(20 * mm, y, line)

    # Payment terms block - the due date appears here as well as in the portal.
    y -= 14 * mm
    c.setFont("Helvetica-Bold", 11)
    c.drawString(20 * mm, y, "Payment terms")
    c.setFont("Helvetica", 10)
    y -= 5.5 * mm
    c.drawString(20 * mm, y, f"Payment due date: {inv['due_date']}")
    y -= 5.5 * mm
    c.drawString(20 * mm, y, "Terms: Net 30 from date of issue")

    # Totals table.
    net = float(inv["net_amount"])
    total = float(inv["amount"])
    tax = round(total - net, 2)

    y -= 18 * mm
    c.setFont("Helvetica-Bold", 11)
    c.drawString(20 * mm, y, "Summary")
    c.setLineWidth(0.6)
    y -= 4 * mm
    c.line(20 * mm, y, width - 20 * mm, y)

    rows = [
        ("Subtotal (net of tax)", f"{inv['currency']} {net:,.2f}"),
        ("Tax", f"{inv['currency']} {tax:,.2f}"),
    ]
    c.setFont("Helvetica", 10)
    for label, value in rows:
        y -= 7 * mm
        c.drawString(22 * mm, y, label)
        c.drawRightString(width - 22 * mm, y, value)

    y -= 4 * mm
    c.line(20 * mm, y, width - 20 * mm, y)
    y -= 8 * mm
    c.setFont("Helvetica-Bold", 12)
    c.drawString(22 * mm, y, "TOTAL PAYABLE")
    c.drawRightString(width - 22 * mm, y, f"{inv['currency']} {total:,.2f}")

    y -= 18 * mm
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(20 * mm, y, "This document is the authoritative record of the amount payable.")
    y -= 5 * mm
    c.drawString(20 * mm, y, f"Status at time of issue: {inv['status']}")

    c.showPage()
    c.save()


def main() -> None:
    db.ensure_ready()
    invoices = db.list_invoices()
    for inv in invoices:
        draw_invoice(inv)
    print(f"wrote {len(invoices)} invoice PDFs to {OUT}")


if __name__ == "__main__":
    main()
