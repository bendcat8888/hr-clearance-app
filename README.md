# Dynamic HR Clearance System (FastAPI + Postgres + SSO)

A web-based HR clearance workflow with:
- HR form generation (dynamic departments)
- Per-department signature pages (unique sign links)
- Live status dashboard (active clearances)
- Print-ready clearance form
- Innogen Universal SSO login (email-first)
- Department email directory + SMTP notifications + send logs

## Tech Stack
- FastAPI (Uvicorn)
- PostgreSQL (asyncpg)
- SQLAlchemy Async
- Jinja2 templates + Tailwind CDN
- Docker + Docker Compose

## Quick Start (Docker)

### 1) Create `.env` (same folder as `docker-compose.yml`)
Create a `.env` file and set at minimum:

```env
APP_HOST=https://clearance.innogen.local
SSO_BASE_URL=https://sso.innogen-pharma.com

HR_SESSION_SECRET=REPLACE_WITH_A_LONG_RANDOM_STRING
COOKIE_HTTPS_ONLY=1

SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_USE_SSL=1
SMTP_USERNAME=no-reply@innogen-pharma.com
SMTP_PASSWORD="YOUR_GMAIL_APP_PASSWORD"
SMTP_FROM=no-reply@innogen-pharma.com
SMTP_FROM_NAME=HR Clearance
EMAIL_NOTIFICATIONS_ENABLED=1
```

Notes:
- `HR_SESSION_SECRET` must be a long random string and should stay stable in production.
- If `SMTP_PASSWORD` contains spaces, keep it inside quotes.

### 2) Start
Initial build/run:

```bash
sudo docker compose up -d --build
```

After code-only updates (no dependency changes), a lighter option:

```bash
sudo docker compose restart app
```

### 3) Open the app
- Main app: `https://clearance.innogen.local/`
- SSO login page: `/login`
- Status dashboard: `/status`
- HR Access: `/history`

## Authentication (Innogen Universal SSO)
- `/` is protected. If not authenticated, it redirects to `/login`.
- `/login` is email-first:
  - `@innogen-pharma.com` → Google sign-in
  - `@innogen-pharma.ph` → cPanel password flow
  - Apple sign-in is always available
- `/sso-callback` verifies the SSO session and creates the app session.

SSO API calls used:
- `GET {SSO_BASE_URL}/api/v1/auth/method?email=...`
- `GET {SSO_BASE_URL}/api/v1/verify-session` (cookie-based)
- OAuth redirects:
  - Google: `{SSO_BASE_URL}/api/v1/oauth/google/start?email={email}&return_to={APP_HOST}/sso-callback`
  - Apple: `{SSO_BASE_URL}/api/v1/oauth/apple/start?return_to={APP_HOST}/sso-callback`

## Workflow

### 1) Configure department emails
On the main form page (`/`), the **Approver Departments** section includes:
- **Add** button (create department + email)
- **Pencil edit** (update name/email)
- Optional remove (if not already used by historical records)

These values persist in the database and are reused for future clearances.

### 2) Generate a clearance
Fill out the employee details, choose departments, and submit.
The app creates:
- A `Clearance` record
- `FormApprover` rows (one per department), each with a unique `sign_token`

### 3) Automatic email notifications (SMTP)
After a clearance is created, the app sends an email to each selected department that has an email configured.

Email includes:
- Employee information
- A button linking to the department’s signing URL:
  `https://clearance.innogen.local/sign/{token}`
- InnoGen logo (`/static/img/InnoGen.png`)
- Footer notice:
  - Note: This is an automated notification. Please do not reply to this email.
  - InnoGen's IT Department © 2026
  - This is an automated email notification.

### 4) Department signing
Each department signs via:
- `GET /sign/{token}`
- `POST /sign/{token}` (signature pad + submission)

### 5) Live status tracking
- `GET /status` shows all active clearances
- `GET /status/{id}` shows clearance detail

### 6) Printing
Completed clearances can be printed via:
- `GET /print/{clearance_id}`

## Email Send Logs (HR Access)
The HR Access page (`/history`) includes:
- Recent sign-in logs
- Email notification logs (sent/failed + error)

This is used to audit who was notified and whether SMTP succeeded.

## Data Persistence
PostgreSQL data is stored in a Docker volume:
- `postgres_data`

Updating code does not delete your data.

## Common Troubleshooting

### Emails not sending
- Confirm `.env` has:
  - `SMTP_USERNAME`
  - `SMTP_PASSWORD`
  - `EMAIL_NOTIFICATIONS_ENABLED=1`
- Check app logs:
  ```bash
  sudo docker compose logs -f app
  ```
- Check HR Access → Email Notifications logs for `failed` status and error message.

### Login keeps redirecting back to `/login`
- Ensure `HR_SESSION_SECRET` is set and stable (not a placeholder).
- Confirm `COOKIE_HTTPS_ONLY=1` when using HTTPS.

### Nginx / WAF issues with signature submit (403)
If using Nginx + ModSecurity, signature payloads can be blocked. Typical fix is to increase body size and disable WAF for `/sign/` location only.

## Project Structure
- `HR_App.py` — main FastAPI entrypoint
- `models.py` — SQLAlchemy models
- `database.py` — async engine/session
- `templates/` — UI templates
- `assets/` — static files (logo, favicon, etc.)
- `docker-compose.yml` — app + postgres
- `Dockerfile` — container build
