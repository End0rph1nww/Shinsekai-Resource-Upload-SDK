#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shinsekai Resource Station 上传客户端。

这个文件是给软件内集成用的“小 SDK”：调用方只需要创建 client，
然后调用 upload_resource(...)。断点续传、分片签名、R2 PUT、ETag 上报、
complete 入库都封装在这里。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import time
from typing import Callable

import requests


DEFAULT_API = "https://api.end0rph1n.icu"
DEFAULT_PART_SIZE = 50 * 1024 * 1024


class ShinsekaiUploadError(RuntimeError):
    """上传流程里的接口错误统一抛这个异常，方便上层软件捕获并展示。"""


@dataclass(frozen=True)
class UploadProgress:
    """上传进度回调数据。UI 可以用这些字段更新进度条和速度显示。"""

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
    """API Key 上传客户端，适合被桌面端、本体工具或批处理脚本直接调用。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_API,
        *,
        timeout: int = 60,
        session: requests.Session | None = None,
    ):
        if not api_key:
            raise ValueError("api_key 不能为空")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    def upload_resource(
        self,
        name: str,
        filepath: str,
        resource_type: str,
        *,
        uploader: str = "",
        description: str = "",
        progress: ProgressCallback | None = None,
    ) -> dict:
        """
        上传一个资源文件。

        resource_type:
          - character_pack: .char
          - background_pack: .bg
        """
        if not os.path.isfile(filepath):
            raise FileNotFoundError(filepath)

        filename = os.path.basename(filepath)
        file_size = os.path.getsize(filepath)
        started_at = time.time()

        self._emit(progress, "hashing", "正在计算 SHA-256", 0, file_size)
        file_hash = self.sha256_file(filepath)

        # start 会返回服务端 pending 中已经完成的分片；这就是断点续传的核心。
        start_data = self._post_json("/api/resources/multipart/start", {
            "display_name": name,
            "filename": filename,
            "resource_type": resource_type,
            "total_size": file_size,
            "content_type": "application/octet-stream",
            "sha256": file_hash,
        }, "start")

        key = start_data["key"]
        upload_id = start_data["upload_id"]
        total_parts = int(start_data["total_parts"])
        part_size = int(start_data.get("part_size") or DEFAULT_PART_SIZE)
        parts = self._normalize_parts(start_data.get("parts_done", []))
        uploaded_bytes = sum(self._part_bytes(n, file_size, part_size) for n in parts)

        self._emit(
            progress,
            "started",
            f"上传任务已恢复 {len(parts)}/{total_parts} 片" if parts else "上传任务已创建",
            uploaded_bytes,
            file_size,
            total_parts=total_parts,
        )

        with open(filepath, "rb") as f:
            for part_number in range(1, total_parts + 1):
                if part_number in parts:
                    # 已完成分片只移动指针，不重复读取本地大文件块。
                    f.seek(self._part_bytes(part_number, file_size, part_size), os.SEEK_CUR)
                    continue

                chunk = f.read(part_size)
                if not chunk:
                    break

                presign_data = self._post_json("/api/resources/multipart/presign", {
                    "key": key,
                    "upload_id": upload_id,
                    "part_number": part_number,
                }, f"presign part {part_number}")

                part_started_at = time.time()
                put_resp = self.session.put(
                    presign_data["presigned_url"],
                    data=chunk,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=self.timeout,
                )
                elapsed_part = time.time() - part_started_at
                if not put_resp.ok:
                    raise ShinsekaiUploadError(f"PUT part {part_number} failed: HTTP {put_resp.status_code}")

                etag = put_resp.headers.get("ETag", "")
                if not etag:
                    raise ShinsekaiUploadError(f"PUT part {part_number} succeeded but ETag is missing")

                # report-part 成功后，服务端 pending 才真正记住这个分片。
                self._post_json("/api/resources/multipart/report-part", {
                    "key": key,
                    "upload_id": upload_id,
                    "part_number": part_number,
                    "etag": etag,
                }, f"report part {part_number}")

                parts[part_number] = {"PartNumber": part_number, "ETag": etag}
                uploaded_bytes += len(chunk)
                elapsed_total = time.time() - started_at
                chunk_speed = len(chunk) / 1024 / 1024 / elapsed_part if elapsed_part > 0 else 0.0
                avg_speed = uploaded_bytes / 1024 / 1024 / elapsed_total if elapsed_total > 0 else 0.0
                self._emit(
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

        ordered_parts = [parts[i] for i in sorted(parts)]
        if len(ordered_parts) != total_parts:
            missing = sorted(set(range(1, total_parts + 1)) - set(parts))
            raise ShinsekaiUploadError(f"missing parts before complete: {missing[:10]}")

        self._emit(progress, "completing", "正在合并分片并发布资源", file_size, file_size, total_parts=total_parts)
        result = self._post_json("/api/resources/multipart/complete", {
            "key": key,
            "upload_id": upload_id,
            "name": name,
            "resource_type": resource_type,
            "uploader": uploader,
            "description": description,
            "sha256": file_hash,
            "parts": ordered_parts,
        }, "complete")

        self._emit(progress, "done", "上传完成", file_size, file_size, total_parts=total_parts)
        return result

    def list_pending(self) -> list[dict]:
        """查询当前 API Key 所属账号下的未完成上传。"""
        return self._get_json("/api/resources/multipart/pending", "list pending")

    def delete_pending(self, pending_id: int) -> dict:
        """放弃某个未完成上传，并让服务端尝试 abort R2 multipart upload。"""
        return self._delete_json(f"/api/resources/multipart/pending/{pending_id}", f"delete pending {pending_id}")

    @staticmethod
    def sha256_file(filepath: str) -> str:
        """服务端看不到文件内容，所以 hash 必须由客户端计算。"""
        sha256 = hashlib.sha256()
        with open(filepath, "rb") as f:
            while chunk := f.read(8 * 1024 * 1024):
                sha256.update(chunk)
        return sha256.hexdigest()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _get_json(self, path: str, action: str) -> dict | list:
        resp = self.session.get(f"{self.base_url}{path}", headers=self._headers(), timeout=self.timeout)
        return self._checked_json(resp, action)

    def _post_json(self, path: str, payload: dict, action: str) -> dict:
        resp = self.session.post(f"{self.base_url}{path}", headers=self._headers(), json=payload, timeout=self.timeout)
        return self._checked_json(resp, action)

    def _delete_json(self, path: str, action: str) -> dict:
        resp = self.session.delete(f"{self.base_url}{path}", headers=self._headers(), timeout=self.timeout)
        return self._checked_json(resp, action)

    @staticmethod
    def _checked_json(resp: requests.Response, action: str) -> dict | list:
        if resp.ok:
            return resp.json()
        if resp.headers.get("content-type", "").startswith("application/json"):
            detail = resp.json().get("detail", resp.text[:200])
        else:
            detail = resp.text[:200]
        raise ShinsekaiUploadError(f"{action} failed: HTTP {resp.status_code} {detail}")

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
