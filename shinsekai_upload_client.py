#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shinsekai Resource Station 上传 SDK。

默认仍然走旧版 API Key 上传：

    client = ShinsekaiUploadClient("sk-sn-your_key")

EXE 或桌面客户端可以改走设备认证：

    client = ShinsekaiUploadClient.from_device_file("device_id.txt")
    print(client.device_auth.bind_code)

绑定码由服务端为用户生成。客户端只负责展示自己的绑定码，或者把用户输入的
另一个设备绑定码提交给 /auth/device/claim，用来建立跨设备资源管理关系。旧版
/auth/device/merge 仍保留为兼容入口，并沿用迁移语义。

分片上传默认顺序执行。把 ``parallel_uploads`` 设为大于 1 的值即可并行上传分片。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import os
import threading
import time
import uuid
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests


DEFAULT_API = "https://api.example.com"
DEFAULT_WEB = "https://shinsekai.example.com"
DEFAULT_PART_SIZE = 20 * 1024 * 1024
VERIFIED_MODELS = ("GPT-Sovits", "Genie", "MiniMax", "Qwen")
SYSTEM_TAGS = {
    "角色包",
    "背景包",
    "character_pack",
    "background_pack",
    "角色卡",
    "背景",
    "语音",
}


class ShinsekaiUploadError(RuntimeError):
    """上传流程中的 API 或 R2 步骤失败时抛出。"""


@dataclass(frozen=True)
class DeviceAuthInfo:
    """设备认证结果。bind_code 是服务端用户绑定码，不是本地生成的文件码。"""

    access_token: str
    api_key: str
    public_id: str
    bind_code: str = ""
    is_guest: bool = True
    device_id: str = ""
    refresh_token: str = ""


@dataclass(frozen=True)
class UploadProgress:
    """进度回调数据，供 UI 或命令行输出使用。"""

    stage: str
    message: str
    uploaded_bytes: int = 0
    total_bytes: int = 0
    part_number: int = 0
    total_parts: int = 0
    chunk_speed_mbps: float = 0.0
    avg_speed_mbps: float = 0.0

    @property
    def percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(self.uploaded_bytes / self.total_bytes * 100, 100.0)


ProgressCallback = Callable[[UploadProgress], None]


class ShinsekaiUploadClient:
    """API Key 上传客户端，适合桌面端、工具脚本和批处理流程集成。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_API,
        *,
        access_token: str | None = None,
        timeout: int = 60,
        upload_timeout: int = 600,
        parallel_uploads: int = 1,
        session: requests.Session | None = None,
    ):
        api_key = (api_key or "").strip()
        access_token = (access_token or "").strip()
        if not api_key and not access_token:
            raise ValueError("api_key 或 access_token 不能同时为空")
        self.api_key = api_key
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.upload_timeout = upload_timeout
        self.parallel_uploads = self._validate_parallel_uploads(parallel_uploads)
        self.session = session or requests.Session()
        self._session_lock = threading.Lock()
        self.device_auth: DeviceAuthInfo | None = None

    @property
    def bind_code(self) -> str:
        """当前设备认证用户的服务端绑定码，给 EXE 界面展示用。"""
        return self.device_auth.bind_code if self.device_auth else ""

    def community_bind_url(self, *, web_url: str = DEFAULT_WEB, path: str = "/resources") -> str:
        """
        生成社区网站自动绑定 URL。

        EXE 在用户点击上传时完成设备认证并拿到 bind_code。上传完成后，客户端可以把
        这个 URL 放到“进入社区/查看资源”按钮上。浏览器打开后，网站会读取 ?bind=，
        自动把当前网页身份和 EXE 身份同步。
        """
        if not self.bind_code:
            raise ValueError("当前客户端没有 bind_code，请先通过 from_device/from_device_file 完成设备认证")
        return self.build_bind_url(self.bind_code, web_url=web_url, path=path)

    @classmethod
    def from_device(
        cls,
        *,
        device_id: str | None = None,
        bind_code: str | None = None,
        base_url: str = DEFAULT_API,
        timeout: int = 60,
        upload_timeout: int = 600,
        parallel_uploads: int = 1,
        session: requests.Session | None = None,
    ) -> "ShinsekaiUploadClient":
        """
        通过 /auth/device 创建设备上传客户端。

        EXE 首次启动应生成一个 UUID 并长期保存。后续启动继续使用同一个
        device_id，服务端会返回同一用户的身份。已有 device-key 时，服务端可能
        不再返回明文 API Key，而是返回 access_token。这里未传入 device_id 时只
        生成一个临时 UUID，适合测试，不适合作为正式客户端的默认行为。

        bind_code 是预绑定入口：用户首次启动 EXE 时如果已经知道网页或其他设备
        的绑定码，就把它一起传给 /auth/device，服务端会直接把这个 device_id
        挂到绑定码所属用户下面，不再先创建独立游客。
        """
        stable_device_id = (device_id or str(uuid.uuid4())).strip()
        http = session or requests.Session()
        auth = cls.authenticate_device(
            device_id=stable_device_id,
            bind_code=bind_code,
            base_url=base_url,
            timeout=timeout,
            session=http,
        )
        client = cls(
            auth.api_key,
            base_url=base_url,
            access_token=auth.access_token,
            timeout=timeout,
            upload_timeout=upload_timeout,
            parallel_uploads=parallel_uploads,
            session=http,
        )
        client.device_auth = auth
        return client

    @classmethod
    def from_device_file(
        cls,
        device_id_path: str,
        *,
        bind_code: str | None = None,
        base_url: str = DEFAULT_API,
        timeout: int = 60,
        upload_timeout: int = 600,
        parallel_uploads: int = 1,
        session: requests.Session | None = None,
    ) -> "ShinsekaiUploadClient":
        """从本地文件读取或创建 device_id，再走设备认证。"""
        device_id = cls.load_or_create_device_id(device_id_path)
        return cls.from_device(
            device_id=device_id,
            bind_code=bind_code,
            base_url=base_url,
            timeout=timeout,
            upload_timeout=upload_timeout,
            parallel_uploads=parallel_uploads,
            session=session,
        )

    @staticmethod
    def load_or_create_device_id(device_id_path: str) -> str:
        """读取本地 device_id；文件不存在或内容为空时生成并写入新的 UUID。"""
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(device_id_path)))
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read().strip()
            if existing:
                return existing

        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        device_id = str(uuid.uuid4())
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(device_id)
        return device_id

    @staticmethod
    def build_bind_url(bind_code: str, *, web_url: str = DEFAULT_WEB, path: str = "/resources") -> str:
        """把服务端绑定码拼进社区 URL 的 ?bind= 参数。"""
        normalized_code = ShinsekaiUploadClient.normalize_bind_code(bind_code)
        if not normalized_code:
            raise ValueError("bind_code 不能为空")
        base = web_url.rstrip("/") + "/"
        target = urljoin(base, path.lstrip("/"))
        parsed = urlparse(target)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["bind"] = normalized_code
        return urlunparse(parsed._replace(query=urlencode(query)))

    @classmethod
    def authenticate_device(
        cls,
        *,
        device_id: str,
        bind_code: str | None = None,
        base_url: str = DEFAULT_API,
        timeout: int = 60,
        session: requests.Session | None = None,
    ) -> DeviceAuthInfo:
        """调用 /auth/device，返回服务端为该设备分配的 access_token/API Key 与绑定码。"""
        device_id = device_id.strip()
        if not device_id:
            raise ValueError("device_id 不能为空")
        if len(device_id) > 64:
            raise ValueError("device_id 长度不能超过 64 个字符")

        payload = {"device_id": device_id}
        normalized_code = cls.normalize_bind_code(bind_code)
        if normalized_code:
            payload["bind_code"] = normalized_code

        http = session or requests.Session()
        resp = http.post(
            f"{base_url.rstrip('/')}/auth/device",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        data = cls._checked_json(resp, "设备认证")
        if not isinstance(data, dict):
            raise ShinsekaiUploadError("设备认证失败：响应格式无效")
        return cls._device_auth_from_json(data, device_id, require_api_key=False, require_auth_token=True)

    def claim_bind_code(self, bind_code: str) -> DeviceAuthInfo:
        """
        用当前身份认领另一个设备或游客的绑定码。

        这对应 /auth/device/claim：当前客户端必须已经有可用 API Key 或 JWT。新版服务端会在
        user_claims 里记录“当前用户可管理目标游客资源”的关系，/api/my-uploads 会合并这些资源；
        原资源归属和 API Key 不迁移。编辑和删除接口会同时校验资源原作者与 user_claims。
        接口通常不会返回新的 API Key，所以 SDK 会保留当前 self.api_key。
        """
        normalized_code = self.normalize_bind_code(bind_code)
        if not normalized_code:
            raise ValueError("bind_code 不能为空")
        data = self._post_json("/auth/device/claim", {"bind_code": normalized_code}, "认领绑定码")
        if not isinstance(data, dict):
            raise ShinsekaiUploadError("认领绑定码失败：响应格式无效")
        device_id = self.device_auth.device_id if self.device_auth else ""
        auth = self._device_auth_from_json(data, device_id, require_api_key=False, require_auth_token=True)
        if auth.api_key:
            self.api_key = auth.api_key
        if auth.access_token:
            self.access_token = auth.access_token
        previous = self.device_auth
        self.device_auth = DeviceAuthInfo(
            access_token=self.access_token,
            api_key=self.api_key,
            public_id=auth.public_id or (previous.public_id if previous else ""),
            bind_code=auth.bind_code or (previous.bind_code if previous else ""),
            is_guest=auth.is_guest,
            device_id=device_id,
            refresh_token=auth.refresh_token or (previous.refresh_token if previous else ""),
        )
        return self.device_auth

    @classmethod
    def merge_device(
        cls,
        *,
        bind_code: str,
        device_id: str,
        base_url: str = DEFAULT_API,
        timeout: int = 60,
        session: requests.Session | None = None,
    ) -> DeviceAuthInfo:
        """
        把当前设备合并到绑定码所属用户。

        典型 EXE 流程：用户在 EXE 输入网页控制台显示的六位绑定码，SDK 把该码和
        本机 device_id 提交到 /auth/device/merge。服务端会迁移资源归属并返回
        合并后用户的 API Key。
        """
        normalized_code = cls.normalize_bind_code(bind_code)
        device_id = device_id.strip()
        if not normalized_code:
            raise ValueError("bind_code 不能为空")
        if not device_id:
            raise ValueError("device_id 不能为空")

        http = session or requests.Session()
        resp = http.post(
            f"{base_url.rstrip('/')}/auth/device/merge",
            headers={"Content-Type": "application/json"},
            json={"bind_code": normalized_code, "device_id": device_id},
            timeout=timeout,
        )
        data = cls._checked_json(resp, "设备绑定合并")
        if not isinstance(data, dict):
            raise ShinsekaiUploadError("设备绑定合并失败：响应格式无效")
        return cls._device_auth_from_json(data, device_id, require_api_key=False, require_auth_token=True)

    def merge_with_bind_code(self, bind_code: str, device_id: str | None = None) -> DeviceAuthInfo:
        """
        用用户输入的绑定码合并当前设备，并把客户端切换到合并后的 API Key。

        如果客户端是通过 from_device 或 from_device_file 创建的，可以省略
        device_id；SDK 会使用当前设备认证信息里的 device_id。

        这对应“EXE 输入网页绑定码”的方向。反过来如果网页输入 EXE 展示的
        绑定码，由网页前端调用同一个 /auth/device/merge，SDK 只需要展示
        client.bind_code 给用户看。
        """
        stable_device_id = (device_id or (self.device_auth.device_id if self.device_auth else "")).strip()
        auth = self.merge_device(
            bind_code=bind_code,
            device_id=stable_device_id,
            base_url=self.base_url,
            timeout=self.timeout,
            session=self.session,
        )
        self.api_key = auth.api_key
        self.access_token = auth.access_token
        self.device_auth = auth
        return auth

    def upload_resource(
        self,
        name: str,
        filepath: str,
        resource_type: str,
        *,
        uploader: str = "",
        description: str = "",
        tags: list[str] | tuple[str, ...] | None = None,
        verified_models: list[str] | tuple[str, ...] | None = None,
        progress: ProgressCallback | None = None,
        parallel_uploads: int | None = None,
    ) -> dict:
        """
        上传一个资源文件。

        resource_type:
          - character_pack: .char
          - background_pack: .bg

        资源归属由服务端根据 API Key/JWT 用户身份决定。uploader 只是展示字段，
        不再承担绑定码或资源归属同步职责。
        """
        normalized_models = self._normalize_verified_models(verified_models, resource_type)
        normalized_tags = self._normalize_tags(tags)
        if not os.path.isfile(filepath):
            raise FileNotFoundError(filepath)

        filename = os.path.basename(filepath)
        file_size = os.path.getsize(filepath)
        if file_size == 0:
            raise ValueError(f"文件为空: {filepath}")
        workers = self.parallel_uploads if parallel_uploads is None else self._validate_parallel_uploads(parallel_uploads)

        self._emit(progress, "hashing", "正在计算 SHA-256", 0, file_size)
        file_hash = self.sha256_file(filepath)
        upload_bind_code = self.bind_code

        # 设备认证模式下把服务端固定绑定码随上传元数据提交，兼容服务端未来的校验或审计；
        # 当前网站源码的资源归属仍以 Bearer 认证身份为准。
        start_payload = {
            "display_name": name,
            "filename": filename,
            "resource_type": resource_type,
            "total_size": file_size,
            "content_type": "application/octet-stream",
            "sha256": file_hash,
        }
        if upload_bind_code:
            start_payload["bind_code"] = upload_bind_code
        if normalized_models:
            start_payload["verified_models"] = normalized_models

        start_data = self._post_json("/api/resources/multipart/start", start_payload, "start")

        key = start_data["key"]
        upload_id = start_data["upload_id"]
        total_parts = int(start_data["total_parts"])
        part_size = int(start_data.get("part_size") or DEFAULT_PART_SIZE)
        parts = self._normalize_parts(start_data.get("parts_done", []))
        uploaded_bytes = sum(self._part_bytes(n, file_size, part_size) for n in parts)

        self._emit(
            progress,
            "started",
            f"上传任务已恢复 {len(parts)}/{total_parts} 个分片" if parts else "上传任务已创建",
            uploaded_bytes,
            file_size,
            total_parts=total_parts,
        )

        upload_started_at = time.time()
        if workers <= 1:
            uploaded_bytes = self._upload_parts_sequential(
                filepath=filepath,
                key=key,
                upload_id=upload_id,
                part_size=part_size,
                total_parts=total_parts,
                file_size=file_size,
                parts=parts,
                uploaded_bytes=uploaded_bytes,
                upload_started_at=upload_started_at,
                progress=progress,
            )
        else:
            uploaded_bytes = self._upload_parts_parallel(
                filepath=filepath,
                key=key,
                upload_id=upload_id,
                part_size=part_size,
                total_parts=total_parts,
                file_size=file_size,
                parts=parts,
                uploaded_bytes=uploaded_bytes,
                upload_started_at=upload_started_at,
                workers=workers,
                progress=progress,
            )

        ordered_parts = [parts[i] for i in sorted(parts)]
        if len(ordered_parts) != total_parts:
            missing = sorted(set(range(1, total_parts + 1)) - set(parts))
            raise ShinsekaiUploadError(f"合并前仍缺少分片: {missing[:10]}")

        self._emit(progress, "completing", "正在合并分片并发布资源", uploaded_bytes, file_size, total_parts=total_parts)
        complete_payload = {
            "key": key,
            "upload_id": upload_id,
            "name": name,
            "resource_type": resource_type,
            "uploader": uploader,
            "description": description,
            "sha256": file_hash,
            "parts": ordered_parts,
        }
        if upload_bind_code:
            complete_payload["bind_code"] = upload_bind_code
        if normalized_tags:
            complete_payload["tags"] = normalized_tags
        if normalized_models:
            complete_payload["verified_models"] = normalized_models

        result = self._post_json("/api/resources/multipart/complete", complete_payload, "complete")

        self._emit(progress, "done", "上传完成", file_size, file_size, total_parts=total_parts)
        return result

    def list_pending(self) -> list[dict]:
        """列出当前 API Key 账号下未完成的上传。"""
        return self._get_json("/api/resources/multipart/pending", "查询未完成上传")

    def delete_pending(self, pending_id: int) -> dict:
        """放弃一个未完成的分片上传。"""
        return self._delete_json(f"/api/resources/multipart/pending/{pending_id}", f"放弃未完成上传 {pending_id}")

    def list_my_uploads(self) -> list[dict]:
        """
        列出当前身份可管理的资源。

        服务端会返回自己上传的资源，以及通过 /auth/device/claim 认领到的资源。
        """
        data = self._get_json("/api/my-uploads", "查询我的资源")
        if not isinstance(data, list):
            raise ShinsekaiUploadError("查询我的资源失败：响应格式无效")
        return data

    def list_tags(self) -> list[str]:
        """
        列出站内已有的用户标签，供 EXE 或批量工具做自动补全。

        对应网站新分支的 GET /api/tags。服务端只返回用户自定义标签，不返回 uploader、time、
        verified_models 等系统字段。
        """
        data = self._get_json("/api/tags", "查询资源标签")
        if not isinstance(data, list):
            raise ShinsekaiUploadError("查询资源标签失败：响应格式无效")
        return [str(tag).strip() for tag in data if str(tag).strip()]

    def delete_resource(self, resource_id: int) -> dict:
        """
        删除当前身份可管理的资源。

        服务端允许资源原上传者删除；如果当前用户 claim 过该资源所属游客身份，也允许删除。
        删除是全局操作，资源会从所有可见列表中消失。
        """
        rid = self._validate_resource_id(resource_id)
        data = self._delete_json(f"/api/resources/{rid}", f"删除资源 {rid}")
        if not isinstance(data, dict):
            raise ShinsekaiUploadError("删除资源失败：响应格式无效")
        return data

    def edit_resource(
        self,
        resource_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        tags: list[str] | tuple[str, ...] | None = None,
        verified_models: list[str] | tuple[str, ...] | None = None,
        resource_type: str | None = None,
    ) -> dict:
        """
        编辑当前身份可管理的资源。

        name、description、tags、verified_models 至少传一个。verified_models 只应给角色包使用；
        因此传 verified_models 时必须同时传 resource_type="character_pack" 或 "character"。
        服务端负责最终资源权限判断。
        """
        rid = self._validate_resource_id(resource_id)
        payload: dict = {}
        if name is not None:
            payload["name"] = str(name).strip()
        if description is not None:
            payload["description"] = str(description).strip()
        if tags is not None:
            payload["tags"] = self._normalize_tags(tags)
        if verified_models is not None:
            normalized_type = self._normalize_resource_type_for_models(resource_type)
            payload["verified_models"] = self._normalize_verified_models(verified_models, normalized_type)
        if not payload:
            raise ValueError("至少需要提供 name、description、tags 或 verified_models 中的一项")
        data = self._patch_json(f"/api/resources/{rid}", payload, f"编辑资源 {rid}")
        if not isinstance(data, dict):
            raise ShinsekaiUploadError("编辑资源失败：响应格式无效")
        return data

    @staticmethod
    def normalize_bind_code(bind_code: str | None) -> str:
        """标准化服务端用户绑定码，便于 EXE 输入框直接传入。"""
        return (bind_code or "").strip().upper()

    @staticmethod
    def _normalize_verified_models(
        verified_models: list[str] | tuple[str, ...] | None,
        resource_type: str,
    ) -> list[str]:
        """标准化角色包已验证模型；背景包不允许携带模型参数。"""
        if not verified_models:
            return []
        if resource_type != "character_pack":
            raise ValueError("verified_models 只支持 character_pack（.char）资源，background_pack 不能传模型参数")
        return ShinsekaiUploadClient._normalize_model_names(verified_models)

    @staticmethod
    def _normalize_model_names(verified_models: list[str] | tuple[str, ...]) -> list[str]:
        """校验并去重服务端支持的已验证模型名称。"""
        allowed = set(VERIFIED_MODELS)
        normalized: list[str] = []
        for model in verified_models:
            value = str(model).strip()
            if not value:
                continue
            if value not in allowed:
                raise ValueError(f"不支持的 verified_models 项: {value}")
            if value not in normalized:
                normalized.append(value)
        return normalized

    @staticmethod
    def _normalize_tags(tags: list[str] | tuple[str, ...] | None) -> list[str]:
        """
        标准化用户自定义标签。

        网站新分支会把上传时传入的 tags 存到 user_tags；类型标签、系统字段和空值不应该由
        SDK 当作用户标签提交。这里和前端/后端保持同一套过滤语义，并保留调用方传入顺序。
        """
        if not tags:
            return []
        normalized: list[str] = []
        for tag in tags:
            value = str(tag).strip()
            if not value or value in SYSTEM_TAGS:
                continue
            if value not in normalized:
                normalized.append(value)
        return normalized

    @staticmethod
    def _normalize_resource_type_for_models(resource_type: str | None) -> str:
        """编辑模型标签时要求调用方明确资源类型，避免给背景包传模型参数。"""
        value = (resource_type or "").strip()
        aliases = {
            "character": "character_pack",
            "character_pack": "character_pack",
            "background": "background_pack",
            "background_pack": "background_pack",
        }
        normalized = aliases.get(value)
        if not normalized:
            raise ValueError("传 verified_models 时必须同时传 resource_type='character_pack' 或 'character'")
        return normalized

    @staticmethod
    def sha256_file(filepath: str) -> str:
        """在本地计算 SHA-256；服务端不需要接触文件内容。"""
        sha256 = hashlib.sha256()
        with open(filepath, "rb") as f:
            while chunk := f.read(8 * 1024 * 1024):
                sha256.update(chunk)
        return sha256.hexdigest()

    def _upload_parts_sequential(
        self,
        *,
        filepath: str,
        key: str,
        upload_id: str,
        part_size: int,
        total_parts: int,
        file_size: int,
        parts: dict[int, dict],
        uploaded_bytes: int,
        upload_started_at: float,
        progress: ProgressCallback | None,
    ) -> int:
        session_uploaded = 0
        with open(filepath, "rb") as f:
            for part_number in range(1, total_parts + 1):
                size = self._part_bytes(part_number, file_size, part_size)
                if part_number in parts:
                    f.seek(size, os.SEEK_CUR)
                    continue

                chunk = f.read(size)
                if not chunk:
                    break

                etag, elapsed_part = self._put_and_report_part(
                    key=key,
                    upload_id=upload_id,
                    part_number=part_number,
                    chunk=chunk,
                )
                parts[part_number] = {"PartNumber": part_number, "ETag": etag}
                uploaded_bytes += len(chunk)
                session_uploaded += len(chunk)
                self._emit_upload_progress(
                    progress,
                    part_number,
                    total_parts,
                    uploaded_bytes,
                    file_size,
                    len(chunk),
                    elapsed_part,
                    session_uploaded,
                    upload_started_at,
                )
        return uploaded_bytes

    def _upload_parts_parallel(
        self,
        *,
        filepath: str,
        key: str,
        upload_id: str,
        part_size: int,
        total_parts: int,
        file_size: int,
        parts: dict[int, dict],
        uploaded_bytes: int,
        upload_started_at: float,
        workers: int,
        progress: ProgressCallback | None,
    ) -> int:
        pending_numbers = [
            part_number
            for part_number in range(1, total_parts + 1)
            if part_number not in parts and self._part_bytes(part_number, file_size, part_size) > 0
        ]
        if not pending_numbers:
            return uploaded_bytes

        workers = min(workers, len(pending_numbers))
        self._emit(
            progress,
            "uploading",
            f"并行上传启动：剩余 {len(pending_numbers)} 个分片，并发数 {workers}",
            uploaded_bytes,
            file_size,
            total_parts=total_parts,
        )

        session_uploaded = 0
        futures = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for part_number in pending_numbers:
                futures.append(executor.submit(
                    self._upload_part_from_file,
                    filepath,
                    key,
                    upload_id,
                    part_number,
                    part_size,
                    file_size,
                ))

            try:
                for future in as_completed(futures):
                    part_number, etag, size, elapsed_part = future.result()
                    parts[part_number] = {"PartNumber": part_number, "ETag": etag}
                    uploaded_bytes += size
                    session_uploaded += size
                    self._emit_upload_progress(
                        progress,
                        part_number,
                        total_parts,
                        uploaded_bytes,
                        file_size,
                        size,
                        elapsed_part,
                        session_uploaded,
                        upload_started_at,
                    )
            except Exception:
                for future in futures:
                    future.cancel()
                raise

        return uploaded_bytes

    def _upload_part_from_file(
        self,
        filepath: str,
        key: str,
        upload_id: str,
        part_number: int,
        part_size: int,
        file_size: int,
    ) -> tuple[int, str, int, float]:
        size = self._part_bytes(part_number, file_size, part_size)
        offset = (part_number - 1) * part_size
        with open(filepath, "rb") as f:
            f.seek(offset)
            chunk = f.read(size)
        if len(chunk) != size:
            raise ShinsekaiUploadError(f"分片 {part_number} 读取失败：预期 {size} 字节，实际 {len(chunk)} 字节")

        etag, elapsed_part = self._put_and_report_part(
            key=key,
            upload_id=upload_id,
            part_number=part_number,
            chunk=chunk,
            api_lock=self._session_lock,
        )
        return part_number, etag, size, elapsed_part

    def _put_and_report_part(
        self,
        *,
        key: str,
        upload_id: str,
        part_number: int,
        chunk: bytes,
        api_lock: threading.Lock | None = None,
    ) -> tuple[str, float]:
        presign_data = self._post_json_maybe_locked("/api/resources/multipart/presign", {
            "key": key,
            "upload_id": upload_id,
            "part_number": part_number,
        }, f"获取分片 {part_number} 上传凭证", api_lock)

        part_started_at = time.time()
        put = requests.put if api_lock else self.session.put
        put_resp = put(
            presign_data["presigned_url"],
            data=chunk,
            headers={"Content-Type": "application/octet-stream"},
            timeout=self.upload_timeout,
        )
        elapsed_part = time.time() - part_started_at
        if not put_resp.ok:
            raise ShinsekaiUploadError(f"分片 {part_number} PUT 失败: HTTP {put_resp.status_code}")

        etag = put_resp.headers.get("ETag", "")
        if not etag:
            raise ShinsekaiUploadError(f"分片 {part_number} PUT 成功，但响应缺少 ETag")

        self._post_json_maybe_locked("/api/resources/multipart/report-part", {
            "key": key,
            "upload_id": upload_id,
            "part_number": part_number,
            "etag": etag,
        }, f"上报分片 {part_number}", api_lock)
        return etag, elapsed_part

    def _post_json_maybe_locked(
        self,
        path: str,
        payload: dict,
        action: str,
        api_lock: threading.Lock | None,
    ) -> dict:
        if api_lock:
            with api_lock:
                return self._post_json(path, payload, action)
        return self._post_json(path, payload, action)

    def _headers(self) -> dict[str, str]:
        token = self.api_key or self.access_token
        if not token:
            raise ShinsekaiUploadError("缺少可用认证令牌，请先提供 API Key 或完成设备认证")
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _get_json(self, path: str, action: str) -> dict | list:
        resp = self.session.get(f"{self.base_url}{path}", headers=self._headers(), timeout=self.timeout)
        return self._checked_json(resp, action)

    def _post_json(self, path: str, payload: dict, action: str) -> dict:
        resp = self.session.post(f"{self.base_url}{path}", headers=self._headers(), json=payload, timeout=self.timeout)
        return self._checked_json(resp, action)

    def _delete_json(self, path: str, action: str) -> dict:
        resp = self.session.delete(f"{self.base_url}{path}", headers=self._headers(), timeout=self.timeout)
        return self._checked_json(resp, action)

    def _patch_json(self, path: str, payload: dict, action: str) -> dict:
        resp = self.session.patch(f"{self.base_url}{path}", headers=self._headers(), json=payload, timeout=self.timeout)
        return self._checked_json(resp, action)

    @staticmethod
    def _checked_json(resp: requests.Response, action: str) -> dict | list:
        if resp.ok:
            try:
                return resp.json()
            except ValueError as exc:
                raise ShinsekaiUploadError(f"{action} 失败：响应不是 JSON") from exc
        if resp.headers.get("content-type", "").startswith("application/json"):
            try:
                detail = resp.json().get("detail", resp.text[:200])
            except ValueError:
                detail = resp.text[:200]
        else:
            detail = resp.text[:200]
        raise ShinsekaiUploadError(f"{action} 失败: HTTP {resp.status_code} {detail}")

    @staticmethod
    def _device_auth_from_json(
        data: dict,
        device_id: str,
        *,
        require_api_key: bool = True,
        require_auth_token: bool = False,
    ) -> DeviceAuthInfo:
        access_token = str(data.get("access_token") or "").strip()
        api_key = str(data.get("api_key") or "").strip()
        if require_api_key and not api_key:
            raise ShinsekaiUploadError("设备认证失败：响应缺少 api_key")
        if require_auth_token and not (api_key or access_token):
            raise ShinsekaiUploadError("设备认证失败：响应缺少 api_key 或 access_token")
        return DeviceAuthInfo(
            access_token=access_token,
            api_key=api_key,
            public_id=str(data.get("public_id") or ""),
            bind_code=str(data.get("bind_code") or ""),
            is_guest=bool(data.get("is_guest", True)),
            device_id=device_id,
            refresh_token=str(data.get("refresh_token") or ""),
        )

    @staticmethod
    def _normalize_parts(raw_parts: list[dict]) -> dict[int, dict]:
        parts: dict[int, dict] = {}
        for part in raw_parts:
            try:
                part_number = int(part.get("PartNumber") or part.get("part_number"))
            except (TypeError, ValueError):
                continue
            etag = str(part.get("ETag") or part.get("etag") or "").strip()
            if part_number > 0 and etag:
                parts[part_number] = {"PartNumber": part_number, "ETag": etag}
        return parts

    @staticmethod
    def _part_bytes(part_number: int, file_size: int, part_size: int) -> int:
        offset = (part_number - 1) * part_size
        return max(0, min(part_size, file_size - offset))

    @staticmethod
    def _validate_parallel_uploads(value: int) -> int:
        try:
            workers = int(value)
        except (TypeError, ValueError):
            raise ValueError("parallel_uploads 必须是整数") from None
        if workers < 1:
            raise ValueError("parallel_uploads 必须大于等于 1")
        return workers

    @staticmethod
    def _validate_resource_id(resource_id: int) -> int:
        try:
            rid = int(resource_id)
        except (TypeError, ValueError):
            raise ValueError("resource_id 必须是整数") from None
        if rid <= 0:
            raise ValueError("resource_id 必须大于 0")
        return rid

    @staticmethod
    def _emit_upload_progress(
        progress: ProgressCallback | None,
        part_number: int,
        total_parts: int,
        uploaded_bytes: int,
        file_size: int,
        chunk_size: int,
        elapsed_part: float,
        session_uploaded: int,
        upload_started_at: float,
    ) -> None:
        elapsed_total = time.time() - upload_started_at
        chunk_speed = chunk_size / 1024 / 1024 / elapsed_part if elapsed_part > 0 else 0.0
        avg_speed = session_uploaded / 1024 / 1024 / elapsed_total if elapsed_total > 0 else 0.0
        ShinsekaiUploadClient._emit(
            progress,
            "uploading",
            f"分片 {part_number}/{total_parts} 已上传",
            uploaded_bytes,
            file_size,
            part_number=part_number,
            total_parts=total_parts,
            chunk_speed_mbps=chunk_speed,
            avg_speed_mbps=avg_speed,
        )

    @staticmethod
    def _emit(
        progress: ProgressCallback | None,
        stage: str,
        message: str,
        uploaded_bytes: int,
        total_bytes: int,
        *,
        part_number: int = 0,
        total_parts: int = 0,
        chunk_speed_mbps: float = 0.0,
        avg_speed_mbps: float = 0.0,
    ) -> None:
        if progress:
            progress(UploadProgress(
                stage=stage,
                message=message,
                uploaded_bytes=uploaded_bytes,
                total_bytes=total_bytes,
                part_number=part_number,
                total_parts=total_parts,
                chunk_speed_mbps=chunk_speed_mbps,
                avg_speed_mbps=avg_speed_mbps,
            ))
