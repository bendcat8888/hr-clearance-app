# Page Design — Dynamic HR Clearance System (Desktop-first)

## Global Styles (applies to all pages)
- Layout system: Hybrid CSS Grid (page scaffolding) + Flexbox (row alignment inside cards/toolbars).
- Spacing & sizing: 8px spacing scale; max content width 1200px; dense tables for admin views.
- Colors (tokens):
  - Background: #F7F8FA
  - Surface (cards): #FFFFFF
  - Text: #111827
  - Muted text: #6B7280
  - Primary: #1D4ED8 (buttons/links)
  - Success: #16A34A (approved)
  - Warning: #F59E0B (pending)
  - Danger: #DC2626 (rejected)
  - Border: #E5E7EB
- Typography: system font stack; base 14–16px; table text 13–14px; headings 20/24/28.
- Buttons:
  - Primary: filled, hover darken, disabled 60% opacity.
  - Secondary: outline with border.
  - Destructive: red filled.
- Links: underlined on hover; keep visible focus rings for keyboard.
- Form fields: consistent label alignment; validation messages under fields.
- Responsive behavior (desktop-first):
  - ≥1200px: full-width dashboard with side-by-side filters.
  - 768–1199px: filters wrap to 2 rows; tables horizontally scroll in container.
  - <768px (optional): stacked cards; table becomes card list where feasible.

---

## Page 1: HR Dashboard

### Meta Information
- Title: "HR Clearance Dashboard"
- Description: "Create and track employee clearance requests across departments."
- Open Graph: title + description; no images required.

### Page Structure
- Overall: Header bar + main content container.
- Main content uses a two-section stacked layout:
  1) Toolbar (filters + primary actions)
  2) Results (table) + optional empty state

### Sections & Components
1) Top Header Bar
- Left: Product name (“HR Clearance System”).
- Right: Optional user indicator (HR Admin) + logout (if applicable).

2) Toolbar Card (Filters + Actions)
- Layout: CSS Grid with columns for filters; button group aligned right.
- Filters (minimum):
  - Employee search (text input)
  - Status dropdown (Pending / Completed / Rejected / All)
  - Date range (from/to) or single “Created after” date
- Actions:
  - Primary button: “New Clearance Request”

3) Clearance Requests Table
- Columns:
  - Employee (name + employee number)
  - Created date
  - Status badge
  - Progress summary (e.g., “3/6 signed”)
  - Last updated
  - Row action: “Open”
- States:
  - Loading: skeleton rows
  - Empty: message + “Create new request” CTA

4) Create Request (either modal or separate page as per route)
- Form fields:
  - Employee selector (searchable)
  - Departments checklist (multi-select)
  - Submit button (creates request and generates sign UUIDs)
- Confirmation:
  - Show created request link + quick access to Clearance Detail

---

## Page 2: Clearance Detail

### Meta Information
- Title: "Clearance Details"
- Description: "Review departmental approvals and signature records for a clearance request."
- Open Graph: title + description.

### Page Structure
- Layout: Two-column desktop layout.
  - Left (main, ~8/12): timeline + department cards
  - Right (sidebar, ~4/12): summary + link tools + print/export

### Sections & Components
1) Breadcrumbs
- “HR Dashboard / Clearance / {Employee Name}”

2) Summary Panel (Sidebar)
- Employee info block: name, employee no, email (if available)
- Request info: created_at, overall status badge, completed_at (if set)
- Actions:
  - Button: “Print Record” (opens print route)
  - Button/link: “Export” (if implemented as download)

3) Department Status Timeline (Main)
- Presented as stacked cards, one per department step.
- Each department card includes:
  - Department name + status badge
  - Timestamps: signed_at (if signed)
  - Signer name (if captured)
  - Comment text (if present)
  - Signature preview area:
    - If signed: show image preview (contained, max height ~120px)
    - If pending: show placeholder “Awaiting signature”

4) Sign Link Tools (Sidebar or within each department card)
- For each department step:
  - Read-only input containing the UUID sign URL
  - “Copy link” button
  - State label: “Pending link” or “Completed”

5) Error/Integrity States
- If clearance ID not found: show simple not-found with link back to dashboard.

---

## Page 3: Department Sign Page (UUID)

### Meta Information
- Title: "Department Clearance Sign-Off"
- Description: "Review the clearance request and sign to approve or reject."
- Open Graph: title + description.

### Page Structure
- Centered single-column layout (max width ~720px) to keep signing focused.
- Sections stacked as cards.

### Sections & Components
1) Header
- Minimal branding + page title.
- Small helper text: “This link is unique to your department.”

2) Request Context Card
- Employee name + employee number
- Clearance created date
- Department name
- Current step status

3) Signature Capture Card
- Signature pad area (canvas)
  - Size: full width, 220–280px height
  - Visual: light border + subtle background
- Controls (row):
  - Secondary button: “Clear signature”
  - Optional: “Undo” (only if implemented)
- Validation:
  - Block submission if no strokes captured

4) Decision Card
- Inputs:
  - Signer name (text input) — required
  - Comment (textarea) — optional
- Actions:
  - Primary: “Approve & Sign”
  - Destructive: “Reject” (still requires signature + signer name)

5) Completion / Lock States
- If UUID invalid/expired: show error message and no signature pad.
- If already completed:
  - Show read-only signed record (status, signer, timestamp, signature preview, comment)
  - Hide submit buttons; show “Already completed” banner.

6) Interaction & Feedback
- On submit:
  - Disable buttons, show inline spinner
  - On success: show confirmation banner and switch page to read-only
- Error handling:
  - Show a clear message (e.g., “Unable to save signature, please retry.”)

---

## Print View: Clearance Record (accessed from Clearance Detail)

### Meta Information
- Title: "Printable Clearance Record"
- Description: "Print-ready clearance record with department signatures."

### Layout
- A4-friendly single column, white background.
- Avoid interactive controls; render signatures as images.

### Content Blocks
- Header: organization name + “Employee Clearance Record”
- Employee block: name, employee no, email
- Table: department, status, signer, signed_at
- Signature blocks: one per department (small, consistent sizing)
- Footer: generated timestamp