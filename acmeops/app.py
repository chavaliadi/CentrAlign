"""AcmeOps - a small but real internal company environment.

Two systems the agent has to work across, exactly as a real task would:

* ``/portal``   - the vendor invoice portal. Behind a login. Lists invoices,
                  each with a detail page and a downloadable PDF whose figures
                  are the authoritative ones.
* ``/payables`` - the internal payables system. A table of records plus forms
                  to create and correct them. This is the system of record the
                  verifier re-reads.

Nothing here is mocked at the transport layer: real HTTP, real HTML, real
SQLite, real PDFs, real session cookies. The agent drives it through a browser
the same way a person would.
"""

from __future__ import annotations

import secrets
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .chaos import CHAOS

BASE = Path(__file__).parent
PORTAL_USER = "ap.clerk@acme.example"
PORTAL_PASSWORD = "Vendor#2026"
ACTOR = "ai-task-worker"

app = FastAPI(title="AcmeOps", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))

# In-memory sessions. A dict is the right amount of machinery for a sandbox.
SESSIONS: dict[str, dict[str, Any]] = {}


def ensure_environment() -> None:
    """Make the environment self-contained.

    The invoice PDFs are generated rather than committed, and they are the only
    place the authoritative total appears. Relying on a separate `make_pdfs`
    step meant a fresh checkout served 404s for every PDF until something
    happened to run it - which is exactly what a test suite does not do. The
    app now builds whatever is missing when it starts, so cloning and running
    is enough.
    """
    db.ensure_ready()
    seed = BASE / "seed"
    if not seed.exists() or not any(seed.glob("*.pdf")):
        from .make_pdfs import main as generate_pdfs

        generate_pdfs()


ensure_environment()


def render(request: Request, name: str, ctx: dict[str, Any]) -> HTMLResponse:
    """Pick the layout variant for this render.

    Normally chaos decides, so the agent meets a shifting DOM. A ``?layout=a|b``
    query parameter pins it instead - that exists so the test suite can assert
    both variants stay readable, and so a demo can show the same task running
    against each one.
    """
    forced = request.query_params.get("layout", "").lower()
    variant = forced if forced in ("a", "b") else CHAOS.variant_for(request.url.path)
    ctx.setdefault("variant", variant)
    return templates.TemplateResponse(request, name, ctx)


# --- chaos middleware --------------------------------------------------------


@app.middleware("http")
async def chaos_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith(("/static", "/healthz", "/admin")):
        return await call_next(request)

    CHAOS.maybe_sleep()

    if request.method == "GET" and CHAOS.should_fail_read(path):
        return HTMLResponse(
            status_code=503,
            content=(
                "<html><body><h1>503 - Service Temporarily Unavailable</h1>"
                "<p id='err'>AcmeOps is briefly unavailable. Please retry.</p>"
                "</body></html>"
            ),
        )

    return await call_next(request)


# --- auth --------------------------------------------------------------------


def current_session(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get("acme_session")
    if not token:
        return None
    return SESSIONS.get(token)


def require_portal(request: Request) -> dict[str, Any] | RedirectResponse:
    """Portal pages need a session. Chaos may revoke it mid-task, which the
    agent has to notice and recover from by logging back in."""
    sess = current_session(request)
    if sess is None:
        return RedirectResponse(url="/portal/login?reason=required", status_code=303)
    if CHAOS.should_expire_session():
        SESSIONS.pop(request.cookies.get("acme_session", ""), None)
        return RedirectResponse(url="/portal/login?reason=expired", status_code=303)
    return sess


# --- landing -----------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return render(request, "home.html", {"title": "AcmeOps"})


@app.get("/healthz")
def healthz():
    return {"ok": True, "chaos": CHAOS.snapshot()}


@app.post("/admin/reset")
def admin_reset(profile: str = "clean"):
    """Used by the run harness to rebuild the world between tasks.

    ``clean`` seeds vendors and invoices but no payables, so a data-entry task
    is judged only on what the agent itself wrote. ``wrong_due_date`` also
    seeds the mis-keyed payable the correction task has to find.
    """
    db.reset(seed=True, include_payables=(profile == "wrong_due_date"))
    SESSIONS.clear()
    CHAOS._authed_requests = 0  # noqa: SLF001 - sandbox harness hook
    CHAOS._expiries_fired = 0  # noqa: SLF001
    return {"reset": True}


# --- vendor portal -----------------------------------------------------------


@app.get("/portal/login", response_class=HTMLResponse)
def portal_login_form(request: Request, reason: str = ""):
    messages = {
        "required": "Please sign in to view the vendor portal.",
        "expired": "Your session expired. Please sign in again.",
        "bad": "Incorrect email or password.",
    }
    return render(
        request,
        "login.html",
        {"title": "Sign in - AcmeOps Vendor Portal", "message": messages.get(reason, "")},
    )


@app.post("/portal/login")
def portal_login(request: Request, email: str = Form(...), password: str = Form(...)):
    if email.strip() != PORTAL_USER or password != PORTAL_PASSWORD:
        return RedirectResponse(url="/portal/login?reason=bad", status_code=303)
    token = secrets.token_urlsafe(24)
    SESSIONS[token] = {"email": email.strip(), "id": str(uuid.uuid4())}
    response = RedirectResponse(url="/portal", status_code=303)
    response.set_cookie("acme_session", token, httponly=True, samesite="lax")
    return response


@app.get("/portal", response_class=HTMLResponse)
def portal_index(request: Request, vendor: str = "", status: str = ""):
    sess = require_portal(request)
    if isinstance(sess, RedirectResponse):
        return sess
    db.ensure_ready()
    invoices = db.list_invoices(vendor=vendor or None, status=status or None)
    vendors = sorted({i["vendor"] for i in db.list_invoices()})
    return render(
        request,
        "portal_index.html",
        {
            "title": "Vendor Invoices - AcmeOps",
            "invoices": invoices,
            "vendors": vendors,
            "vendor_filter": vendor,
            "status_filter": status,
            "user": sess["email"],
        },
    )


@app.get("/portal/invoice/{number}", response_class=HTMLResponse)
def portal_invoice(request: Request, number: str):
    sess = require_portal(request)
    if isinstance(sess, RedirectResponse):
        return sess
    invoice = db.get_invoice(number)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return render(
        request,
        "portal_invoice.html",
        {"title": f"{number} - AcmeOps", "invoice": invoice, "user": sess["email"]},
    )


@app.get("/portal/invoice/{number}/pdf")
def portal_invoice_pdf(request: Request, number: str):
    sess = require_portal(request)
    if isinstance(sess, RedirectResponse):
        return sess
    invoice = db.get_invoice(number)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    path = BASE / "seed" / f"{number}.pdf"
    if not path.exists():
        # Regenerate rather than 404: a missing generated artefact is our
        # problem to fix, not an error for the agent to work around.
        ensure_environment()
    if not path.exists():
        raise HTTPException(status_code=404, detail="PDF could not be generated")
    return FileResponse(path, media_type="application/pdf", filename=f"{number}.pdf")


# --- internal payables system ------------------------------------------------


@app.get("/payables", response_class=HTMLResponse)
def payables_index(request: Request):
    db.ensure_ready()
    return render(
        request,
        "payables_index.html",
        {"title": "Payables - AcmeOps Internal", "payables": db.list_payables()},
    )


@app.get("/payables/new", response_class=HTMLResponse)
def payables_new_form(request: Request, error: str = ""):
    db.ensure_ready()
    return render(
        request,
        "payables_form.html",
        {
            "title": "New Payable - AcmeOps Internal",
            "vendors": db.SEED_VENDORS,
            "error": error,
            "payable": None,
            "action": "/payables/new",
        },
    )


@app.post("/payables/new")
def payables_create(
    request: Request,
    vendor: str = Form(...),
    invoice_number: str = Form(...),
    amount: str = Form(...),
    due_date: str = Form(...),
):
    try:
        amount_value = float(str(amount).replace(",", "").replace("$", "").strip())
    except ValueError:
        return RedirectResponse(url="/payables/new?error=amount", status_code=303)
    if not invoice_number.strip():
        return RedirectResponse(url="/payables/new?error=invoice", status_code=303)
    new_id = db.insert_payable(
        vendor=vendor.strip(),
        invoice_number=invoice_number.strip(),
        amount=amount_value,
        due_date=due_date.strip(),
        actor=ACTOR,
    )
    return RedirectResponse(url=f"/payables?created={new_id}", status_code=303)


@app.get("/payables/{payable_id}/edit", response_class=HTMLResponse)
def payables_edit_form(request: Request, payable_id: int, error: str = ""):
    payable = db.get_payable(payable_id)
    if not payable:
        raise HTTPException(status_code=404, detail="Payable not found")
    return render(
        request,
        "payables_form.html",
        {
            "title": f"Edit payable #{payable_id} - AcmeOps Internal",
            "vendors": db.SEED_VENDORS,
            "error": error,
            "payable": payable,
            "action": f"/payables/{payable_id}/edit",
        },
    )


@app.post("/payables/{payable_id}/edit")
def payables_update(
    request: Request,
    payable_id: int,
    vendor: str = Form(...),
    invoice_number: str = Form(...),
    amount: str = Form(...),
    due_date: str = Form(...),
):
    try:
        amount_value = float(str(amount).replace(",", "").replace("$", "").strip())
    except ValueError:
        return RedirectResponse(url=f"/payables/{payable_id}/edit?error=amount", status_code=303)
    ok = db.update_payable(payable_id, amount_value, due_date.strip(), actor=ACTOR)
    if not ok:
        raise HTTPException(status_code=404, detail="Payable not found")
    return RedirectResponse(url=f"/payables?updated={payable_id}", status_code=303)


# --- read-only JSON, used only by the verifier -------------------------------


@app.get("/api/payables")
def api_payables():
    """The verifier may read state through this endpoint instead of the UI the
    agent used, so a UI-level illusion of success cannot fool verification."""
    return JSONResponse({"payables": db.list_payables(), "audit": db.audit_log()})


@app.get("/api/invoices")
def api_invoices():
    return JSONResponse({"invoices": db.list_invoices()})
