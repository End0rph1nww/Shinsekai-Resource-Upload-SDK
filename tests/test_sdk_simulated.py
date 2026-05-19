from __future__ import annotations

import hashlib
import importlib
import uuid

import pytest

import shinsekai_upload_client as sdk
from shinsekai_upload_client import ShinsekaiUploadClient, ShinsekaiUploadError, UploadProgress


class FakeResponse:
    def __init__(self, data=None, *, ok=True, status_code=200, headers=None, text=""):
        self._data = data
        self.ok = ok
        self.status_code = status_code
        self.headers = {"content-type": "application/json"} if headers is None else headers
        self.text = text

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return {} if self._data is None else self._data


class FakeServerSession:
    """Fake server endpoints used by the SDK; no online data is touched."""

    def __init__(
        self,
        *,
        part_size=5,
        done_parts=None,
        fail=None,
        device_api_key="sk-sn-device",
        device_access_token="jwt-device",
        prebind_api_key="sk-sn-master",
        prebind_access_token="jwt-master",
        duplicate_start=False,
        duplicate_complete=False,
        device_access_tokens=None,
        reject_tokens=None,
    ):
        self.part_size = part_size
        self.done_parts = done_parts or []
        self.fail = fail or {}
        self.device_api_key = device_api_key
        self.device_access_token = device_access_token
        self.prebind_api_key = prebind_api_key
        self.prebind_access_token = prebind_access_token
        self.duplicate_start = duplicate_start
        self.duplicate_complete = duplicate_complete
        self.device_access_tokens = list(device_access_tokens or [])
        self.reject_tokens = set(reject_tokens or [])
        self.auth_device_calls = 0
        self.posts = []
        self.gets = []
        self.deletes = []
        self.patches = []
        self.puts = []
        self.complete_payload = None
        self.presigned_numbers = []
        self.reported_parts = []
        self.started_payload = None

    def _maybe_fail(self, key):
        if key in self.fail:
            value = self.fail[key]
            if isinstance(value, Exception):
                raise value
            return value
        return None

    def post(self, url, headers=None, json=None, timeout=None):
        headers = headers or {}
        payload = json or {}
        self.posts.append((url, headers, payload))

        failed = self._maybe_fail(("post", url.rsplit("/", 1)[-1]))
        if failed:
            return failed
        rejected = self._maybe_reject_auth(headers)
        if rejected:
            return rejected

        if url.endswith("/auth/device"):
            self._assert_device_auth_payload(payload)
            is_prebound = payload.get("bind_code") == "A1B2C3"
            access_token = self.prebind_access_token if is_prebound else self._next_device_access_token()
            return FakeResponse({
                "access_token": access_token,
                "api_key": self.prebind_api_key if is_prebound else self.device_api_key,
                "public_id": "pub-master" if is_prebound else "pub-device",
                "bind_code": "A1B2C3" if is_prebound else "EXE123",
                "is_guest": False if is_prebound else True,
                "refresh_token": "refresh-token",
            })

        if url.endswith("/auth/device/claim"):
            self._assert_auth_header(headers)
            self.assert_payload_code(payload, "GUEST1")
            return FakeResponse({
                "access_token": "jwt-current",
                "api_key": "",
                "public_id": "pub-current",
                "bind_code": "CUR123",
                "is_guest": False,
            })

        if url.endswith("/auth/device/merge"):
            self.assert_payload_code(payload, "WEB999")
            if not payload.get("device_id"):
                raise AssertionError("merge requires device_id")
            return FakeResponse({
                "access_token": "jwt-merged",
                "api_key": "sk-sn-merged",
                "public_id": "pub-merged",
                "bind_code": "WEB999",
                "is_guest": False,
            })

        if url.endswith("/api/resources/multipart/start"):
            self._assert_auth_header(headers)
            self.started_payload = payload
            if self.duplicate_start:
                return FakeResponse({
                    "upload_id": "",
                    "key": "",
                    "part_size": 0,
                    "total_parts": 0,
                    "duplicate": True,
                    "existing_id": 101,
                    "public_url": "https://cdn.invalid/existing",
                })
            total_parts = (payload["total_size"] + self.part_size - 1) // self.part_size
            return FakeResponse({
                "key": "obj-key",
                "upload_id": "upload-id",
                "total_parts": total_parts,
                "part_size": self.part_size,
                "parts_done": self.done_parts,
            })

        if url.endswith("/api/resources/multipart/presign"):
            self.presigned_numbers.append(payload["part_number"])
            return FakeResponse({"presigned_url": f"https://r2.invalid/part-{payload['part_number']}"})

        if url.endswith("/api/resources/multipart/report-part"):
            if not payload.get("etag"):
                raise AssertionError("report-part requires etag")
            self.reported_parts.append(payload["part_number"])
            return FakeResponse({"ok": True})

        if url.endswith("/api/resources/multipart/complete"):
            self.complete_payload = payload
            part_numbers = [part["PartNumber"] for part in payload["parts"]]
            if part_numbers != sorted(part_numbers):
                raise AssertionError("parts must be ordered before complete")
            if self.duplicate_complete:
                return FakeResponse({
                    "duplicate": True,
                    "existing_id": 202,
                    "public_url": "https://cdn.invalid/existing-from-complete",
                })
            return FakeResponse({
                "id": 101,
                "url": "https://cdn.invalid/resource",
                "parts": payload["parts"],
                "uploader": payload["uploader"],
            })

        raise AssertionError(f"unhandled POST {url}")

    def get(self, url, headers=None, timeout=None):
        self.gets.append((url, headers or {}))
        rejected = self._maybe_reject_auth(headers or {})
        if rejected:
            return rejected
        self._assert_auth_header(headers or {})
        if url.endswith("/api/tags"):
            return FakeResponse(["剧情向", "中文"])
        if url.endswith("/api/my-uploads"):
            return FakeResponse([{"id": 101, "name": "role", "type": "character"}])
        return FakeResponse([{"id": 7, "filename": "pending.char"}])

    def delete(self, url, headers=None, timeout=None):
        self.deletes.append((url, headers or {}))
        rejected = self._maybe_reject_auth(headers or {})
        if rejected:
            return rejected
        self._assert_auth_header(headers or {})
        if url.endswith("/api/resources/multipart/pending/7"):
            return FakeResponse({"ok": True, "deleted": 7})
        if url.endswith("/api/resources/101"):
            return FakeResponse({"status": "deleted", "id": 101})
        if not url.endswith("/api/resources/multipart/pending/7"):
            raise AssertionError(f"unexpected delete url {url}")

    def patch(self, url, headers=None, json=None, timeout=None):
        self.patches.append((url, headers or {}, json or {}))
        rejected = self._maybe_reject_auth(headers or {})
        if rejected:
            return rejected
        self._assert_auth_header(headers or {})
        if not url.endswith("/api/resources/101"):
            raise AssertionError(f"unexpected patch url {url}")
        return FakeResponse({
            "id": 101,
            "name": (json or {}).get("name", "role"),
            "description": (json or {}).get("description", ""),
            "tags": (json or {}).get("tags", []),
        })

    def put(self, url, data=None, headers=None, timeout=None):
        failed = self._maybe_fail(("put", "session"))
        if failed:
            return failed
        self.puts.append((url, len(data or b""), headers or {}))
        return FakeResponse({}, headers={"ETag": f"etag-session-{len(self.puts)}"})

    @staticmethod
    def _assert_auth_header(headers):
        if not headers.get("Authorization", "").startswith("Bearer "):
            raise AssertionError("missing bearer auth")

    def _next_device_access_token(self):
        if self.device_access_tokens:
            index = min(self.auth_device_calls, len(self.device_access_tokens) - 1)
            token = self.device_access_tokens[index]
        else:
            token = self.device_access_token
        self.auth_device_calls += 1
        return token

    def _maybe_reject_auth(self, headers):
        auth = headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth.removeprefix("Bearer ").strip()
        if token in self.reject_tokens:
            return FakeResponse({"detail": "expired token"}, ok=False, status_code=401)
        return None

    @staticmethod
    def _assert_device_auth_payload(payload):
        if not payload.get("device_id"):
            raise AssertionError("device_id is required")
        if "fingerprint" in payload:
            raise AssertionError("SDK should not send fingerprint in the simplified device auth flow")

    @staticmethod
    def assert_payload_code(payload, expected):
        if payload.get("bind_code") != expected:
            raise AssertionError(f"expected bind_code {expected}, got {payload.get('bind_code')}")


def make_file(path, data: bytes) -> None:
    path.write_bytes(data)


def test_validation_and_helpers():
    with pytest.raises(ValueError):
        ShinsekaiUploadClient("")
    with pytest.raises(ValueError):
        ShinsekaiUploadClient("sk", parallel_uploads=0)
    with pytest.raises(FileNotFoundError):
        ShinsekaiUploadClient("sk").upload_resource("n", __file__ + ".missing", "character_pack")

    assert ShinsekaiUploadClient.normalize_bind_code(" ab12cd ") == "AB12CD"
    assert ShinsekaiUploadClient("sk").bind_code == ""
    assert (
        ShinsekaiUploadClient.build_bind_url(" ab12cd ", web_url="https://web.test", path="/resources?tab=mine")
        == "https://web.test/resources?tab=mine&bind=AB12CD"
    )
    with pytest.raises(ValueError):
        ShinsekaiUploadClient.build_bind_url("")
    with pytest.raises(ValueError, match="64"):
        ShinsekaiUploadClient.authenticate_device(
            device_id="x" * 65,
            base_url="https://api.test",
            session=FakeServerSession(),
        )
    assert UploadProgress(stage="x", message="m", uploaded_bytes=5, total_bytes=10).percent == 50.0
    assert UploadProgress(stage="x", message="m").percent == 0.0


def test_device_id_file_create_and_reuse(tmp_path):
    path = tmp_path / "nested" / "device.txt"
    first = ShinsekaiUploadClient.load_or_create_device_id(str(path))
    uuid.UUID(first)
    assert ShinsekaiUploadClient.load_or_create_device_id(str(path)) == first
    path.write_text("manual-device", encoding="utf-8")
    assert ShinsekaiUploadClient.load_or_create_device_id(str(path)) == "manual-device"


def test_device_auth_from_device_and_file(tmp_path):
    fake = FakeServerSession()
    client = ShinsekaiUploadClient.from_device(
        device_id=" device-1 ",
        base_url="https://api.test/",
        parallel_uploads=2,
        session=fake,
    )
    assert client.api_key == "sk-sn-device"
    assert client.device_auth.device_id == "device-1"
    assert client.bind_code == "EXE123"
    assert client.community_bind_url(web_url="https://web.test") == "https://web.test/resources?bind=EXE123"
    assert client.parallel_uploads == 2
    assert fake.posts[0][2] == {"device_id": "device-1"}

    path = tmp_path / "device.txt"
    client2 = ShinsekaiUploadClient.from_device_file(str(path), base_url="https://api.test", session=FakeServerSession())
    assert path.exists()
    assert client2.bind_code == "EXE123"


def test_device_auth_without_api_key_uses_access_token_for_upload(tmp_path):
    fake = FakeServerSession(part_size=8, device_api_key="")
    path = tmp_path / "sample.char"
    make_file(path, b"0123456789")

    client = ShinsekaiUploadClient.from_device(
        device_id="device-1",
        base_url="https://api.test",
        session=fake,
    )
    client.upload_resource("role", str(path), "character_pack")

    assert client.api_key == ""
    assert client.access_token == "jwt-device"
    start_post = next(item for item in fake.posts if item[0].endswith("/api/resources/multipart/start"))
    assert start_post[1]["Authorization"] == "Bearer jwt-device"


def test_device_auth_prefers_access_token_over_rotating_device_key(tmp_path):
    fake = FakeServerSession(part_size=8)
    path = tmp_path / "sample.char"
    make_file(path, b"0123456789")

    client = ShinsekaiUploadClient.from_device(
        device_id="device-1",
        base_url="https://api.test",
        session=fake,
    )
    client.upload_resource("role", str(path), "character_pack")

    assert client.api_key == "sk-sn-device"
    assert client.access_token == "jwt-device"
    start_post = next(item for item in fake.posts if item[0].endswith("/api/resources/multipart/start"))
    assert start_post[1]["Authorization"] == "Bearer jwt-device"


def test_device_auth_refreshes_access_token_once_after_401():
    fake = FakeServerSession(
        device_access_tokens=["jwt-expired", "jwt-fresh"],
        reject_tokens={"jwt-expired"},
    )
    client = ShinsekaiUploadClient.from_device(
        device_id="device-1",
        base_url="https://api.test",
        session=fake,
    )

    uploads = client.list_my_uploads()

    assert uploads[0]["id"] == 101
    assert client.access_token == "jwt-fresh"
    assert fake.auth_device_calls == 2
    assert fake.gets[0][1]["Authorization"] == "Bearer jwt-expired"
    assert fake.gets[1][1]["Authorization"] == "Bearer jwt-fresh"


def test_device_auth_prebind(tmp_path):
    fake = FakeServerSession()
    client = ShinsekaiUploadClient.from_device_file(
        str(tmp_path / "device.txt"),
        bind_code=" a1b2c3 ",
        base_url="https://api.test",
        session=fake,
    )
    assert fake.posts[0][2]["bind_code"] == "A1B2C3"
    assert "fingerprint" not in fake.posts[0][2]
    assert client.api_key == "sk-sn-master"
    assert client.bind_code == "A1B2C3"
    assert client.device_auth.is_guest is False


def test_claim_bind_code():
    client = ShinsekaiUploadClient.from_device(
        device_id="device-1",
        base_url="https://api.test",
        session=FakeServerSession(),
    )
    before_key = client.api_key
    auth = client.claim_bind_code(" guest1 ")
    assert auth.api_key == before_key
    assert auth.access_token == "jwt-current"
    assert client.api_key == before_key
    assert client.access_token == "jwt-current"
    assert client.bind_code == "CUR123"
    assert client.device_auth.public_id == "pub-current"


def test_merge_compatibility():
    fake = FakeServerSession()
    auth = ShinsekaiUploadClient.merge_device(
        bind_code=" web999 ",
        device_id="device-1",
        base_url="https://api.test",
        session=fake,
    )
    assert auth.api_key == "sk-sn-merged"
    assert fake.posts[-1][2] == {"bind_code": "WEB999", "device_id": "device-1"}

    client = ShinsekaiUploadClient.from_device(device_id="device-1", base_url="https://api.test", session=FakeServerSession())
    auth2 = client.merge_with_bind_code("web999")
    assert auth2.public_id == "pub-merged"
    assert client.api_key == "sk-sn-merged"
    assert client.bind_code == "WEB999"


def test_sequential_upload_full_chain(tmp_path):
    fake = FakeServerSession(part_size=4)
    progress = []
    path = tmp_path / "sample.char"
    data = b"0123456789"
    make_file(path, data)

    client = ShinsekaiUploadClient("sk-sn-old", base_url="https://api.test", parallel_uploads=1, session=fake)
    result = client.upload_resource(
        "role",
        str(path),
        "character_pack",
        uploader="author",
        description="desc",
        progress=progress.append,
    )

    assert result["url"] == "https://cdn.invalid/resource"
    assert [item[1] for item in fake.puts] == [4, 4, 2]
    assert fake.presigned_numbers == [1, 2, 3]
    assert fake.reported_parts == [1, 2, 3]
    assert fake.started_payload["filename"] == "sample.char"
    assert fake.started_payload["sha256"] == hashlib.sha256(data).hexdigest()
    assert fake.complete_payload["uploader"] == "author"
    assert "bind_code" not in fake.started_payload
    assert "bind_code" not in fake.complete_payload
    assert progress[0].stage == "hashing"
    assert progress[-1].stage == "done"


def test_upload_returns_existing_resource_when_start_reports_duplicate(tmp_path):
    fake = FakeServerSession(duplicate_start=True)
    progress = []
    path = tmp_path / "sample.char"
    make_file(path, b"0123456789")

    client = ShinsekaiUploadClient("sk-sn-old", base_url="https://api.test", session=fake)
    result = client.upload_resource("role", str(path), "character_pack", progress=progress.append)

    assert result["duplicate"] is True
    assert result["id"] == 101
    assert result["url"] == "https://cdn.invalid/existing"
    assert fake.puts == []
    assert fake.complete_payload is None
    assert progress[-1].stage == "done"


def test_upload_returns_existing_resource_when_complete_reports_duplicate(tmp_path):
    fake = FakeServerSession(part_size=8, duplicate_complete=True)
    progress = []
    path = tmp_path / "sample.char"
    make_file(path, b"0123456789")

    client = ShinsekaiUploadClient("sk-sn-old", base_url="https://api.test", session=fake)
    result = client.upload_resource("role", str(path), "character_pack", progress=progress.append)

    assert result["duplicate"] is True
    assert result["id"] == 202
    assert result["url"] == "https://cdn.invalid/existing-from-complete"
    assert fake.puts
    assert fake.complete_payload is not None
    assert progress[-1].stage == "done"


def test_device_upload_includes_bind_code_metadata(tmp_path):
    fake = FakeServerSession(part_size=8)
    path = tmp_path / "sample.char"
    make_file(path, b"0123456789")
    client = ShinsekaiUploadClient.from_device(
        device_id="device-1",
        base_url="https://api.test",
        session=fake,
    )
    client.upload_resource("role", str(path), "character_pack")

    assert client.bind_code == "EXE123"
    assert fake.started_payload["bind_code"] == "EXE123"
    assert fake.complete_payload["bind_code"] == "EXE123"


def test_character_upload_includes_verified_models(tmp_path):
    fake = FakeServerSession(part_size=8)
    path = tmp_path / "sample.char"
    make_file(path, b"0123456789")
    client = ShinsekaiUploadClient("sk-sn-old", base_url="https://api.test", session=fake)
    client.upload_resource(
        "role",
        str(path),
        "character_pack",
        verified_models=["GPT-Sovits", "Qwen", "GPT-Sovits"],
    )

    assert fake.started_payload["verified_models"] == ["GPT-Sovits", "Qwen"]
    assert fake.complete_payload["verified_models"] == ["GPT-Sovits", "Qwen"]


def test_upload_includes_user_tags_only_on_complete(tmp_path):
    fake = FakeServerSession(part_size=8)
    path = tmp_path / "sample.char"
    make_file(path, b"0123456789")
    client = ShinsekaiUploadClient("sk-sn-old", base_url="https://api.test", session=fake)

    client.upload_resource(
        "role",
        str(path),
        "character_pack",
        tags=["剧情向", " ", "中文", "剧情向", "character_pack", "背景"],
    )

    assert "tags" not in fake.started_payload
    assert fake.complete_payload["tags"] == ["剧情向", "中文"]


def test_background_upload_rejects_verified_models(tmp_path):
    fake = FakeServerSession()
    path = tmp_path / "sample.bg"
    make_file(path, b"0123456789")
    client = ShinsekaiUploadClient("sk-sn-old", base_url="https://api.test", session=fake)

    with pytest.raises(ValueError, match="character_pack"):
        client.upload_resource(
            "background",
            str(path),
            "background_pack",
            verified_models=["GPT-Sovits"],
        )

    assert fake.posts == []


def test_rejects_unknown_verified_model(tmp_path):
    path = tmp_path / "sample.char"
    make_file(path, b"0123456789")
    client = ShinsekaiUploadClient("sk-sn-old", base_url="https://api.test", session=FakeServerSession())

    with pytest.raises(ValueError, match="UnknownModel"):
        client.upload_resource(
            "role",
            str(path),
            "character_pack",
            verified_models=["UnknownModel"],
        )


def test_parallel_upload_full_chain(tmp_path, monkeypatch):
    fake = FakeServerSession(part_size=3)
    global_puts = []

    def fake_global_put(url, data=None, headers=None, timeout=None):
        global_puts.append((url, len(data or b"")))
        return FakeResponse({}, headers={"ETag": f"etag-global-{url.rsplit('-', 1)[-1]}"})

    monkeypatch.setattr(sdk.requests, "put", fake_global_put)

    path = tmp_path / "sample.bg"
    make_file(path, b"abcdefghij")
    client = ShinsekaiUploadClient("sk-sn-old", base_url="https://api.test", parallel_uploads=4, session=fake)
    result = client.upload_resource("background", str(path), "background_pack")

    assert len(global_puts) == 4
    assert len(fake.puts) == 0
    assert [part["PartNumber"] for part in result["parts"]] == [1, 2, 3, 4]
    assert sorted(fake.reported_parts) == [1, 2, 3, 4]


def test_resume_upload_skips_finished_parts(tmp_path):
    fake = FakeServerSession(part_size=4, done_parts=[{"PartNumber": 1, "ETag": "etag-old"}])
    path = tmp_path / "resume.char"
    make_file(path, b"0123456789")
    client = ShinsekaiUploadClient("sk-sn-old", base_url="https://api.test", session=fake)
    result = client.upload_resource("resume", str(path), "character_pack")

    assert fake.presigned_numbers == [2, 3]
    assert [item[1] for item in fake.puts] == [4, 2]
    assert [part["PartNumber"] for part in result["parts"]] == [1, 2, 3]


def test_pending_list_and_delete():
    fake = FakeServerSession()
    client = ShinsekaiUploadClient("sk-sn-old", base_url="https://api.test", session=fake)
    pending = client.list_pending()
    deleted = client.delete_pending(7)

    assert pending[0]["id"] == 7
    assert deleted["deleted"] == 7
    assert fake.gets[0][0].endswith("/api/resources/multipart/pending")
    assert fake.deletes[0][0].endswith("/api/resources/multipart/pending/7")


def test_resource_management_methods():
    fake = FakeServerSession()
    client = ShinsekaiUploadClient("sk-sn-old", base_url="https://api.test", session=fake)

    uploads = client.list_my_uploads()
    tags = client.list_tags()
    edited = client.edit_resource(
        101,
        name="new role",
        description="new desc",
        tags=["tag-a", " ", "tag-b", "tag-a", "character_pack"],
        verified_models=["GPT-Sovits", "Qwen", "GPT-Sovits"],
        resource_type="character_pack",
    )
    deleted = client.delete_resource(101)

    assert uploads[0]["id"] == 101
    assert tags == ["剧情向", "中文"]
    assert edited["name"] == "new role"
    assert deleted == {"status": "deleted", "id": 101}
    assert fake.gets[0][0].endswith("/api/my-uploads")
    assert fake.gets[1][0].endswith("/api/tags")
    assert fake.patches[0][0].endswith("/api/resources/101")
    assert fake.patches[0][2] == {
        "name": "new role",
        "description": "new desc",
        "tags": ["tag-a", "tag-b"],
        "verified_models": ["GPT-Sovits", "Qwen"],
    }
    assert fake.deletes[0][0].endswith("/api/resources/101")

    with pytest.raises(ValueError):
        client.edit_resource(101)
    with pytest.raises(ValueError):
        client.delete_resource(0)
    with pytest.raises(ValueError, match="resource_type"):
        client.edit_resource(101, verified_models=["Qwen"])
    with pytest.raises(ValueError, match="character_pack"):
        client.edit_resource(101, verified_models=["Qwen"], resource_type="background_pack")
    with pytest.raises(ValueError, match="UnknownModel"):
        client.edit_resource(101, verified_models=["UnknownModel"], resource_type="character_pack")


def test_error_paths(tmp_path):
    err = FakeResponse({"detail": "bad bind"}, ok=False, status_code=404)
    with pytest.raises(ShinsekaiUploadError, match="bad bind"):
        ShinsekaiUploadClient._checked_json(err, "merge")

    err2 = FakeResponse(None, ok=False, status_code=500, headers={"content-type": "text/plain"}, text="server exploded")
    with pytest.raises(ShinsekaiUploadError, match="server exploded"):
        ShinsekaiUploadClient._checked_json(err2, "start")

    with pytest.raises(ShinsekaiUploadError, match="JSON"):
        ShinsekaiUploadClient._checked_json(FakeResponse(ValueError("not json"), ok=True), "auth")

    with pytest.raises(ShinsekaiUploadError, match="api_key"):
        ShinsekaiUploadClient._device_auth_from_json({}, "d")
    with pytest.raises(ShinsekaiUploadError, match="api_key 或 access_token"):
        ShinsekaiUploadClient._device_auth_from_json({}, "d", require_api_key=False, require_auth_token=True)

    empty = tmp_path / "empty.char"
    make_file(empty, b"")
    client = ShinsekaiUploadClient("sk", base_url="https://api.test", session=FakeServerSession())
    with pytest.raises(ValueError):
        client.upload_resource("empty", str(empty), "character_pack")

    sample = tmp_path / "sample.char"
    make_file(sample, b"12345")
    put_fail = FakeServerSession(part_size=5, fail={("put", "session"): FakeResponse({}, ok=False, status_code=403)})
    with pytest.raises(ShinsekaiUploadError, match="PUT"):
        ShinsekaiUploadClient("sk", base_url="https://api.test", session=put_fail).upload_resource("bad", str(sample), "character_pack")

    no_etag = FakeServerSession(part_size=5, fail={("put", "session"): FakeResponse({}, headers={})})
    with pytest.raises(ShinsekaiUploadError, match="ETag"):
        ShinsekaiUploadClient("sk", base_url="https://api.test", session=no_etag).upload_resource("bad", str(sample), "character_pack")


def test_upload_apikey_module_import():
    mod = importlib.import_module("upload_apikey")
    assert hasattr(mod, "make_client")
    assert hasattr(mod, "print_progress")
