from __future__ import annotations

import os
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
    return ShinsekaiUploadClient.from_device_file(
        str(tmp_path / f"{prefix}_device_id.txt"),
        bind_code=bind_code,
        base_url=BASE_URL,
        parallel_uploads=parallel_uploads,
    )


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


def test_online_full_q1_browser_guest_upload_then_exe_prebind_syncs(tmp_path: Path):
    require_full_online()
    created: list[int] = []
    browser = full_device_client(tmp_path, "full_q1_browser")

    try:
        browser_file = upload_char(browser, tmp_path, "full_q1_browser", created)
        exe = full_device_client(tmp_path, "full_q1_exe", bind_code=browser.bind_code)
        exe_file = upload_char(exe, tmp_path, "full_q1_exe", created)
        ids = {item["id"] for item in fetch_my_uploads(browser)}

        assert exe.bind_code == browser.bind_code
        assert browser_file["id"] in ids
        assert exe_file["id"] in ids
    finally:
        cleanup_resources(browser, created)


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


def test_online_full_q3_register_keeps_bind_code_and_guest_token_uploads(tmp_path: Path):
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
        second = upload_char(guest, tmp_path, "full_q3_after_register_guest_token", created)
        ids = {item["id"] for item in fetch_my_uploads(registered)}

        assert after_me["role"] == "user"
        assert after_me["bind_code"] == before_bind
        assert first["id"] in ids
        assert second["id"] in ids
    finally:
        cleanup_resources(guest, created)


def test_online_full_q4_exe_first_upload_then_web_prebind_syncs(tmp_path: Path):
    require_full_online()
    created: list[int] = []
    exe = full_device_client(tmp_path, "full_q4_exe")

    try:
        uploaded = upload_char(exe, tmp_path, "full_q4_exe", created)
        web = full_device_client(tmp_path, "full_q4_web", bind_code=exe.bind_code)
        ids = {item["id"] for item in fetch_my_uploads(web)}

        assert web.bind_code == exe.bind_code
        assert uploaded["id"] in ids
    finally:
        cleanup_resources(exe, created)


def test_online_full_q4_existing_browser_claims_exe_bind_code(tmp_path: Path):
    require_full_online()
    browser_created: list[int] = []
    exe_created: list[int] = []
    browser = full_device_client(tmp_path, "full_q4b_browser")
    exe = full_device_client(tmp_path, "full_q4b_exe")

    try:
        browser_file = upload_char(browser, tmp_path, "full_q4b_browser", browser_created)
        exe_file = upload_char(exe, tmp_path, "full_q4b_exe", exe_created)
        browser.claim_bind_code(exe.bind_code)
        ids = {item["id"] for item in fetch_my_uploads(browser)}

        assert browser_file["id"] in ids
        assert exe_file["id"] in ids
    finally:
        cleanup_resources(browser, browser_created)
        cleanup_resources(exe, exe_created)


def test_online_full_q5_bind_code_stable_after_register_and_third_prebind(tmp_path: Path):
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
    assert third.bind_code == before_bind


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


def test_online_full_repeated_prebind_same_device_is_idempotent(tmp_path: Path):
    require_full_online()
    master = full_device_client(tmp_path, "full_repeat_master")
    first = full_device_client(tmp_path, "full_repeat_slave", bind_code=master.bind_code)
    second = full_device_client(tmp_path, "full_repeat_slave", bind_code=master.bind_code)

    assert first.bind_code == master.bind_code
    assert second.bind_code == master.bind_code


def test_online_full_claim_self_rejected_and_already_claimed_by_other_returns_current_identity(tmp_path: Path):
    require_full_online()
    master_a_created: list[int] = []
    guest_created: list[int] = []
    master_a = full_device_client(tmp_path, "full_claim_master_a")
    master_b = full_device_client(tmp_path, "full_claim_master_b")
    guest = full_device_client(tmp_path, "full_claim_guest")

    try:
        master_a_file = upload_char(master_a, tmp_path, "full_claim_master_a", master_a_created)
        guest_file = upload_char(guest, tmp_path, "full_claim_guest", guest_created)

        with pytest.raises(ShinsekaiUploadError):
            master_a.claim_bind_code(master_a.bind_code)

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


def test_online_full_registered_bind_claim_rejected_but_prebind_uploads_sync(tmp_path: Path):
    require_full_online()
    created: list[int] = []
    password = "CodexOnlineFull123!"
    email = f"codex-full-registered-bind-{uuid.uuid4().hex[:12]}@example.com"
    guest = full_device_client(tmp_path, "full_registered_bind_guest")
    before_bind = guest.bind_code
    device_id = guest.device_auth.device_id

    try:
        register_user_from_device(guest, email=email, password=password)
        registered = login_client(email, password, device_id=device_id)
        outsider = full_device_client(tmp_path, "full_registered_bind_outsider")

        with pytest.raises(ShinsekaiUploadError):
            outsider.claim_bind_code(before_bind)

        third = full_device_client(tmp_path, "full_registered_bind_third", bind_code=before_bind)
        uploaded = upload_char(third, tmp_path, "full_registered_bind_third", created)

        assert third.bind_code == before_bind
        assert int(uploaded["id"]) in upload_ids(registered)
    finally:
        cleanup_resources(registered if "registered" in locals() else guest, created)


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
