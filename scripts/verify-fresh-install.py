"""Run inside an isolated, disposable backend container with an empty database.

Creates fictional users and one approved leave. Never run on a real deployment.
Credentials remain in memory and are not printed.
"""

from datetime import date, timedelta
import json
import os
import secrets
import urllib.error
import urllib.request

from app.db.create_admin import BootstrapAdminInput, create_bootstrap_admin
from app.db.mysql import SessionLocal


def request(method, endpoint, payload=None, token=None, expected=200):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        "http://frontend/api" + endpoint,
        data=None if payload is None else json.dumps(payload).encode(),
        headers=headers, method=method,
    )
    try:
        response = urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as error:
        if error.code != expected:
            raise RuntimeError(f"{method} {endpoint}: HTTP {error.code}, expected {expected}") from None
        error.close()
        return None
    with response:
        if response.status != expected:
            raise RuntimeError(f"{method} {endpoint}: HTTP {response.status}, expected {expected}")
        return json.load(response)


def activate(username, temporary_password):
    old_token = request("POST", "/auth/login", {"username": username, "password": temporary_password})["access_token"]
    assert request("GET", "/users/me", token=old_token)["must_change_password"]
    request("GET", "/users/", token=old_token, expected=403)
    password = "Sm0ke-" + secrets.token_urlsafe(18)
    request("POST", "/auth/change-password", {"old_password": temporary_password, "new_password": password}, old_token)
    request("GET", "/users/me", token=old_token, expected=401)
    token = request("POST", "/auth/login", {"username": username, "password": password})["access_token"]
    assert not request("GET", "/users/me", token=token)["must_change_password"]
    return token


def main():
    if os.environ.get("RELEASE_SMOKE_TEST") != "1":
        raise RuntimeError("Requires RELEASE_SMOKE_TEST=1 and a disposable empty database")
    assert request("GET", "/health")["status"] == "ok"
    assert request("GET", "/ready")["status"] == "ready"
    # Bootstrap itself refuses any nonempty users table before writing.
    with SessionLocal() as db:
        administrator, temporary = create_bootstrap_admin(
            db, BootstrapAdminInput("smoke_admin", "SMOKE001", "Smoke Admin", "smoke-admin@example.com"),
        )
        admin_name = administrator.username
    admin = activate(admin_name, temporary)
    print("PASS: bootstrap, forced password change and old-token revocation")
    accounts = {}
    for index, role in enumerate(("manager", "hr", "employee"), start=2):
        payload = {"username": f"smoke_{role}", "employee_no": f"SMOKE00{index}",
                   "name": f"Smoke {role}", "department": "Fictional test", "role": role}
        if role == "employee":
            payload["manager_id"] = accounts["manager"][0]
        created = request("POST", "/users/", payload, admin, expected=201)
        accounts[role] = (created["user"]["id"], activate(payload["username"], created["temporary_password"]))
    employee = accounts["employee"][1]
    manager = accounts["manager"][1]
    hr = accounts["hr"][1]
    tomorrow = (date.today() + timedelta(days=30)).isoformat()
    leave = request("POST", "/leave/", {"leave_type": "annual", "start_date": tomorrow,
                    "end_date": tomorrow, "reason": "Fictional release smoke test"}, employee, expected=201)
    target = "/leave/" + leave["id"]
    request("POST", target + "/manager-review", {"decision": "approved"}, employee, expected=403)
    assert any(item["id"] == leave["id"] for item in request("GET", "/leave/pending/manager", token=manager))
    request("POST", target + "/manager-review", {"decision": "approved"}, manager)
    assert any(item["id"] == leave["id"] for item in request("GET", "/leave/pending/hr", token=hr))
    request("POST", target + "/hr-review", {"decision": "approved"}, hr)
    final = request("GET", target, token=employee)
    assert (final["status"], final["manager_status"], final["hr_status"]) == ("approved",) * 3
    events = request("GET", "/audit/logs?resource_id=" + leave["id"], token=admin)
    actions = {item["action"] for item in events if item["resource_id"] == leave["id"]}
    assert {"CREATE_LEAVE", "MANAGER_APPROVE_LEAVE", "HR_APPROVE_LEAVE"} <= actions
    assert request("GET", "/notifications/unread-count", token=employee)["unread_count"] >= 2
    print("PASS: real MySQL + Nginx HTTP approval, RBAC, audit and notification workflow")


if __name__ == "__main__":
    main()
