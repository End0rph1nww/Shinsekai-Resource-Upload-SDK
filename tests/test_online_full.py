from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest
import requests

from shinsekai_upload_client import ShinsekaiUploadClient, ShinsekaiUploadError
from tests.test_online_smoke import (
    BASE_URL,
    auth_headers,
    cleanup_resources,
    fetch_my_uploads,
    require_online,
    unique_name,
    write_payload,
)


pytestmark = [pytest.mark.online, pytest.mark.online_full]


def require_full_online() -> None:
    require_online()
    if os.getenv("SHINSEKAI_ONLINE_FULL") != "1":
        pytest.skip("set SHINSEKAI_ONLINE_FULL=1 to run destructive full online tests")


def full_device_client(
    tmp_path: Path,
    prefix: str,
    *,
    bind_code: str | None = None,
    parallel_uploads: int = 5,
) -> ShinsekaiUploadClient:
    device_path = str(tmp_path / f"{prefix}_device_id.txt")
    for attempt in range(2):
        try:
            return ShinsekaiUploadClient.from_device_file(
                device_path,
                bind_code=bind_code,
                base_url=BASE_URL,
                parallel_uploads=parallel_uploads,
            )
        except ShinsekaiUploadError as exc:
            if attempt == 0 and "HTTP 429" in str(exc):
                time.sleep(65)
                continue
            raise
    raise AssertionError("device auth retry loop exhausted")


def upload_char(client: ShinsekaiUploadClient, tmp_path: Path, prefix: str, created: list[int]) -> dict:
    path = tmp_path / f"{unique_name(prefix)}.char"
    write_payload(path, 72 * 1024, path.stem)
    result = client.upload_resource(
        unique_name(prefix),
        str(path),
        "character_pack",
        uploader="codex-online-full",
        description=f"online full test: {prefix}",
        verified_models=["GPT-Sovits"],
    )
    created.append(int(result["id"]))
    return result


def get_me(client: ShinsekaiUploadClient) -> dict:
    resp = requests.get(f"{BASE_URL}/users/me", headers=auth_headers(client), timeout=60)
    assert resp.ok, resp.text
    data = resp.json()
    assert isinstance(data, dict)
    return data


def register_user_from_device(client: ShinsekaiUploadClient, *, email: str, password: str) -> dict:
    device_id = client.device_auth.device_id if client.device_auth else ""
    resp = requests.post(
        f"{BASE_URL}/auth/register",
        headers={"Content-Type": "application/json"},
        json={
            "email": email,
            "password": password,
            "nickname": email.split("@")[0],
            "device_id": device_id,
        },
        timeout=60,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert isinstance(data, dict)
    return data


def register_user(email: str, password: str) -> dict:
    resp = requests.post(
        f"{BASE_URL}/auth/register",
        headers={"Content-Type": "application/json"},
        json={
            "email": email,
            "password": password,
            "nickname": email.split("@")[0],
        },
        timeout=60,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert isinstance(data, dict)
    return data


def login_client(email: str, password: str, *, device_id: str = "") -> ShinsekaiUploadClient:
    resp = requests.post(
        f"{BASE_URL}/auth/login",
        headers={"Content-Type": "application/json"},
        json={"email": email, "password": password, "device_id": device_id},
        timeout=60,
    )
    assert resp.ok, resp.text
    token = resp.json()["access_token"]
    return ShinsekaiUploadClient("", access_token=token, base_url=BASE_URL, parallel_uploads=5)


def create_api_key(jwt_client: ShinsekaiUploadClient) -> tuple[int, str]:
    resp = requests.post(
        f"{BASE_URL}/keys",
        headers=auth_headers(jwt_client),
        json={"name": unique_name("online_full_key")},
        timeout=60,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return int(data["id"]), data["key"]


def revoke_api_key(jwt_client: ShinsekaiUploadClient, key_id: int) -> None:
    requests.delete(f"{BASE_URL}/keys/{key_id}", headers=auth_headers(jwt_client), timeout=60)


def upload_ids(client: ShinsekaiUploadClient) -> set[int]:
    return {int(item["id"]) for item in client.list_my_uploads()}


def upload_by_id(client: ShinsekaiUploadClient, resource_id: int) -> dict:
    for item in client.list_my_uploads():
        if int(item["id"]) == int(resource_id):
            return item
    raise AssertionError(f"resource {resource_id} not found")


def test_online_full_q1_browser_guest_upload_then_opening_exe_bind_claims_exe_files(tmp_path: Path):
    require_full_online()
    password = "CodexOnlineFull123!"
    email = f"codex-full-q1-{uuid.uuid4().hex[:12]}@example.com"
    browser_created: list[int] = []
    exe_created: list[int] = []
    browser = full_device_client(tmp_path, "full_q1_browser")
    device_id = browser.device_auth.device_id

    try:
        browser_file = upload_char(browser, tmp_path, "full_q1_browser", browser_created)
        exe = full_device_client(tmp_path, "full_q1_exe", bind_code=browser.bind_code)
        exe_file = upload_char(exe, tmp_path, "full_q1_exe", exe_created)
        browser.claim_bind_code(exe.bind_code)
        browser_ids = {item["id"] for item in fetch_my_uploads(browser)}
        exe_ids = {item["id"] for item in fetch_my_uploads(exe)}

        assert exe.bind_code != browser.bind_code
        assert browser_file["id"] in browser_ids
        assert exe_file["id"] not in browser_ids
        assert browser_file["id"] not in exe_ids
        assert exe_file["id"] in exe_ids
        register_user_from_device(browser, email=email, password=password)
        registered = login_client(email, password, device_id=device_id)
        registered_ids = {item["id"] for item in fetch_my_uploads(registered)}
        assert browser_file["id"] in registered_ids
        assert exe_file["id"] in registered_ids
    finally:
        cleanup_resources(registered if "registered" in locals() else browser, browser_created)
        if "exe" in locals():
            cleanup_resources(exe, exe_created)


def test_online_full_q2_new_device_without_bind_is_separate_identity(tmp_path: Path):
    require_full_online()
    created: list[int] = []
    browser_a = full_device_client(tmp_path, "full_q2_browser_a")

    try:
        uploaded = upload_char(browser_a, tmp_path, "full_q2_browser_a", created)
        browser_b = full_device_client(tmp_path, "full_q2_browser_b")
        ids = {item["id"] for item in fetch_my_uploads(browser_b)}

        assert browser_b.bind_code != browser_a.bind_code
        assert uploaded["id"] not in ids
    finally:
        cleanup_resources(browser_a, created)


def test_online_full_q3_register_keeps_bind_code_and_current_device_key_identity(tmp_path: Path):
    require_full_online()
    created: list[int] = []
    password = "CodexOnlineFull123!"
    email = f"codex-full-{uuid.uuid4().hex[:12]}@example.com"
    guest = full_device_client(tmp_path, "full_q3_guest")
    before_bind = guest.bind_code
    device_id = guest.device_auth.device_id

    try:
        first = upload_char(guest, tmp_path, "full_q3_before_register", created)
        register_user_from_device(guest, email=email, password=password)
        registered = login_client(email, password, device_id=device_id)
        after_me = get_me(registered)
        ids = {item["id"] for item in fetch_my_uploads(registered)}

        assert after_me["role"] == "user"
        assert after_me["bind_code"] == before_bind
        assert first["id"] in ids
        old_key_me = requests.get(
            f"{BASE_URL}/users/me",
            headers={"Authorization": f"Bearer {guest.api_key}"},
            timeout=60,
        )
        assert old_key_me.status_code == 200
        assert old_key_me.json()["role"] == "user"
    finally:
        cleanup_resources(registered if "registered" in locals() else guest, created)


def test_online_full_existing_user_login_jwt_survives_stale_guest_key_users_me_401(tmp_path: Path):
    require_full_online()
    password = "CodexOnlineFull123!"
    email = f"codex-full-stale-key-{uuid.uuid4().hex[:12]}@example.com"
    register_user(email=email, password=password)
    guest = full_device_client(tmp_path, "full_stale_key_guest")
    old_key = guest.api_key
    device_id = guest.device_auth.device_id

    registered = login_client(email, password, device_id=device_id)
    stale = requests.get(
        f"{BASE_URL}/users/me",
        headers={"Authorization": f"Bearer {old_key}"},
        timeout=60,
    )
    me = get_me(registered)

    assert old_key
    assert stale.status_code == 401
    assert me["role"] == "user"


def test_online_full_q4_exe_first_upload_then_web_claims_exe_files(tmp_path: Path):
    require_full_online()
    password = "CodexOnlineFull123!"
    email = f"codex-full-q4-{uuid.uuid4().hex[:12]}@example.com"
    exe_created: list[int] = []
    web_created: list[int] = []
    exe = full_device_client(tmp_path, "full_q4_exe")

    try:
        uploaded = upload_char(exe, tmp_path, "full_q4_exe", exe_created)
        web = full_device_client(tmp_path, "full_q4_web")
        device_id = web.device_auth.device_id
        web.claim_bind_code(exe.bind_code)
        web_ids = {item["id"] for item in fetch_my_uploads(web)}

        assert web.bind_code != exe.bind_code
        assert web.device_auth and web.device_auth.is_guest is True
        assert uploaded["id"] not in web_ids

        web_file = upload_char(web, tmp_path, "full_q4_web_future", web_created)
        exe_ids = {item["id"] for item in fetch_my_uploads(exe)}
        web_ids = {item["id"] for item in fetch_my_uploads(web)}

        assert uploaded["id"] in exe_ids
        assert web_file["id"] not in exe_ids
        assert uploaded["id"] not in web_ids
        assert web_file["id"] in web_ids
        register_user_from_device(web, email=email, password=password)
        registered = login_client(email, password, device_id=device_id)
        registered_ids = {item["id"] for item in fetch_my_uploads(registered)}
        assert uploaded["id"] in registered_ids
        assert web_file["id"] in registered_ids
    finally:
        cleanup_resources(exe, exe_created)
        if "web" in locals():
            cleanup_resources(registered if "registered" in locals() else web, web_created)


def test_online_full_q4_existing_browser_claims_exe_bind_code(tmp_path: Path):
    require_full_online()
    password = "CodexOnlineFull123!"
    email = f"codex-full-q4b-{uuid.uuid4().hex[:12]}@example.com"
    browser_created: list[int] = []
    exe_created: list[int] = []
    browser = full_device_client(tmp_path, "full_q4b_browser")
    exe = full_device_client(tmp_path, "full_q4b_exe")
    device_id = browser.device_auth.device_id

    try:
        browser_file = upload_char(browser, tmp_path, "full_q4b_browser", browser_created)
        exe_file = upload_char(exe, tmp_path, "full_q4b_exe", exe_created)
        browser.claim_bind_code(exe.bind_code)
        ids = {item["id"] for item in fetch_my_uploads(browser)}

        assert browser_file["id"] in ids
        assert exe_file["id"] not in ids
        register_user_from_device(browser, email=email, password=password)
        registered = login_client(email, password, device_id=device_id)
        registered_ids = {item["id"] for item in fetch_my_uploads(registered)}
        assert browser_file["id"] in registered_ids
        assert exe_file["id"] in registered_ids
    finally:
        cleanup_resources(registered if "registered" in locals() else browser, browser_created)
        cleanup_resources(exe, exe_created)


def test_online_full_q5_bind_code_stable_after_register_and_third_bind_argument_ignored(tmp_path: Path):
    require_full_online()
    password = "CodexOnlineFull123!"
    email = f"codex-full-stable-{uuid.uuid4().hex[:12]}@example.com"
    guest = full_device_client(tmp_path, "full_q5_guest")
    before_bind = guest.bind_code
    device_id = guest.device_auth.device_id

    register_user_from_device(guest, email=email, password=password)
    registered = login_client(email, password, device_id=device_id)
    third = full_device_client(tmp_path, "full_q5_third", bind_code=before_bind)

    assert get_me(registered)["bind_code"] == before_bind
    assert third.bind_code != before_bind


def test_online_full_logout_refresh_device_auth_returns_new_guest(tmp_path: Path):
    require_full_online()
    password = "CodexOnlineFull123!"
    email = f"codex-full-logout-{uuid.uuid4().hex[:12]}@example.com"
    device_path = tmp_path / "full_logout_device_id.txt"
    guest = ShinsekaiUploadClient.from_device_file(str(device_path), base_url=BASE_URL)
    before_bind = guest.bind_code
    device_id = guest.device_auth.device_id if guest.device_auth else ""

    register_user_from_device(guest, email=email, password=password)
    registered = login_client(email, password, device_id=device_id)
    refreshed = ShinsekaiUploadClient.from_device_file(str(device_path), base_url=BASE_URL)

    assert get_me(registered)["bind_code"] == before_bind
    assert refreshed.device_auth.is_guest is True
    assert refreshed.bind_code != before_bind


def test_online_full_invalid_bind_code_creates_independent_guest(tmp_path: Path):
    require_full_online()
    created: list[int] = []
    master = full_device_client(tmp_path, "full_invalid_master")

    try:
        master_file = upload_char(master, tmp_path, "full_invalid_master", created)
        guest = full_device_client(tmp_path, "full_invalid_guest", bind_code="BAD999")

        assert guest.bind_code != master.bind_code
        assert guest.bind_code != "BAD999"
        assert master_file["id"] in {item["id"] for item in fetch_my_uploads(master)}
        assert master_file["id"] not in {item["id"] for item in fetch_my_uploads(guest)}
    finally:
        cleanup_resources(master, created)


def test_online_full_repeated_device_auth_with_bind_code_argument_is_idempotent(tmp_path: Path):
    require_full_online()
    master = full_device_client(tmp_path, "full_repeat_master")
    first = full_device_client(tmp_path, "full_repeat_slave", bind_code=master.bind_code)
    second = full_device_client(tmp_path, "full_repeat_slave", bind_code=master.bind_code)

    assert first.bind_code != master.bind_code
    assert second.bind_code == first.bind_code


def test_online_full_claim_self_rejected_and_already_claimed_by_other_returns_current_identity(tmp_path: Path):
    require_full_online()
    password = "CodexOnlineFull123!"
    master_a_created: list[int] = []
    guest_created: list[int] = []
    master_a_guest = full_device_client(tmp_path, "full_claim_master_a")
    master_b_guest = full_device_client(tmp_path, "full_claim_master_b")
    email_a = f"codex-full-claim-a-{uuid.uuid4().hex[:12]}@example.com"
    email_b = f"codex-full-claim-b-{uuid.uuid4().hex[:12]}@example.com"
    device_id_a = master_a_guest.device_auth.device_id
    device_id_b = master_b_guest.device_auth.device_id
    register_user_from_device(master_a_guest, email=email_a, password=password)
    register_user_from_device(master_b_guest, email=email_b, password=password)
    master_a = login_client(email_a, password, device_id=device_id_a)
    master_b = login_client(email_b, password, device_id=device_id_b)
    master_a_bind = get_me(master_a)["bind_code"]
    guest = full_device_client(tmp_path, "full_claim_guest")

    try:
        master_a_file = upload_char(master_a, tmp_path, "full_claim_master_a", master_a_created)
        guest_file = upload_char(guest, tmp_path, "full_claim_guest", guest_created)

        with pytest.raises(ShinsekaiUploadError):
            master_a.claim_bind_code(master_a_bind)

        master_a.claim_bind_code(guest.bind_code)

        # 新版绑定码是共享认领：A 和 B 都能看到 guest 的资源，但 B 不会看到 A 自己上传的资源。
        again = master_b.claim_bind_code(guest.bind_code)
        a_ids = {item["id"] for item in fetch_my_uploads(master_a)}
        b_ids = {item["id"] for item in fetch_my_uploads(master_b)}

        assert again.bind_code == master_b.bind_code
        assert guest_file["id"] in a_ids
        assert guest_file["id"] in b_ids
        assert master_a_file["id"] in a_ids
        assert master_a_file["id"] not in b_ids

        edited = master_b.edit_resource(
            int(guest_file["id"]),
            description="online full test: claimed resource edited by another claimant",
            tags=["codex-online-full", "claimed-edit"],
            verified_models=["GPT-Sovits"],
            resource_type="character_pack",
        )
        assert edited["id"] == guest_file["id"]
        assert upload_by_id(master_a, int(guest_file["id"]))["description"] == "online full test: claimed resource edited by another claimant"
        assert upload_by_id(guest, int(guest_file["id"]))["description"] == "online full test: claimed resource edited by another claimant"

        deleted = master_b.delete_resource(int(guest_file["id"]))
        assert deleted["status"] == "deleted"
        guest_created.remove(int(guest_file["id"]))
        assert guest_file["id"] not in upload_ids(master_a)
        assert guest_file["id"] not in upload_ids(master_b)
        assert guest_file["id"] not in upload_ids(guest)
    finally:
        cleanup_resources(master_a, master_a_created)
        cleanup_resources(guest, guest_created)


def test_online_full_unclaimed_resource_edit_delete_forbidden(tmp_path: Path):
    require_full_online()
    owner_created: list[int] = []
    owner = full_device_client(tmp_path, "full_forbidden_owner")
    outsider = full_device_client(tmp_path, "full_forbidden_outsider")

    try:
        uploaded = upload_char(owner, tmp_path, "full_forbidden_owner", owner_created)
        resource_id = int(uploaded["id"])

        with pytest.raises(ShinsekaiUploadError):
            outsider.edit_resource(
                resource_id,
                description="outsider should not edit",
                verified_models=["Qwen"],
                resource_type="character_pack",
            )
        with pytest.raises(ShinsekaiUploadError):
            outsider.delete_resource(resource_id)

        assert resource_id in upload_ids(owner)
        assert resource_id not in upload_ids(outsider)
    finally:
        cleanup_resources(owner, owner_created)


def test_online_full_registered_bind_can_be_claimed_but_device_auth_does_not_prebind(tmp_path: Path):
    require_full_online()
    created: list[int] = []
    third_created: list[int] = []
    password = "CodexOnlineFull123!"
    email = f"codex-full-registered-bind-{uuid.uuid4().hex[:12]}@example.com"
    outsider_email = f"codex-full-outsider-{uuid.uuid4().hex[:12]}@example.com"
    guest = full_device_client(tmp_path, "full_registered_bind_guest")
    before_bind = guest.bind_code
    device_id = guest.device_auth.device_id

    try:
        register_user_from_device(guest, email=email, password=password)
        registered = login_client(email, password, device_id=device_id)
        outsider = full_device_client(tmp_path, "full_registered_bind_outsider")
        registered_file = upload_char(registered, tmp_path, "full_registered_bind_owner", created)

        claim = outsider.claim_bind_code(before_bind)
        assert claim.bind_code == outsider.bind_code
        assert int(registered_file["id"]) not in upload_ids(outsider)
        outsider_device_id = outsider.device_auth.device_id
        register_user_from_device(outsider, email=outsider_email, password=password)
        outsider_registered = login_client(outsider_email, password, device_id=outsider_device_id)
        assert int(registered_file["id"]) in upload_ids(outsider_registered)

        third = full_device_client(tmp_path, "full_registered_bind_third", bind_code=before_bind)
        uploaded = upload_char(third, tmp_path, "full_registered_bind_third", created)
        third_created.append(int(uploaded["id"]))
        created.remove(int(uploaded["id"]))

        assert third.bind_code != before_bind
        assert int(uploaded["id"]) not in upload_ids(registered)
        assert int(uploaded["id"]) in upload_ids(third)
    finally:
        cleanup_resources(registered if "registered" in locals() else guest, created)
        if "third" in locals():
            cleanup_resources(third, third_created)


def test_online_full_duplicate_sha256_returns_existing_resource(tmp_path: Path):
    require_full_online()
    created: list[int] = []
    client = full_device_client(tmp_path, "full_duplicate_sha")

    try:
        first_path = tmp_path / f"{unique_name('duplicate_a')}.char"
        second_path = tmp_path / f"{unique_name('duplicate_b')}.char"
        marker = unique_name("same_payload")
        write_payload(first_path, 72 * 1024, marker)
        second_path.write_bytes(first_path.read_bytes())

        uploaded = client.upload_resource(unique_name("duplicate_a"), str(first_path), "character_pack")
        created.append(int(uploaded["id"]))

        duplicate = client.upload_resource(unique_name("duplicate_b"), str(second_path), "character_pack")
        assert duplicate["duplicate"] is True
        assert int(duplicate["id"]) == int(uploaded["id"])
        assert duplicate["url"] == uploaded["url"]
    finally:
        cleanup_resources(client, created)


def test_online_full_registered_user_can_create_api_key_and_upload(tmp_path: Path):
    require_full_online()
    created: list[int] = []
    key_id = 0
    password = "CodexOnlineFull123!"
    email = f"codex-full-key-{uuid.uuid4().hex[:12]}@example.com"
    guest = full_device_client(tmp_path, "full_key_guest")
    device_id = guest.device_auth.device_id

    try:
        register_user_from_device(guest, email=email, password=password)
        registered = login_client(email, password, device_id=device_id)
        key_id, api_key = create_api_key(registered)
        api_key_client = ShinsekaiUploadClient(api_key, base_url=BASE_URL, parallel_uploads=5)
        uploaded = upload_char(api_key_client, tmp_path, "full_key_upload", created)

        assert uploaded["id"] in {item["id"] for item in fetch_my_uploads(registered)}
        edited = api_key_client.edit_resource(
            int(uploaded["id"]),
            description="online full test: api key edited resource",
            verified_models=["Qwen"],
            resource_type="character_pack",
        )
        assert edited["id"] == uploaded["id"]

        deleted = api_key_client.delete_resource(int(uploaded["id"]))
        assert deleted["status"] == "deleted"
        created.remove(int(uploaded["id"]))
        assert uploaded["id"] not in upload_ids(registered)
    finally:
        if key_id:
            revoke_api_key(registered, key_id)
        cleanup_resources(registered if "registered" in locals() else guest, created)


def test_online_full_device_tts_is_forbidden(tmp_path: Path):
    require_full_online()
    guest = full_device_client(tmp_path, "full_tts_guest")
    assert guest.api_key

    resp = requests.post(
        f"{BASE_URL}/v1/t2a_v2",
        headers={"Authorization": f"Bearer {guest.api_key}", "Content-Type": "application/json"},
        json={"text": "online full test", "model": "minimax-speech-2.8-turbo"},
        timeout=60,
    )

    assert resp.status_code == 403
