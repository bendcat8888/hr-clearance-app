import uuid
import os
import secrets
import asyncio
import hashlib
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import parse_qs
from typing import Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.middleware.sessions import SessionMiddleware

from database import AsyncSessionLocal, get_session, init_engine
from models import Base, Clearance, Department, EmailSendLog, FormApprover, HrAccessUser, SignInLog


def format_datetime_ph(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    return dt.strftime("%Y-%m-%d %I:%M %p").lower()


def utc_now() -> datetime:
    return datetime.now(UTC)


async def get_form_value(request: Request, field_name: str, *, required: bool = False, default: Optional[str] = None) -> Optional[str]:
    form = await get_form_data(request)
    values = form.get(field_name)
    value = values[-1] if values else default
    if value is None:
        if required:
            raise HTTPException(status_code=400, detail=f"Missing field: {field_name}")
        return None
    stripped = value.strip()
    if required and stripped == "":
        raise HTTPException(status_code=400, detail=f"Missing field: {field_name}")
    return stripped


async def get_form_data(request: Request) -> dict[str, list[str]]:
    cached = getattr(request.state, "_parsed_form_data", None)
    if cached is not None:
        return cached

    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    request.state._parsed_form_data = parsed
    return parsed


def get_client_ip(request: Request) -> Optional[str]:
    forwarded_for = (request.headers.get("x-forwarded-for") or "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else None


async def record_sign_in(
    session: AsyncSession,
    request: Request,
    *,
    provider: str,
    email: Optional[str] = None,
    domain: Optional[str] = None,
    username: Optional[str] = None,
) -> None:
    session.add(
        SignInLog(
            provider=provider,
            email=email,
            domain=domain,
            username=username,
            ip_address=get_client_ip(request),
            user_agent=((request.headers.get("user-agent") or "").strip() or None),
        )
    )
    await session.commit()


DEFAULT_APPROVER_DEPARTMENTS = [
    "IMMEDIATE SUPERIOR",
    "BU ADMIN",
    "HRAD",
    "FINANCE",
    "FLEET",
    "IT/MIS",
    "MARKETING",
    "SALES",
    "TRAINING",
    "WAREHOUSE",
    "BU7 HEAD (FOR: PSR - DSM)",
    "DEPARTMENT/BU HEAD",
]

SSO_BASE_URL = (os.getenv("SSO_BASE_URL") or "https://sso.innogen-pharma.com").rstrip("/")
APP_HOST = (os.getenv("APP_HOST") or "http://localhost:8518").strip().rstrip("/")
if not APP_HOST.startswith("http://") and not APP_HOST.startswith("https://"):
    APP_HOST = "https://" + APP_HOST
HR_USERNAME = "HR"
HR_DEFAULT_PASSWORD = "HR123"

SMTP_HOST = os.getenv("SMTP_HOST") or "smtp.gmail.com"
SMTP_PORT = int(os.getenv("SMTP_PORT") or "465")
SMTP_USE_SSL = (os.getenv("SMTP_USE_SSL") or "1").strip().lower() in {"1", "true", "yes"}
SMTP_USERNAME = os.getenv("SMTP_USERNAME") or ""
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD") or ""
SMTP_FROM = os.getenv("SMTP_FROM") or SMTP_USERNAME or "no-reply@innogen-pharma.com"
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME") or "HR Clearance"
EMAIL_NOTIFICATIONS_ENABLED = (os.getenv("EMAIL_NOTIFICATIONS_ENABLED") or "1").strip().lower() in {"1", "true", "yes"}


app = FastAPI(title="Dynamic HR Clearance System")
templates = Jinja2Templates(directory="templates")

app.mount("/static", StaticFiles(directory="assets"), name="static")

session_secret = os.getenv("HR_SESSION_SECRET") or secrets.token_urlsafe(32)
cookie_https_only = (os.getenv("COOKIE_HTTPS_ONLY") or "0").strip().lower() in {"1", "true", "yes"}
app.add_middleware(SessionMiddleware, secret_key=session_secret, same_site="lax", https_only=cookie_https_only)


def is_hr_logged_in(request: Request) -> bool:
    if request.session.get("sso_user"):
        return True
    if request.session.get("hr_logged_in") is True:
        return True
    return False


def is_hr_access_logged_in(request: Request) -> bool:
    return request.session.get("hr_logged_in") is True


def hr_login_redirect(next_path: str) -> RedirectResponse:
    return RedirectResponse(url=f"/login?next={next_path}", status_code=303)


def hr_access_login_redirect(next_path: str) -> RedirectResponse:
    return RedirectResponse(url=f"/hr/login?next={next_path}", status_code=303)


def hr_access_entry_redirect() -> RedirectResponse:
    return hr_access_login_redirect("/history")


def activate_hr_access_session(request: Request) -> None:
    request.session["hr_logged_in"] = True


def clear_hr_access_session(request: Request) -> None:
    request.session.pop("hr_logged_in", None)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, digest = stored_hash.split("$", 1)
    except ValueError:
        return False
    computed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200000).hex()
    return secrets.compare_digest(computed, digest)


async def get_hr_access_user(session: AsyncSession) -> HrAccessUser:
    user = (await session.execute(select(HrAccessUser).where(HrAccessUser.username == HR_USERNAME))).scalars().first()
    if not user:
        user = HrAccessUser(username=HR_USERNAME, password_hash=hash_password(HR_DEFAULT_PASSWORD))
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


def _send_email_sync(*, to_email: str, subject: str, html_body: str) -> None:
    if not EMAIL_NOTIFICATIONS_ENABLED:
        return
    if not SMTP_USERNAME or not SMTP_PASSWORD:
        raise RuntimeError("SMTP is not configured")
    msg = MIMEMultipart()
    msg["From"] = f'"{SMTP_FROM_NAME}" <{SMTP_FROM}>'
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))
    if SMTP_USE_SSL:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg, to_addrs=[to_email])
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg, to_addrs=[to_email])


def _email_html_template(*, title: str, heading: str, summary_lines: list[str], button_url: str, button_text: str) -> str:
    summary_html = "".join(f"<p style='margin:0 0 6px 0;font-size:14px;color:#334155;'><strong>{line}</strong></p>" for line in summary_lines if line)
    logo_url = f"{APP_HOST}/static/img/InnoGen.png"
    return f"""
    <html>
      <body style="background-color:#f1f5f9;padding:24px;font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#0f172a;">
        <div style="max-width:640px;margin:0 auto;background-color:#ffffff;border-radius:12px;padding:24px;border:1px solid #e2e8f0;">
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;">
            <img src="{logo_url}" alt="InnoGen" style="height:36px;width:auto;display:block;" />
            <div style="font-size:12px;color:#64748b;">{title}</div>
          </div>
          <h2 style="margin:0 0 12px 0;font-size:18px;color:#0f172a;">{heading}</h2>
          {summary_html}
          <p style="margin:14px 0 18px 0;font-size:14px;color:#1f2937;">
            Please open the clearance form using the button below.
          </p>
          <p style="text-align:center;margin:0 0 18px 0;">
            <a href="{button_url}" style="display:inline-block;padding:12px 28px;border-radius:999px;background-color:#2563eb;color:#ffffff;text-decoration:none;font-weight:600;font-size:14px;">
              {button_text}
            </a>
          </p>
          <p style="margin:0 0 6px 0;font-size:12px;color:#64748b;">
            If the button does not work, copy and paste this link into your browser:
          </p>
          <p style="margin:0;font-size:11px;color:#64748b;word-break:break-all;">{button_url}</p>
          <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0;" />
          <div style="font-size:11px;color:#94a3b8;line-height:1.5;">
            <div>Note: This is an automated notification. Please do not reply to this email.</div>
            <div>InnoGen's IT Department © 2026</div>
            <div>This is an automated email notification.</div>
          </div>
        </div>
      </body>
    </html>
    """


async def _log_email_send(
    *,
    clearance_id: int,
    department_id: int,
    to_email: str,
    subject: str,
    status: str,
    error: Optional[str],
) -> None:
    init_engine()
    if AsyncSessionLocal is None:
        return
    async with AsyncSessionLocal() as s:
        s.add(
            EmailSendLog(
                clearance_id=clearance_id,
                department_id=department_id,
                to_email=to_email,
                subject=subject,
                status=status,
                error=(error[:1800] if error else None),
            )
        )
        await s.commit()


async def _send_and_log(
    *,
    clearance_id: int,
    department_id: int,
    to_email: str,
    subject: str,
    html_body: str,
) -> None:
    try:
        await asyncio.to_thread(_send_email_sync, to_email=to_email, subject=subject, html_body=html_body)
        await _log_email_send(
            clearance_id=clearance_id,
            department_id=department_id,
            to_email=to_email,
            subject=subject,
            status="sent",
            error=None,
        )
    except Exception as exc:
        await _log_email_send(
            clearance_id=clearance_id,
            department_id=department_id,
            to_email=to_email,
            subject=subject,
            status="failed",
            error=str(exc),
        )


def clear_app_session(request: Request) -> None:
    request.session.clear()


async def ensure_schema(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE IF EXISTS departments ADD COLUMN IF NOT EXISTS email VARCHAR(200)"))
        await conn.execute(text("ALTER TABLE IF EXISTS clearances ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ"))
        await conn.execute(
            text("ALTER TABLE IF EXISTS clearances ADD COLUMN IF NOT EXISTS first_printed_at TIMESTAMPTZ")
        )
        await conn.execute(
            text("ALTER TABLE IF EXISTS clearances ADD COLUMN IF NOT EXISTS last_printed_at TIMESTAMPTZ")
        )
        await conn.execute(text("ALTER TABLE IF EXISTS clearances ADD COLUMN IF NOT EXISTS print_count INTEGER DEFAULT 0"))
        await conn.execute(text("ALTER TABLE IF EXISTS form_approvers ADD COLUMN IF NOT EXISTS disapproval_reason TEXT"))
        await conn.execute(text("ALTER TABLE IF EXISTS form_approvers ADD COLUMN IF NOT EXISTS disapproved_at TIMESTAMPTZ"))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return RedirectResponse(url="/static/img/favicon.png", status_code=307)


@app.on_event("startup")
async def startup() -> None:
    engine = init_engine()
    templates.env.globals["format_ph"] = format_datetime_ph
    templates.env.globals["sso_base_url"] = SSO_BASE_URL
    templates.env.globals["app_host"] = APP_HOST
    for _ in range(30):
        try:
            await ensure_schema(engine)
            break
        except Exception:
            await asyncio.sleep(1.5)
    async with engine.begin() as conn:
        existing = await conn.execute(select(func.count(Department.id)))
        count = int(existing.scalar_one())
        if count == 0:
            rows = [
                {"name": name, "sort_order": i}
                for i, name in enumerate(DEFAULT_APPROVER_DEPARTMENTS, start=1)
            ]
            await conn.execute(Department.__table__.insert(), rows)
        hr_user_count = await conn.execute(select(func.count(HrAccessUser.id)))
        if int(hr_user_count.scalar_one()) == 0:
            await conn.execute(
                HrAccessUser.__table__.insert(),
                [{"username": HR_USERNAME, "password_hash": hash_password(HR_DEFAULT_PASSWORD)}],
            )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, session: AsyncSession = Depends(get_session)):
    if not is_hr_logged_in(request):
        return hr_login_redirect("/")
    departments = (await session.execute(select(Department).order_by(Department.sort_order, Department.name))).scalars().all()
    return templates.TemplateResponse(
        request,
        "index.html",
        {"departments": departments, "today": utc_now().strftime("%d-%b-%y")},
    )


@app.get("/login", response_class=HTMLResponse)
async def sso_login_page(request: Request):
    if is_hr_logged_in(request):
        next_path = request.query_params.get("next") or "/"
        return RedirectResponse(url=next_path, status_code=303)
    next_path = request.query_params.get("next") or "/"
    return templates.TemplateResponse(request, "sso_login.html", {"next": next_path})


@app.get("/sso-callback", response_class=HTMLResponse)
@app.get("/sso_callback.html", response_class=HTMLResponse)
async def sso_callback_page(request: Request):
    return templates.TemplateResponse(request, "sso_callback.html", {"next": "/"})


@app.post("/auth/sso/complete")
async def sso_complete(request: Request, user: dict = Body(...), session: AsyncSession = Depends(get_session)):
    email = (user.get("email") or "").strip()
    domain = (user.get("domain") or "").strip()
    provider = (user.get("provider") or "sso").strip().lower()
    if not email or not domain:
        raise HTTPException(status_code=400, detail="Invalid user payload")
    request.session["sso_user"] = {"email": email, "domain": domain}
    await record_sign_in(session, request, provider=provider, email=email, domain=domain)
    return {"ok": True, "user": request.session["sso_user"]}


@app.get("/logout")
async def logout(request: Request):
    clear_app_session(request)
    return RedirectResponse(url="/login", status_code=303)


@app.get("/protected", response_class=HTMLResponse)
async def protected_page(request: Request):
    if not is_hr_logged_in(request):
        return hr_login_redirect("/protected")
    return templates.TemplateResponse(request, "protected.html", {"user": request.session.get("sso_user")})


@app.get("/hr-access")
async def hr_access_entry(request: Request):
    clear_hr_access_session(request)
    return RedirectResponse(url="/hr/login?next=/history", status_code=303)


@app.get("/hr/login", response_class=HTMLResponse)
async def hr_login_page(request: Request):
    if is_hr_access_logged_in(request):
        return RedirectResponse(url="/history", status_code=303)
    next_path = request.query_params.get("next") or "/history"
    return templates.TemplateResponse(request, "login.html", {"next": next_path, "error": None, "message": None})


@app.post("/hr/login", response_class=HTMLResponse)
async def hr_login_submit(request: Request, session: AsyncSession = Depends(get_session)):
    username = await get_form_value(request, "username", required=True)
    password = await get_form_value(request, "password", required=True)
    next_path = await get_form_value(request, "next", default="/history")
    user = await get_hr_access_user(session)
    if username == user.username and verify_password(password, user.password_hash):
        activate_hr_access_session(request)
        await record_sign_in(session, request, provider="local_hr", username=username)
        return RedirectResponse(url=next_path or "/history", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"next": next_path, "error": "Invalid credentials", "message": None}
    )


@app.get("/hr/logout")
async def hr_logout(request: Request):
    clear_hr_access_session(request)
    return RedirectResponse(url="/", status_code=303)


@app.post("/hr/forgot-password", response_class=HTMLResponse)
async def hr_forgot_password(request: Request, session: AsyncSession = Depends(get_session)):
    next_path = await get_form_value(request, "next", default="/history")
    user = await get_hr_access_user(session)
    user.password_hash = hash_password(HR_DEFAULT_PASSWORD)
    user.updated_at = utc_now()
    await session.commit()
    clear_hr_access_session(request)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "next": next_path,
            "error": None,
            "message": "Password reset to default. Use HR / HR123 to sign in.",
        },
    )


@app.post("/hr/change-password")
async def hr_change_password(request: Request, session: AsyncSession = Depends(get_session)):
    if not is_hr_access_logged_in(request):
        return hr_access_login_redirect("/history")

    current_password = await get_form_value(request, "current_password", required=True)
    new_password = await get_form_value(request, "new_password", required=True)
    confirm_password = await get_form_value(request, "confirm_password", required=True)

    if new_password != confirm_password:
        raise HTTPException(status_code=400, detail="New password and confirmation do not match")
    if len(new_password) < 4:
        raise HTTPException(status_code=400, detail="New password must be at least 4 characters")

    user = await get_hr_access_user(session)
    if not verify_password(current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    user.password_hash = hash_password(new_password)
    user.updated_at = utc_now()
    await session.commit()
    return {"ok": True}



@app.post("/create")
async def create_clearance(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await get_form_data(request)
    employee_id = await get_form_value(request, "employee_id", required=True)
    employee_name = await get_form_value(request, "employee_name", required=True)
    position = await get_form_value(request, "position", required=True)
    division_department = await get_form_value(request, "division_department", required=True)
    date_hired = await get_form_value(request, "date_hired")
    date_separated = await get_form_value(request, "date_separated")
    date_prepared = await get_form_value(request, "date_prepared")
    reason_for_separation = await get_form_value(request, "reason_for_separation")
    payroll_type = await get_form_value(request, "payroll_type")
    payroll_class = await get_form_value(request, "payroll_class")
    remarks = await get_form_value(request, "remarks")
    selected_department_ids = [int(v) for v in form.get("department_ids", [])]
    if not selected_department_ids:
        raise HTTPException(status_code=400, detail="Select at least one department")

    clearance = Clearance(
        employee_id=employee_id,
        employee_name=employee_name,
        position=position,
        division_department=division_department,
        date_hired=date_hired,
        date_separated=date_separated,
        date_prepared=date_prepared,
        reason_for_separation=reason_for_separation,
        payroll_type=payroll_type,
        payroll_class=payroll_class,
        remarks=remarks,
    )
    session.add(clearance)
    await session.flush()

    departments = (
        (await session.execute(select(Department).where(Department.id.in_(selected_department_ids))))
        .scalars()
        .all()
    )
    dept_by_id = {d.id: d for d in departments}
    created_approvers: list[tuple[Department, FormApprover]] = []

    for dept_id in selected_department_ids:
        dept = dept_by_id.get(dept_id)
        if not dept:
            continue
        approver = FormApprover(clearance_id=clearance.id, department_id=dept.id)
        session.add(approver)
        created_approvers.append((dept, approver))

    await session.commit()
    if EMAIL_NOTIFICATIONS_ENABLED and created_approvers:
        for dept, approver in created_approvers:
            to_email = (dept.email or "").strip()
            if not to_email:
                continue
            sign_url = f"{APP_HOST}/sign/{approver.sign_token}"
            subject = f"Clearance sign-off: {clearance.employee_name} ({clearance.employee_id})"
            html_body = _email_html_template(
                title="Accountability and Clearance Release Form",
                heading=f"Signature request for {dept.name}",
                summary_lines=[
                    f"Employee: {clearance.employee_name} ({clearance.employee_id})",
                    f"Position: {clearance.position}",
                    f"Division/Department: {clearance.division_department}",
                    f"Reason for separation: {clearance.reason_for_separation or '-'}",
                ],
                button_url=sign_url,
                button_text="Open clearance form",
            )
            asyncio.create_task(
                _send_and_log(
                    clearance_id=clearance.id,
                    department_id=dept.id,
                    to_email=to_email,
                    subject=subject,
                    html_body=html_body,
                )
            )
    return RedirectResponse(url=f"/status/{clearance.id}", status_code=303)


@app.get("/status", response_class=HTMLResponse)
async def status_all(request: Request):
    return templates.TemplateResponse(request, "status.html", {"clearance_id": None})


@app.get("/status/{clearance_id}", response_class=HTMLResponse)
async def status_one(request: Request, clearance_id: int):
    return templates.TemplateResponse(request, "status.html", {"clearance_id": clearance_id})


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request, session: AsyncSession = Depends(get_session)):
    if not is_hr_access_logged_in(request):
        return hr_access_entry_redirect()

    all_clearances = (
        await session.execute(
            select(Clearance)
            .order_by(Clearance.id.desc())
        )
    ).scalars().all()

    pending = [c for c in all_clearances if c.completed_at is None]
    newly_completed = [c for c in all_clearances if c.completed_at is not None and c.first_printed_at is None]
    printed = [c for c in all_clearances if c.completed_at is not None and c.first_printed_at is not None]
    sign_in_logs = (
        await session.execute(select(SignInLog).order_by(SignInLog.signed_in_at.desc()).limit(12))
    ).scalars().all()
    email_logs = (
        await session.execute(
            select(EmailSendLog)
            .options(selectinload(EmailSendLog.department))
            .order_by(EmailSendLog.sent_at.desc())
            .limit(80)
        )
    ).scalars().all()

    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "pending": pending,
            "newly_completed": newly_completed,
            "printed": printed,
            "sign_in_logs": sign_in_logs,
            "email_logs": email_logs,
        },
    )


def normalize_department_email(email: Optional[str]) -> Optional[str]:
    if email is None:
        return None
    value = email.strip().lower()
    if value == "":
        return None
    if "@" not in value or value.startswith("@") or value.endswith("@"):
        raise HTTPException(status_code=400, detail="Invalid department email")
    return value


@app.post("/departments")
async def create_department(request: Request, payload: dict = Body(...), session: AsyncSession = Depends(get_session)):
    if not is_hr_logged_in(request):
        raise HTTPException(status_code=401, detail="Authentication required")

    name = (payload.get("name") or "").strip()
    email = normalize_department_email(payload.get("email"))
    if not name:
        raise HTTPException(status_code=400, detail="Department name is required")

    existing = (
        await session.execute(select(Department).where(func.lower(Department.name) == name.lower()))
    ).scalars().first()
    if existing:
        raise HTTPException(status_code=400, detail="Department already exists")

    max_sort = (await session.execute(select(func.max(Department.sort_order)))).scalar_one()
    department = Department(name=name, email=email, sort_order=int(max_sort or 0) + 1)
    session.add(department)
    await session.commit()
    await session.refresh(department)
    return {
        "ok": True,
        "department": {
            "id": department.id,
            "name": department.name,
            "email": department.email,
            "sort_order": department.sort_order,
        },
    }


@app.post("/departments/{department_id}")
async def update_department(
    request: Request,
    department_id: int,
    payload: dict = Body(...),
    session: AsyncSession = Depends(get_session),
):
    if not is_hr_logged_in(request):
        raise HTTPException(status_code=401, detail="Authentication required")

    department = await session.get(Department, department_id)
    if not department:
        raise HTTPException(status_code=404, detail="Department not found")

    name = (payload.get("name") or "").strip()
    email = normalize_department_email(payload.get("email"))
    if not name:
        raise HTTPException(status_code=400, detail="Department name is required")

    existing = (
        await session.execute(
            select(Department)
            .where(func.lower(Department.name) == name.lower())
            .where(Department.id != department_id)
        )
    ).scalars().first()
    if existing:
        raise HTTPException(status_code=400, detail="Department already exists")

    department.name = name
    department.email = email
    await session.commit()
    return {
        "ok": True,
        "department": {
            "id": department.id,
            "name": department.name,
            "email": department.email,
            "sort_order": department.sort_order,
        },
    }


@app.post("/departments/{department_id}/delete")
async def delete_department(request: Request, department_id: int, session: AsyncSession = Depends(get_session)):
    if not is_hr_logged_in(request):
        raise HTTPException(status_code=401, detail="Authentication required")

    department = await session.get(Department, department_id)
    if not department:
        raise HTTPException(status_code=404, detail="Department not found")

    in_use = (
        await session.execute(
            select(func.count(FormApprover.id)).where(FormApprover.department_id == department_id)
        )
    ).scalar_one()
    if int(in_use) > 0:
        raise HTTPException(status_code=400, detail="Department is already used in clearance records")

    await session.delete(department)
    await session.commit()
    return {"ok": True, "department_id": department_id}


@app.post("/delete/{clearance_id}")
async def delete_clearance(request: Request, clearance_id: int, session: AsyncSession = Depends(get_session)):
    if not is_hr_access_logged_in(request):
        return hr_access_entry_redirect()

    clearance = await session.get(Clearance, clearance_id)
    if not clearance:
        raise HTTPException(status_code=404, detail="Clearance not found")

    await session.delete(clearance)
    await session.commit()

    return RedirectResponse(url="/history", status_code=303)


@app.get("/edit/{clearance_id}", response_class=HTMLResponse)
async def edit_clearance_page(request: Request, clearance_id: int, session: AsyncSession = Depends(get_session)):
    if not is_hr_access_logged_in(request):
        return hr_access_entry_redirect()

    clearance = (
        (
            await session.execute(
                select(Clearance)
                .where(Clearance.id == clearance_id)
                .options(selectinload(Clearance.approvers))
            )
        )
        .scalars()
        .first()
    )
    if not clearance:
        raise HTTPException(status_code=404, detail="Clearance not found")
    if clearance.completed_at is not None:
        raise HTTPException(status_code=400, detail="Cannot edit completed clearances")

    departments = (await session.execute(select(Department).order_by(Department.sort_order, Department.name))).scalars().all()
    selected_dept_ids = [a.department_id for a in clearance.approvers]

    return templates.TemplateResponse(
        request,
        "edit.html",
        {
            "clearance": clearance,
            "departments": departments,
            "selected_dept_ids": selected_dept_ids,
        },
    )


@app.post("/edit/{clearance_id}")
async def edit_clearance_submit(
    request: Request,
    clearance_id: int,
    session: AsyncSession = Depends(get_session),
):
    if not is_hr_access_logged_in(request):
        return hr_access_entry_redirect()

    clearance = (
        (
            await session.execute(
                select(Clearance)
                .where(Clearance.id == clearance_id)
                .options(selectinload(Clearance.approvers))
            )
        )
        .scalars()
        .first()
    )
    if not clearance:
        raise HTTPException(status_code=404, detail="Clearance not found")
    if clearance.completed_at is not None:
        raise HTTPException(status_code=400, detail="Cannot edit completed clearances")

    form = await get_form_data(request)
    employee_id = await get_form_value(request, "employee_id", required=True)
    employee_name = await get_form_value(request, "employee_name", required=True)
    position = await get_form_value(request, "position", required=True)
    division_department = await get_form_value(request, "division_department", required=True)
    date_hired = await get_form_value(request, "date_hired")
    date_separated = await get_form_value(request, "date_separated")
    date_prepared = await get_form_value(request, "date_prepared")
    reason_for_separation = await get_form_value(request, "reason_for_separation")
    payroll_type = await get_form_value(request, "payroll_type")
    payroll_class = await get_form_value(request, "payroll_class")
    remarks = await get_form_value(request, "remarks")
    new_dept_ids = [int(v) for v in form.get("department_ids", [])]
    if not new_dept_ids:
        raise HTTPException(status_code=400, detail="Select at least one department")

    clearance.employee_id = employee_id
    clearance.employee_name = employee_name
    clearance.position = position
    clearance.division_department = division_department
    clearance.date_hired = date_hired
    clearance.date_separated = date_separated
    clearance.date_prepared = date_prepared
    clearance.reason_for_separation = reason_for_separation
    clearance.payroll_type = payroll_type
    clearance.payroll_class = payroll_class
    clearance.remarks = remarks

    # Sync departments
    existing_dept_ids = [a.department_id for a in clearance.approvers]
    
    # Remove unchecked ones
    for a in list(clearance.approvers):
        if a.department_id not in new_dept_ids:
            await session.delete(a)
            
    # Add new checked ones
    for dept_id in new_dept_ids:
        if dept_id not in existing_dept_ids:
            session.add(FormApprover(clearance_id=clearance.id, department_id=dept_id))

    await session.commit()
    return RedirectResponse(url="/history", status_code=303)


@app.get("/print/{clearance_id}", response_class=HTMLResponse)
async def print_clearance(request: Request, clearance_id: int, session: AsyncSession = Depends(get_session)):
    if not is_hr_access_logged_in(request):
        return hr_access_entry_redirect()

    clearance = (
        (
            await session.execute(
                select(Clearance)
                .where(Clearance.id == clearance_id)
                .options(selectinload(Clearance.approvers).selectinload(FormApprover.department))
            )
        )
        .scalars()
        .first()
    )
    if not clearance:
        raise HTTPException(status_code=404, detail="Clearance not found")
    if clearance.completed_at is None:
        raise HTTPException(status_code=400, detail="Clearance is not completed yet")

    now = utc_now()
    if clearance.first_printed_at is None:
        clearance.first_printed_at = now
    clearance.last_printed_at = now
    clearance.print_count = int(clearance.print_count or 0) + 1
    await session.commit()
    await session.refresh(clearance)

    approvers_sorted = sorted(
        clearance.approvers,
        key=lambda x: x.department.sort_order if x.department else 0,
    )
    return templates.TemplateResponse(
        request,
        "print.html",
        {"clearance": clearance, "approvers": approvers_sorted},
    )


@app.get("/sign/{token}", response_class=HTMLResponse)
async def sign_page(request: Request, token: uuid.UUID, session: AsyncSession = Depends(get_session)):
    approver = (
        (
            await session.execute(
                select(FormApprover)
                .where(FormApprover.sign_token == token)
                .options(selectinload(FormApprover.clearance), selectinload(FormApprover.department))
            )
        )
        .scalars()
        .first()
    )
    if not approver:
        raise HTTPException(status_code=404, detail="Sign link not found")
    return templates.TemplateResponse(request, "sign.html", {"approver": approver, "token": str(token)})


@app.post("/sign/{token}")
async def sign_submit(
    request: Request,
    token: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    signer_name = await get_form_value(request, "signer_name", required=True)
    accountability_item = await get_form_value(request, "accountability_item")
    accountability_amount = await get_form_value(request, "accountability_amount")
    signature_data_url = await get_form_value(request, "signature_data_url", required=True)
    approver = (
        (await session.execute(select(FormApprover).where(FormApprover.sign_token == token)))
        .scalars()
        .first()
    )
    if not approver:
        raise HTTPException(status_code=404, detail="Sign link not found")
    if approver.signed_at is not None:
        return RedirectResponse(url=f"/sign/{token}", status_code=303)

    amount_val = None
    if accountability_amount is not None and accountability_amount.strip() != "":
        try:
            amount_val = float(accountability_amount)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid amount") from exc

    approver.signer_name = signer_name
    approver.accountability_item = accountability_item if accountability_item else None
    approver.accountability_amount = amount_val
    approver.signature_data_url = signature_data_url
    approver.signed_at = utc_now()
    approver.disapproval_reason = None
    approver.disapproved_at = None

    await session.flush()
    total = (
        await session.execute(
            select(func.count(FormApprover.id)).where(FormApprover.clearance_id == approver.clearance_id)
        )
    ).scalar_one()
    signed = (
        await session.execute(
            select(func.count(FormApprover.id))
            .where(FormApprover.clearance_id == approver.clearance_id)
            .where(FormApprover.signed_at.is_not(None))
        )
    ).scalar_one()
    if int(total) > 0 and int(signed) == int(total):
        clearance = await session.get(Clearance, approver.clearance_id)
        if clearance and clearance.completed_at is None:
            clearance.completed_at = utc_now()

    await session.commit()
    return RedirectResponse(url=f"/sign/{token}", status_code=303)


@app.post("/disapprove/{token}")
async def disapprove_submit(
    token: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    approver = (
        (await session.execute(select(FormApprover).where(FormApprover.sign_token == token)))
        .scalars()
        .first()
    )
    if not approver:
        raise HTTPException(status_code=404, detail="Sign link not found")

    reason = await get_form_value(request, "disapproval_reason", required=True)
    approver.disapproval_reason = reason
    approver.disapproved_at = utc_now()
    approver.signed_at = None
    approver.signature_data_url = None
    approver.signer_name = None

    await session.commit()
    return RedirectResponse(url=f"/sign/{token}", status_code=303)


@app.get("/api/clearances")
async def api_clearances(session: AsyncSession = Depends(get_session)):
    clearances = (
        await session.execute(
            select(Clearance)
            .where(Clearance.completed_at.is_(None))
            .order_by(Clearance.id.desc())
            .options(selectinload(Clearance.approvers))
        )
    ).scalars().all()
    result = []
    for c in clearances:
        total = len(c.approvers)
        signed = sum(1 for a in c.approvers if a.signed_at is not None)
        result.append(
            {
                "id": c.id,
                "employee_id": c.employee_id,
                "employee_name": c.employee_name,
                "division_department": c.division_department,
                "position": c.position,
                "created_at": c.created_at.isoformat(),
                "total": total,
                "signed": signed,
            }
        )
    return {"clearances": result}


@app.get("/api/clearances/{clearance_id}")
async def api_clearance_detail(
    request: Request, clearance_id: int, session: AsyncSession = Depends(get_session)
):
    clearance = (
        (
            await session.execute(
                select(Clearance)
                .where(Clearance.id == clearance_id)
                .options(selectinload(Clearance.approvers).selectinload(FormApprover.department))
            )
        )
        .scalars()
        .first()
    )
    if not clearance:
        raise HTTPException(status_code=404, detail="Clearance not found")

    current_user_email = ((request.session.get("sso_user") or {}).get("email") or "").strip().lower()
    approvers = []
    for a in sorted(clearance.approvers, key=lambda x: x.department.sort_order if x.department else 0):
        department_email = ((a.department.email if a.department else "") or "").strip().lower()
        can_open = department_email == "" or department_email == current_user_email
        status = "Signed" if a.signed_at is not None else ("Disapproved" if a.disapproved_at is not None else "Pending")
        approvers.append(
            {
                "department": a.department.name if a.department else "",
                "department_email": a.department.email if a.department else None,
                "signed": a.signed_at is not None,
                "signed_at": a.signed_at.isoformat() if a.signed_at else None,
                "sign_url": f"/sign/{a.sign_token}",
                "accountability_item": a.accountability_item,
                "accountability_amount": float(a.accountability_amount) if a.accountability_amount is not None else None,
                "signer_name": a.signer_name,
                "can_open": can_open,
                "disapproval_reason": a.disapproval_reason,
                "disapproved_at": a.disapproved_at.isoformat() if a.disapproved_at else None,
                "status": status,
            }
        )
    return {
        "id": clearance.id,
        "employee_id": clearance.employee_id,
        "employee_name": clearance.employee_name,
        "position": clearance.position,
        "division_department": clearance.division_department,
        "date_hired": clearance.date_hired,
        "date_separated": clearance.date_separated,
        "date_prepared": clearance.date_prepared,
        "reason_for_separation": clearance.reason_for_separation,
        "payroll_type": clearance.payroll_type,
        "payroll_class": clearance.payroll_class,
        "remarks": clearance.remarks,
        "approvers": approvers,
    }
