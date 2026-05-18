#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shinsekai 资源上传示例脚本。

默认模式使用已有上传 API Key，和旧版 SDK 一样。
如果宿主是 EXE 客户端，也可以开启设备认证：SDK 会读取或创建本地 device_id，
再向 /auth/device 报到，拿到游客或已绑定用户的 access_token/API Key 后上传。
"""

import sys

from shinsekai_upload_client import ShinsekaiUploadClient, ShinsekaiUploadError, UploadProgress

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


API = "https://api.example.com"

# 旧版 API Key 模式：保持 USE_DEVICE_AUTH = False，然后填写这个值。
API_KEY = "sk-sn-your_key"

# 设备认证模式：EXE 推荐打开，并把 device_id 持久化到本地文件。
USE_DEVICE_AUTH = False
DEVICE_ID_FILE = "./shinsekai_device_id.txt"
DEVICE_ID = ""          # 如果不想用文件，也可以直接填稳定 UUID。

# 预绑定：EXE 首次启动时，如果用户已经知道网页/其他设备的绑定码，填这里。
# SDK 会把它随 /auth/device 一起提交，服务端直接把本机 device_id 挂到主用户下面。
PREBIND_CODE = ""

# 认领：当前客户端已经认证后，输入另一个游客/设备显示的绑定码，调 /auth/device/claim。
# 服务端会把那个绑定码所属用户的资源迁移到当前用户下面。
CLAIM_BIND_CODE = ""

# 可选分片并发数。1 表示旧版顺序上传；5 接近网页上传器当前策略。
PARALLEL_UPLOADS = 5


def print_progress(progress: UploadProgress) -> None:
    """命令行进度输出；接入软件时可以替换成 UI 进度回调。"""
    if progress.stage in ("hashing", "started", "completing", "done"):
        print(f"  {progress.message}")
        return

    if progress.stage == "uploading":
        print(
            f"  {progress.message} | {progress.percent:.0f}% | "
            f"{progress.chunk_speed_mbps:.1f} MB/s | avg {progress.avg_speed_mbps:.1f} MB/s"
        )


def make_client() -> ShinsekaiUploadClient:
    if USE_DEVICE_AUTH:
        if DEVICE_ID:
            client = ShinsekaiUploadClient.from_device(
                device_id=DEVICE_ID,
                bind_code=PREBIND_CODE or None,
                base_url=API,
                parallel_uploads=PARALLEL_UPLOADS,
            )
        else:
            client = ShinsekaiUploadClient.from_device_file(
                DEVICE_ID_FILE,
                bind_code=PREBIND_CODE or None,
                base_url=API,
                parallel_uploads=PARALLEL_UPLOADS,
            )

        if client.device_auth:
            print(f"设备 public_id: {client.device_auth.public_id}")
            print(f"设备 device_id: {client.device_auth.device_id}")
            print(f"本用户绑定码: {client.bind_code}")
            if client.bind_code:
                print(f"社区自动绑定链接: {client.community_bind_url()}")
            print(f"是否游客: {client.device_auth.is_guest}")

        if CLAIM_BIND_CODE:
            auth = client.claim_bind_code(CLAIM_BIND_CODE)
            print("绑定码认领完成。")
            print(f"当前 public_id: {auth.public_id}")
            print(f"当前绑定码: {auth.bind_code}")
            print(f"是否游客: {auth.is_guest}")

        return client

    if API_KEY == "sk-sn-your_key":
        print("请先设置 API_KEY；或者把 USE_DEVICE_AUTH 改成 True 使用游客/设备认证。")
        sys.exit(1)

    return ShinsekaiUploadClient(
        API_KEY,
        base_url=API,
        parallel_uploads=PARALLEL_UPLOADS,
    )


if __name__ == "__main__":
    # 格式：(资源名, 文件路径, 资源类型, 上传者展示名, 描述, 已验证模型列表, 用户标签列表)
    # resource_type: "character_pack"（.char）或 "background_pack"（.bg）
    # verified_models 只给 character_pack 使用，可选值：GPT-Sovits / Genie / MiniMax / Qwen。
    # tags 是用户自定义标签，会显示在资源卡片和筛选栏里；不要把“角色包/背景包”这类资源类型塞进 tags。
    # background_pack 不允许传模型参数；传了会被 SDK 拒绝，避免背景资源带错模型标签。
    # uploader 只是页面展示字段，真正资源归属由服务端根据 API Key/JWT 决定。
    UPLOADS = [
        # ("七海千秋", "./nanami.char", "character_pack", "", "角色包说明", ["GPT-Sovits", "Qwen"], ["剧情向", "中文"]),
        # ("教室背景", "./classroom.bg", "background_pack", "", "背景包说明", None, ["校园"]),
    ]

    if not UPLOADS:
        print("UPLOADS 列表为空。请编辑脚本添加要上传的文件。")
        sys.exit(0)

    client = make_client()
    ok = 0

    for item in UPLOADS:
        name, filepath, resource_type = item[0], item[1], item[2]
        uploader = item[3] if len(item) > 3 else ""
        description = item[4] if len(item) > 4 else ""
        verified_models = item[5] if len(item) > 5 else None
        tags = item[6] if len(item) > 6 else None
        print(f"[{name}] {filepath}")

        try:
            result = client.upload_resource(
                name,
                filepath,
                resource_type,
                uploader=uploader,
                description=description,
                tags=tags,
                verified_models=verified_models,
                progress=print_progress,
            )
            print(f"  url: {result.get('url', '')}\n")
            ok += 1
        except (OSError, ShinsekaiUploadError, ValueError) as exc:
            print(f"  失败: {exc}\n")

    print(f"结果: {ok}/{len(UPLOADS)} 个上传成功")
