#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shinsekai 资源上传示例脚本（API Key 认证）

真正的上传逻辑已经封装在 shinsekai_upload_client.py。
这个文件只负责填写 API_KEY、UPLOADS，然后调用 client。
"""

import sys

from shinsekai_upload_client import ShinsekaiUploadClient, ShinsekaiUploadError, UploadProgress

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


API = "https://api.end0rph1n.icu"
API_KEY = "sk-sn-your_key"


def print_progress(progress: UploadProgress) -> None:
    """命令行进度输出。软件内集成时可以换成 UI 进度条回调。"""
    if progress.stage in ("hashing", "started", "completing", "done"):
        print(f"  {progress.message}")
        return

    if progress.stage == "uploading":
        print(
            f"  {progress.message} | {progress.percent:.0f}% | "
            f"{progress.chunk_speed_mbps:.1f} MB/s | avg {progress.avg_speed_mbps:.1f} MB/s"
        )


if __name__ == "__main__":
    if API_KEY == "sk-sn-your_key":
        print("Set API_KEY first.")
        sys.exit(1)

    # 格式: (资源名, 文件路径, 资源类型, 上传者, 描述)
    # resource_type: "character_pack"(.char) 或 "background_pack"(.bg)
    UPLOADS = [
        # ("nanami", "./nanami.char", "character_pack", "End0rph1n", "DR character"),
        # ("classroom", "./classroom.bg", "background_pack", "End0rph1n", "classroom bg"),
    ]

    if not UPLOADS:
        print("UPLOADS list is empty. Edit the script to add files.")
        sys.exit(0)

    client = ShinsekaiUploadClient(API_KEY, base_url=API)
    ok = 0

    for item in UPLOADS:
        name, filepath, resource_type = item[0], item[1], item[2]
        uploader = item[3] if len(item) > 3 else ""
        description = item[4] if len(item) > 4 else ""
        print(f"[{name}] {filepath}")

        try:
            result = client.upload_resource(
                name,
                filepath,
                resource_type,
                uploader=uploader,
                description=description,
                progress=print_progress,
            )
            print(f"  url: {result.get('url', '')}\n")
            ok += 1
        except (OSError, ShinsekaiUploadError, ValueError) as exc:
            print(f"  failed: {exc}\n")

    print(f"result: {ok}/{len(UPLOADS)} succeeded")
