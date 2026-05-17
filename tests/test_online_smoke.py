from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest
import requests

from shinsekai_upload_client import DEFAULT_PART_SIZE, ShinsekaiUploadClient


pytestmark = pytest.mark.online

BASE_URL = os.getenv("SHINSEKAI_BASE_URL", "https://api.end0rph1n.icu").rstrip("/")
WEB_URL = os.getenv("SHINSEKAI_WEB_URL", "https://shinsekai.end0rph1n.icu").rstrip("/")


def require_online() -> None:
    if os.getenv("SHINSEKAI_ONLINE_TEST") != "1":
        pytest.skip("set SHINSEKAI_ONLINE_TEST=1 to run live online smoke tests")


def unique_name(prefix: str) -> str:
    return f"codex_{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def write_payload(path: Path, size: int, marker: str) -> None:
    # 每个线上用例都写入唯一 marker，避免 SHA-256 去重命中旧测试文件。
    chunk = (f"{marker}\n".encode("utf-8") * 4096) or b"x"
    remaining = size
    with path.open("wb") as f:
        while remaining > 0:
            piece = chunk[: min(len(chunk), remaining)]
            f.write(piece)
            remaining -= len(piece)


def auth_headers(client: ShinsekaiUploadClient) -> dict[str, str]:
    token = client.api_key or client.access_token
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def device_client(
    tmp_path: Path,
    prefix: str,
    *,
    bind_code: str | None = None,
    parallel_uploads: int = 5,
) -> ShinsekaiUploadClient:
    fingerprint = f"{prefix}|ANGLE RTX 4070|16|Win32|-540|{uuid.uuid4().hex}"
    return ShinsekaiUploadClient.from_device_file(
        str(tmp_path / f"{prefix}_device_id.txt"),
        fingerprint=fingerprint,
        bind_code=bind_code,
        base_url=BASE_URL,
        parallel_uploads=parallel_uploads,
    )


def api_key_client() -> ShinsekaiUploadClient:
    api_key = os.getenv("SHINSEKAI_API_KEY", "").strip()
    if not api_key:
        pytest.skip("set SHINSEKAI_API_KEY to run API-key upload smoke test")
    # API Key 会进入 HTTP Authorization header；占位文本或非 ASCII 值不应触发真实请求。
    if any(ord(ch) > 127 for ch in api_key) or "your" in api_key.lower() or "..." in api_key:
        pytest.skip("set a real ASCII SHINSEKAI_API_KEY to run API-key upload smoke test")
    return ShinsekaiUploadClient(api_key, base_url=BASE_URL, parallel_uploads=5)


def fetch_my_uploads(client: ShinsekaiUploadClient) -> list[dict]:
    resp = requests.get(f"{BASE_URL}/api/my-uploads", headers=auth_headers(client), timeout=60)
    assert resp.ok, resp.text
    data = resp.json()
    assert isinstance(data, list)
    return data


def cleanup_resources(client: ShinsekaiUploadClient, resource_ids: list[int]) -> None:
    for resource_id in resource_ids:
        try:
            requests.delete(f"{BASE_URL}/api/resources/{resource_id}", headers=auth_headers(client), timeout=60)
        except requests.RequestException:
            pass


def test_online_device_bind_code_stable_across_reauth(tmp_path: Path):
    require_online()

    client1 = device_client(tmp_path, "stable")
    client2 = ShinsekaiUploadClient.from_device_file(
        str(tmp_path / "stable_device_id.txt"),
        fingerprint="stable|ANGLE RTX 4070|16|Win32|-540",
        base_url=BASE_URL,
    )

    assert len(client1.bind_code) == 6
    assert client2.bind_code == client1.bind_code
    assert client2.access_token


def test_online_device_character_upload_bind_url_and_my_uploads(tmp_path: Path):
    require_online()
    created: list[int] = []
    client = device_client(tmp_path, "device_char")

    try:
        file_path = tmp_path / f"{unique_name('char')}.char"
        write_payload(file_path, 96 * 1024, file_path.stem)
        result = client.upload_resource(
            unique_name("device_char"),
            str(file_path),
            "character_pack",
            uploader="codex-online",
            description="online smoke: device character upload",
            verified_models=["GPT-Sovits"],
        )
        created.append(int(result["id"]))

        url = client.community_bind_url(web_url=WEB_URL)
        uploads = fetch_my_uploads(client)

        assert f"bind={client.bind_code}" in url
        assert any(item["id"] == result["id"] for item in uploads)
        assert any("GPT-Sovits" in item.get("models", []) for item in uploads if item["id"] == result["id"])
    finally:
        cleanup_resources(client, created)


def test_online_device_background_upload(tmp_path: Path):
    require_online()
    created: list[int] = []
    client = device_client(tmp_path, "device_bg")

    try:
        file_path = tmp_path / f"{unique_name('background')}.bg"
        write_payload(file_path, 64 * 1024, file_path.stem)
        result = client.upload_resource(
            unique_name("device_bg"),
            str(file_path),
            "background_pack",
            uploader="codex-online",
            description="online smoke: device background upload",
        )
        created.append(int(result["id"]))

        uploads = fetch_my_uploads(client)
        assert any(item["id"] == result["id"] and item["type"] == "background" for item in uploads)
    finally:
        cleanup_resources(client, created)


def test_online_sdk_resource_management_owner_roundtrip(tmp_path: Path):
    require_online()
    created: list[int] = []
    client = device_client(tmp_path, "resource_manage")

    try:
        file_path = tmp_path / f"{unique_name('manage')}.char"
        write_payload(file_path, 80 * 1024, file_path.stem)
        result = client.upload_resource(
            unique_name("resource_manage"),
            str(file_path),
            "character_pack",
            uploader="codex-online",
            description="online smoke: resource management before edit",
            verified_models=["GPT-Sovits"],
        )
        resource_id = int(result["id"])
        created.append(resource_id)

        assert any(item["id"] == resource_id for item in client.list_my_uploads())
        edited = client.edit_resource(
            resource_id,
            description="online smoke: resource management after edit",
            tags=["codex-online", "owner-edit"],
            verified_models=["Qwen"],
            resource_type="character_pack",
        )
        assert edited["id"] == resource_id

        uploads = client.list_my_uploads()
        edited_item = next(item for item in uploads if item["id"] == resource_id)
        assert edited_item["description"] == "online smoke: resource management after edit"
        assert "owner-edit" in edited_item.get("tags", [])
        assert "Qwen" in edited_item.get("models", [])

        deleted = client.delete_resource(resource_id)
        assert deleted["status"] == "deleted"
        created.remove(resource_id)
        assert resource_id not in {item["id"] for item in client.list_my_uploads()}
    finally:
        cleanup_resources(client, created)


def test_online_prebind_second_device_syncs_uploads(tmp_path: Path):
    require_online()
    created: list[int] = []
    master = device_client(tmp_path, "prebind_master")

    try:
        first_file = tmp_path / f"{unique_name('master')}.char"
        write_payload(first_file, 80 * 1024, first_file.stem)
        first = master.upload_resource(unique_name("master"), str(first_file), "character_pack")
        created.append(int(first["id"]))

        slave = device_client(tmp_path, "prebind_slave", bind_code=master.bind_code)
        second_file = tmp_path / f"{unique_name('slave')}.char"
        write_payload(second_file, 80 * 1024, second_file.stem)
        second = slave.upload_resource(unique_name("slave"), str(second_file), "character_pack")
        created.append(int(second["id"]))

        uploads = fetch_my_uploads(master)
        ids = {item["id"] for item in uploads}

        assert slave.bind_code == master.bind_code
        assert first["id"] in ids
        assert second["id"] in ids
    finally:
        cleanup_resources(master, created)


def test_online_claim_guest_bind_code_syncs_uploads(tmp_path: Path):
    require_online()
    current_created: list[int] = []
    guest_created: list[int] = []
    current = device_client(tmp_path, "claim_current")
    guest = device_client(tmp_path, "claim_guest")

    try:
        current_file = tmp_path / f"{unique_name('current')}.char"
        write_payload(current_file, 80 * 1024, current_file.stem)
        current_result = current.upload_resource(unique_name("current"), str(current_file), "character_pack")
        current_created.append(int(current_result["id"]))

        guest_file = tmp_path / f"{unique_name('guest')}.char"
        write_payload(guest_file, 80 * 1024, guest_file.stem)
        guest_result = guest.upload_resource(unique_name("guest"), str(guest_file), "character_pack")
        guest_created.append(int(guest_result["id"]))

        current.claim_bind_code(guest.bind_code)
        uploads = fetch_my_uploads(current)
        ids = {item["id"] for item in uploads}

        assert current_result["id"] in ids
        assert guest_result["id"] in ids
    finally:
        cleanup_resources(current, current_created)
        cleanup_resources(guest, guest_created)


def test_online_api_key_character_upload(tmp_path: Path):
    require_online()
    client = api_key_client()
    created: list[int] = []

    try:
        file_path = tmp_path / f"{unique_name('apikey')}.char"
        write_payload(file_path, 96 * 1024, file_path.stem)
        result = client.upload_resource(
            unique_name("apikey"),
            str(file_path),
            "character_pack",
            uploader="codex-online",
            description="online smoke: api key character upload",
            verified_models=["Qwen"],
        )
        created.append(int(result["id"]))

        uploads = fetch_my_uploads(client)
        assert any(item["id"] == result["id"] for item in uploads)
    finally:
        cleanup_resources(client, created)


def test_online_large_parallel_upload(tmp_path: Path):
    require_online()
    if os.getenv("SHINSEKAI_ONLINE_LARGE") != "1":
        pytest.skip("set SHINSEKAI_ONLINE_LARGE=1 to upload a >20MB multipart file")

    created: list[int] = []
    client = device_client(tmp_path, "large_parallel", parallel_uploads=5)

    try:
        file_path = tmp_path / f"{unique_name('large')}.char"
        write_payload(file_path, DEFAULT_PART_SIZE + 256 * 1024, file_path.stem)
        result = client.upload_resource(
            unique_name("large_parallel"),
            str(file_path),
            "character_pack",
            uploader="codex-online",
            description="online smoke: large parallel multipart upload",
            parallel_uploads=5,
        )
        created.append(int(result["id"]))

        uploads = fetch_my_uploads(client)
        assert any(item["id"] == result["id"] for item in uploads)
    finally:
        cleanup_resources(client, created)
