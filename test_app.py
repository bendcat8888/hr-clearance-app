import os
import tempfile
import unittest
from datetime import UTC, datetime

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import HR_App
from models import Base, Clearance, Department, HrAccessUser, SignInLog


class AsyncSessionWrapper:
    def __init__(self, session: Session):
        self._session = session

    async def execute(self, stmt):
        return self._session.execute(stmt)

    async def get(self, model, ident):
        return self._session.get(model, ident)

    def add(self, obj):
        self._session.add(obj)

    async def flush(self):
        self._session.flush()

    async def commit(self):
        self._session.commit()

    async def refresh(self, obj):
        self._session.refresh(obj)

    async def delete(self, obj):
        self._session.delete(obj)


class AppSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.db_path}", future=True)
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine, future=True)

        for i, name in enumerate(HR_App.DEFAULT_APPROVER_DEPARTMENTS[:2], start=1):
            self.session.add(Department(name=name, sort_order=i))
        self.session.commit()

        async def override_get_session():
            yield AsyncSessionWrapper(self.session)

        HR_App.templates.env.globals["format_ph"] = HR_App.format_datetime_ph
        HR_App.templates.env.globals["sso_base_url"] = HR_App.SSO_BASE_URL
        HR_App.templates.env.globals["app_host"] = HR_App.APP_HOST
        HR_App.app.dependency_overrides[HR_App.get_session] = override_get_session

        transport = httpx.ASGITransport(app=HR_App.app)
        self.client = httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            follow_redirects=False,
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        HR_App.app.dependency_overrides.clear()
        self.session.close()
        self.engine.dispose()
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    async def test_full_clearance_flow(self):
        response = await self.client.get("/")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), "/login?next=/")

        response = await self.client.post(
            "/hr/login",
            data={"username": "HR", "password": "HR123", "next": "/history"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), "/history")

        sign_in_logs = self.session.query(SignInLog).all()
        self.assertEqual(len(sign_in_logs), 1)
        self.assertEqual(sign_in_logs[0].provider, "local_hr")
        self.assertEqual(sign_in_logs[0].username, "HR")

        response = await self.client.get("/history")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Recent Sign-Ins", response.text)

        response = await self.client.post(
            "/create",
            data={
                "employee_id": "E001",
                "employee_name": "Test User",
                "position": "Engineer",
                "division_department": "IT",
                "date_hired": "2024-01-01",
                "date_separated": "2026-04-06",
                "date_prepared": "2026-04-06",
                "reason_for_separation": "Resigned",
                "payroll_type": "Monthly",
                "payroll_class": "A",
                "remarks": "Validation run",
                "department_ids": ["1", "2"],
            },
        )
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers.get("location", "").startswith("/status/"))
        clearance_id = int(response.headers["location"].rsplit("/", 1)[-1])

        response = await self.client.get("/api/clearances")
        self.assertEqual(response.status_code, 200)
        clearances = response.json()["clearances"]
        self.assertEqual(len(clearances), 1)
        self.assertEqual(clearances[0]["signed"], 0)
        self.assertEqual(clearances[0]["total"], 2)

        response = await self.client.get(f"/api/clearances/{clearance_id}")
        self.assertEqual(response.status_code, 200)
        detail = response.json()
        self.assertEqual(detail["employee_id"], "E001")
        self.assertEqual(len(detail["approvers"]), 2)

        response = await self.client.get(f"/print/{clearance_id}")
        self.assertEqual(response.status_code, 400)

        for approver in detail["approvers"]:
            sign_url = approver["sign_url"]
            response = await self.client.get(sign_url)
            self.assertEqual(response.status_code, 200)

            response = await self.client.post(
                sign_url,
                data={
                    "signer_name": "Signer",
                    "accountability_item": "",
                    "accountability_amount": "",
                    "signature_data_url": "data:image/png;base64,AAAA",
                },
            )
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers.get("location"), sign_url)

        clearance = self.session.get(Clearance, clearance_id)
        self.assertIsNotNone(clearance.completed_at)

        response = await self.client.get(f"/print/{clearance_id}")
        self.assertEqual(response.status_code, 200)

        self.session.refresh(clearance)
        self.assertEqual(clearance.print_count, 1)
        self.assertIsNotNone(clearance.first_printed_at)
        self.assertIsNotNone(clearance.last_printed_at)

    async def test_sso_completion_authenticates_session(self):
        response = await self.client.post(
            "/auth/sso/complete",
            json={"email": "hr@example.com", "domain": "example.com", "provider": "google"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

        sign_in_logs = self.session.query(SignInLog).all()
        self.assertEqual(len(sign_in_logs), 1)
        self.assertEqual(sign_in_logs[0].provider, "google")
        self.assertEqual(sign_in_logs[0].email, "hr@example.com")

        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)

    async def test_sso_callback_html_route_renders(self):
        response = await self.client.get("/sso_callback.html")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Completing sign-in", response.text)

    async def test_hr_access_always_requires_login(self):
        response = await self.client.post(
            "/auth/sso/complete",
            json={"email": "hr@example.com", "domain": "example.com", "provider": "google"},
        )
        self.assertEqual(response.status_code, 200)

        response = await self.client.get("/hr-access")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), "/hr/login?next=/history")

        response = await self.client.get("/hr/login?next=/history")
        self.assertEqual(response.status_code, 200)
        self.assertIn("HR Login", response.text)

    async def test_change_password_and_reset_to_default(self):
        response = await self.client.post(
            "/hr/login",
            data={"username": "HR", "password": "HR123", "next": "/history"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), "/history")

        response = await self.client.post(
            "/hr/change-password",
            data={
                "current_password": "HR123",
                "new_password": "NewPass456",
                "confirm_password": "NewPass456",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

        user = self.session.query(HrAccessUser).filter_by(username="HR").one()
        self.assertTrue(user.password_hash)

        response = await self.client.get("/hr-access")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), "/hr/login?next=/history")

        response = await self.client.post(
            "/hr/login",
            data={"username": "HR", "password": "NewPass456", "next": "/history"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), "/history")

        response = await self.client.post(
            "/hr/forgot-password",
            data={"next": "/history"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Password reset to default", response.text)

        response = await self.client.post(
            "/hr/login",
            data={"username": "HR", "password": "NewPass456", "next": "/history"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Invalid credentials", response.text)

        response = await self.client.post(
            "/hr/login",
            data={"username": "HR", "password": "HR123", "next": "/history"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), "/history")

    async def test_status_page_logout_and_sign_link_visibility(self):
        dept1 = self.session.get(Department, 1)
        dept2 = self.session.get(Department, 2)
        dept1.email = "match@example.com"
        dept2.email = None
        dept3 = Department(name="LEGAL", email="other@example.com", sort_order=3)
        self.session.add(dept3)
        self.session.commit()

        response = await self.client.post(
            "/create",
            data={
                "employee_id": "E001",
                "employee_name": "Test User",
                "position": "Engineer",
                "division_department": "IT",
                "date_hired": "2024-01-01",
                "date_separated": "2026-04-06",
                "date_prepared": "2026-04-06",
                "reason_for_separation": "Resigned",
                "payroll_type": "Monthly",
                "payroll_class": "A",
                "remarks": "Validation run",
                "department_ids": ["1", "2", str(dept3.id)],
            },
        )
        self.assertEqual(response.status_code, 303)
        clearance_id = int(response.headers["location"].rsplit("/", 1)[-1])

        response = await self.client.post(
            "/auth/sso/complete",
            json={"email": "match@example.com", "domain": "example.com", "provider": "google"},
        )
        self.assertEqual(response.status_code, 200)

        response = await self.client.get(f"/api/clearances/{clearance_id}")
        self.assertEqual(response.status_code, 200)
        approvers = response.json()["approvers"]
        by_name = {item["department"]: item for item in approvers}
        self.assertTrue(by_name["IMMEDIATE SUPERIOR"]["can_open"])
        self.assertTrue(by_name["BU ADMIN"]["can_open"])
        self.assertFalse(by_name["LEGAL"]["can_open"])

        response = await self.client.get(f"/status/{clearance_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Logout", response.text)

    async def test_disapprove_then_sign_clears_disapproval(self):
        response = await self.client.post(
            "/create",
            data={
                "employee_id": "E001",
                "employee_name": "Test User",
                "position": "Engineer",
                "division_department": "IT",
                "date_hired": "2024-01-01",
                "date_separated": "2026-04-06",
                "date_prepared": "2026-04-06",
                "reason_for_separation": "Resigned",
                "payroll_type": "Monthly",
                "payroll_class": "A",
                "remarks": "Validation run",
                "department_ids": ["1"],
            },
        )
        self.assertEqual(response.status_code, 303)
        clearance_id = int(response.headers["location"].rsplit("/", 1)[-1])

        detail = (await self.client.get(f"/api/clearances/{clearance_id}")).json()
        sign_url = detail["approvers"][0]["sign_url"]

        response = await self.client.post(
            sign_url.replace("/sign/", "/disapprove/"),
            data={"disapproval_reason": "Missing turnover documents"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), sign_url)

        detail = (await self.client.get(f"/api/clearances/{clearance_id}")).json()
        approver = detail["approvers"][0]
        self.assertEqual(approver["status"], "Disapproved")
        self.assertEqual(approver["disapproval_reason"], "Missing turnover documents")

        response = await self.client.post(
            sign_url,
            data={
                "signer_name": "Signer",
                "accountability_item": "",
                "accountability_amount": "",
                "signature_data_url": "data:image/png;base64,AAAA",
            },
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), sign_url)

        detail = (await self.client.get(f"/api/clearances/{clearance_id}")).json()
        approver = detail["approvers"][0]
        self.assertEqual(approver["status"], "Signed")
        self.assertIsNone(approver["disapproval_reason"])

    async def test_department_add_and_edit_persists(self):
        response = await self.client.post(
            "/hr/login",
            data={"username": "HR", "password": "HR123", "next": "/history"},
        )
        self.assertEqual(response.status_code, 303)

        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("APPROVER DEPARTMENTS", response.text)
        self.assertIn("addDepartmentBtn", response.text)
        self.assertIn("edit-department-btn", response.text)
        self.assertIn("deleteDepartmentBtn", response.text)

        response = await self.client.post(
            "/departments",
            json={"name": "LEGAL", "email": "legal@example.com"},
        )
        self.assertEqual(response.status_code, 200)
        department = response.json()["department"]
        self.assertEqual(department["name"], "LEGAL")
        self.assertEqual(department["email"], "legal@example.com")

        stored = self.session.get(Department, department["id"])
        self.assertIsNotNone(stored)
        self.assertEqual(stored.name, "LEGAL")
        self.assertEqual(stored.email, "legal@example.com")

        response = await self.client.post(
            f"/departments/{department['id']}",
            json={"name": "LEGAL AFFAIRS", "email": "counsel@example.com"},
        )
        self.assertEqual(response.status_code, 200)
        updated = response.json()["department"]
        self.assertEqual(updated["name"], "LEGAL AFFAIRS")
        self.assertEqual(updated["email"], "counsel@example.com")

        self.session.refresh(stored)
        self.assertEqual(stored.name, "LEGAL AFFAIRS")
        self.assertEqual(stored.email, "counsel@example.com")

        response = await self.client.post(f"/departments/{department['id']}/delete")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertIsNone(self.session.get(Department, department["id"]))

    async def test_department_delete_blocked_when_in_use(self):
        response = await self.client.post(
            "/hr/login",
            data={"username": "HR", "password": "HR123", "next": "/history"},
        )
        self.assertEqual(response.status_code, 303)

        response = await self.client.post(
            "/create",
            data={
                "employee_id": "E001",
                "employee_name": "Test User",
                "position": "Engineer",
                "division_department": "IT",
                "date_hired": "2024-01-01",
                "date_separated": "2026-04-06",
                "date_prepared": "2026-04-06",
                "reason_for_separation": "Resigned",
                "payroll_type": "Monthly",
                "payroll_class": "A",
                "remarks": "Validation run",
                "department_ids": ["1"],
            },
        )
        self.assertEqual(response.status_code, 303)

        response = await self.client.post("/departments/1/delete")
        self.assertEqual(response.status_code, 400)
        self.assertIn("already used", response.text)
        self.assertIsNotNone(self.session.get(Department, 1))

    async def test_utc_now_is_timezone_aware(self):
        now = HR_App.utc_now()
        self.assertIsNotNone(now.tzinfo)
        self.assertEqual(now.utcoffset(), datetime.now(UTC).utcoffset())


if __name__ == "__main__":
    unittest.main()
