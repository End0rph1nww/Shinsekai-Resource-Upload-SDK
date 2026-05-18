from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import pytest

from shinsekai_upload_client import ShinsekaiUploadClient


@dataclass
class Identity:
    id: int
    role: str
    bind_code: str
    device_id: str = ""
    is_active: bool = True
    api_keys: list[str] = field(default_factory=list)


class BindScenarioServer:
    """In-memory server model for browser, EXE, register, claim, and URL bind flows."""

    def __init__(self):
        self.users: dict[int, Identity] = {}
        self.api_keys: dict[str, int] = {}
        self.user_devices: dict[str, int] = {}
        self.user_claims: set[tuple[int, int]] = set()
        self.resources: list[dict] = []
        self.next_user_id = 1
        self.next_key_id = 1
        self.next_token_id = 1
        self.next_resource_id = 1

    def auth_device(self, device_id: str, bind_code: str = "") -> dict:
        code = bind_code.strip().upper()
        if code:
            master = self.find_by_bind_code(code)
            if master:
                slave = self.find_active_by_device(device_id)
                if slave and slave.id != master.id:
                    self.migrate(slave, master)
                    slave.is_active = False
                self.user_devices[device_id] = master.id
                return self.issue_auth(master)

        user = self.find_active_by_device(device_id)
        if not user and device_id in self.user_devices:
            user = self.users[self.user_devices[device_id]]
        if not user:
            user = self.create_user(role="device", device_id=device_id)
        return self.issue_auth(user)

    def register(self, device_id: str, email: str) -> Identity:
        user = self.find_active_by_device(device_id)
        if not user and device_id in self.user_devices:
            candidate = self.users[self.user_devices[device_id]]
            if candidate.role == "device" and candidate.is_active:
                user = candidate
        if not user:
            user = self.create_user(role="user", device_id=device_id)
        user.role = "user"
        return user

    def claim(self, current_api_key: str, bind_code: str) -> dict:
        current = self.users[self.api_keys[current_api_key]]
        slave = self.find_by_bind_code(bind_code.strip().upper())
        if not slave:
            raise ValueError("invalid bind code")
        if slave.id == current.id:
            raise ValueError("cannot claim self")
        self.user_claims.add((current.id, slave.id))
        return self.issue_auth(current, include_api_key=False)

    def upload(self, api_key: str, name: str) -> dict:
        user_id = self.api_keys[api_key]
        resource = {"id": self.next_resource_id, "user_id": user_id, "name": name}
        self.next_resource_id += 1
        self.resources.append(resource)
        return resource

    def my_uploads(self, api_key: str) -> list[dict]:
        user_id = self.api_keys[api_key]
        visible_user_ids = {user_id}
        visible_user_ids.update(source_id for owner_id, source_id in self.user_claims if owner_id == user_id)
        return [resource for resource in self.resources if resource["user_id"] in visible_user_ids]

    def edit_resource(self, api_key: str, resource_id: int, name: str) -> dict:
        resource = self.find_resource(resource_id)
        user_id = self.api_keys[api_key]
        if not resource or not self.can_manage(user_id, resource["user_id"]):
            raise ValueError("forbidden")
        resource["name"] = name
        return resource

    def delete_resource(self, api_key: str, resource_id: int) -> None:
        resource = self.find_resource(resource_id)
        user_id = self.api_keys[api_key]
        if not resource or not self.can_manage(user_id, resource["user_id"]):
            raise ValueError("forbidden")
        self.resources = [item for item in self.resources if item["id"] != resource_id]

    def open_web_with_bind(self, device_id: str, bind_code: str, logged_api_key: str | None = None) -> dict:
        if logged_api_key:
            return self.claim(logged_api_key, bind_code)
        return self.auth_device(device_id, bind_code=bind_code)

    def create_user(self, *, role: str, device_id: str = "") -> Identity:
        user = Identity(
            id=self.next_user_id,
            role=role,
            bind_code=f"B{self.next_user_id:05d}",
            device_id=device_id,
        )
        self.next_user_id += 1
        self.users[user.id] = user
        return user

    def issue_auth(self, user: Identity, *, include_api_key: bool | None = None) -> dict:
        access_token = f"jwt-test-{self.next_token_id:04d}"
        self.next_token_id += 1
        self.api_keys[access_token] = user.id

        if include_api_key is None:
            include_api_key = not user.api_keys

        key = ""
        if include_api_key:
            key = f"sk-test-{self.next_key_id:04d}"
            self.next_key_id += 1
            self.api_keys[key] = user.id
            user.api_keys.append(key)

        return {
            "access_token": access_token,
            "api_key": key,
            "public_id": f"user-{user.id}",
            "bind_code": user.bind_code,
            "is_guest": user.role == "device",
        }

    def migrate(self, source: Identity, target: Identity) -> None:
        for resource in self.resources:
            if resource["user_id"] == source.id:
                resource["user_id"] = target.id
        for key, user_id in list(self.api_keys.items()):
            if user_id == source.id:
                self.api_keys[key] = target.id

    def find_by_bind_code(self, bind_code: str) -> Identity | None:
        return next((user for user in self.users.values() if user.bind_code == bind_code), None)

    def find_active_by_device(self, device_id: str) -> Identity | None:
        return next((user for user in self.users.values() if user.is_active and user.device_id == device_id), None)

    def find_resource(self, resource_id: int) -> dict | None:
        return next((resource for resource in self.resources if resource["id"] == resource_id), None)

    def can_manage(self, user_id: int, resource_owner_id: int) -> bool:
        return user_id == resource_owner_id or (user_id, resource_owner_id) in self.user_claims

    def active_user_count(self) -> int:
        return sum(1 for user in self.users.values() if user.is_active)


def auth_token(auth: dict) -> str:
    return auth.get("api_key") or auth.get("access_token") or ""


def test_q1_browser_guest_upload_then_exe_prebind_syncs_files():
    server = BindScenarioServer()
    browser = server.auth_device("browser-a")
    server.upload(auth_token(browser), "browser-file")

    exe = server.auth_device("exe-a", bind_code=browser["bind_code"])
    assert exe["api_key"] == ""
    server.upload(auth_token(exe), "exe-file")

    assert server.my_uploads(auth_token(browser)) == server.my_uploads(auth_token(exe))
    assert [item["name"] for item in server.my_uploads(auth_token(browser))] == ["browser-file", "exe-file"]


def test_q2_new_browser_without_bind_gets_separate_identity():
    server = BindScenarioServer()
    browser_a = server.auth_device("browser-a")
    server.upload(auth_token(browser_a), "browser-a-file")

    browser_b = server.auth_device("browser-b")

    assert browser_a["bind_code"] != browser_b["bind_code"]
    assert [item["name"] for item in server.my_uploads(auth_token(browser_b))] == []


def test_q3_register_keeps_bind_code_and_old_guest_key():
    server = BindScenarioServer()
    guest = server.auth_device("browser-a")
    before_bind = guest["bind_code"]
    server.upload(auth_token(guest), "guest-file")

    user = server.register("browser-a", "alice@example.com")

    assert user.bind_code == before_bind
    assert user.role == "user"
    assert [item["name"] for item in server.my_uploads(auth_token(guest))] == ["guest-file"]


def test_q4_exe_first_upload_then_community_url_auto_binds_browser():
    server = BindScenarioServer()
    exe = server.auth_device("exe-a")
    server.upload(auth_token(exe), "exe-file")
    url = ShinsekaiUploadClient.build_bind_url(exe["bind_code"], web_url="https://web.test", path="/resources")
    bind = parse_qs(urlparse(url).query)["bind"][0]

    browser = server.open_web_with_bind("browser-a", bind)

    assert browser["bind_code"] == exe["bind_code"]
    assert browser["api_key"] == ""
    assert [item["name"] for item in server.my_uploads(auth_token(browser))] == ["exe-file"]


def test_q4_existing_browser_guest_opens_exe_bind_url_claims_exe_files():
    server = BindScenarioServer()
    browser = server.auth_device("browser-a")
    server.upload(auth_token(browser), "browser-file")
    exe = server.auth_device("exe-a")
    server.upload(auth_token(exe), "exe-file")
    url = ShinsekaiUploadClient.build_bind_url(exe["bind_code"], web_url="https://web.test", path="/resources")
    bind = parse_qs(urlparse(url).query)["bind"][0]

    after = server.open_web_with_bind("browser-a", bind, logged_api_key=auth_token(browser))

    assert after["bind_code"] == browser["bind_code"]
    assert [item["name"] for item in server.my_uploads(auth_token(browser))] == ["browser-file", "exe-file"]
    assert [item["name"] for item in server.my_uploads(auth_token(exe))] == ["exe-file"]


def test_q5_bind_code_is_stable_across_auth_register_and_prebind():
    server = BindScenarioServer()
    first = server.auth_device("browser-a")
    second = server.auth_device("browser-a")
    assert first["bind_code"] == second["bind_code"]

    registered = server.register("browser-a", "alice@example.com")
    assert registered.bind_code == first["bind_code"]

    exe = server.auth_device("exe-a", bind_code=first["bind_code"])
    assert exe["bind_code"] == first["bind_code"]


def test_e1_old_guest_api_key_still_works_after_register_upgrade():
    server = BindScenarioServer()
    guest = server.auth_device("browser-a")
    server.register("browser-a", "alice@example.com")
    server.upload(auth_token(guest), "after-register-file")

    assert [item["name"] for item in server.my_uploads(auth_token(guest))] == ["after-register-file"]


def test_e2_repeated_prebind_same_device_does_not_create_extra_user():
    server = BindScenarioServer()
    master = server.auth_device("browser-a")
    before = server.active_user_count()

    server.auth_device("exe-a", bind_code=master["bind_code"])
    after_first = server.active_user_count()
    server.auth_device("exe-a", bind_code=master["bind_code"])
    after_second = server.active_user_count()

    assert before == after_first
    assert after_first == after_second


def test_e3_claim_already_bound_is_idempotent_and_self_is_rejected():
    server = BindScenarioServer()
    master = server.auth_device("browser-a")
    source = server.auth_device("exe-a")

    server.claim(auth_token(master), source["bind_code"])
    again = server.claim(auth_token(master), source["bind_code"])

    assert again["bind_code"] == master["bind_code"]
    with pytest.raises(ValueError):
        server.claim(auth_token(master), master["bind_code"])


def test_e3_claim_already_bound_by_another_user_returns_current_identity():
    server = BindScenarioServer()
    master_a = server.auth_device("browser-a")
    master_b = server.auth_device("browser-b")
    source = server.auth_device("exe-a")
    server.upload(auth_token(source), "source-file")

    server.claim(auth_token(master_a), source["bind_code"])

    again = server.claim(auth_token(master_b), source["bind_code"])

    assert again["bind_code"] == master_b["bind_code"]
    assert [item["name"] for item in server.my_uploads(auth_token(master_a))] == ["source-file"]
    assert [item["name"] for item in server.my_uploads(auth_token(master_b))] == ["source-file"]
    assert server.find_by_bind_code(source["bind_code"]).is_active is True


def test_e4_no_bind_creates_normal_guest():
    server = BindScenarioServer()
    guest = server.auth_device("browser-a")

    assert guest["is_guest"] is True
    assert guest["bind_code"].startswith("B")
    assert len(guest["bind_code"]) == 6


def test_e4_invalid_bind_falls_back_to_normal_guest_without_merge():
    server = BindScenarioServer()
    master = server.auth_device("browser-a")
    server.upload(auth_token(master), "master-file")

    guest = server.auth_device("browser-b", bind_code="BAD999")

    assert guest["bind_code"] != master["bind_code"]
    assert [item["name"] for item in server.my_uploads(auth_token(guest))] == []
    assert [item["name"] for item in server.my_uploads(auth_token(master))] == ["master-file"]


def test_e5_guest_upload_then_exe_claim_shares_source_files_one_way():
    server = BindScenarioServer()
    browser = server.auth_device("browser-a")
    server.upload(auth_token(browser), "browser-file")
    exe = server.auth_device("exe-a")

    server.claim(auth_token(exe), browser["bind_code"])
    server.upload(auth_token(exe), "exe-file")

    assert [item["name"] for item in server.my_uploads(auth_token(exe))] == ["browser-file", "exe-file"]
    assert [item["name"] for item in server.my_uploads(auth_token(browser))] == ["browser-file"]


def test_shared_claim_does_not_share_claimant_private_files_between_claimants():
    server = BindScenarioServer()
    source = server.auth_device("source-device")
    master_a = server.auth_device("browser-a")
    master_b = server.auth_device("browser-b")
    server.upload(auth_token(source), "shared-source-file")
    server.upload(auth_token(master_a), "a-private-file")
    server.upload(auth_token(master_b), "b-private-file")

    server.claim(auth_token(master_a), source["bind_code"])
    server.claim(auth_token(master_b), source["bind_code"])

    assert [item["name"] for item in server.my_uploads(auth_token(master_a))] == [
        "shared-source-file",
        "a-private-file",
    ]
    assert [item["name"] for item in server.my_uploads(auth_token(master_b))] == [
        "shared-source-file",
        "b-private-file",
    ]


def test_claimed_resource_can_be_edited_and_deleted_by_claimant():
    server = BindScenarioServer()
    source = server.auth_device("source-device")
    claimant = server.auth_device("browser-a")
    outsider = server.auth_device("browser-b")
    resource = server.upload(auth_token(source), "source-file")

    with pytest.raises(ValueError):
        server.edit_resource(auth_token(outsider), resource["id"], "outsider-edit")

    server.claim(auth_token(claimant), source["bind_code"])
    server.edit_resource(auth_token(claimant), resource["id"], "claimant-edit")
    assert [item["name"] for item in server.my_uploads(auth_token(source))] == ["claimant-edit"]

    server.delete_resource(auth_token(claimant), resource["id"])
    assert server.my_uploads(auth_token(source)) == []
    assert server.my_uploads(auth_token(claimant)) == []
