# 新世界资源共享上传接口

这是面向新世界程序接入资源站的上传SDK。集成即可上传 `.char` 角色包和 `.bg` 背景包到shinsekai.end0rph1n.icu。

SDK 已经封装好这些流程：计算文件 SHA-256、请求上传任务、分片直传 R2、向服务端报告分片、断点续传、完成发布。

## 安装依赖

Python 版 SDK 只额外用到 `requests`，依赖已经写在 `requirements.txt` 里：

```bash
pip install -r requirements.txt
```

## 最小调用示例

```python
from shinsekai_upload_client import ShinsekaiUploadClient

client = ShinsekaiUploadClient("sk-sn-your_key")

result = client.upload_resource(
    name="七海千秋",
    filepath="./nanami.char",
    resource_type="character_pack",
    uploader="End0rph1n",
    description="角色包说明",
)

print(result["url"])
```

背景包只需要把 `resource_type` 改成 `background_pack`，文件路径换成 `.bg`：

```python
client.upload_resource(
    name="教室背景",
    filepath="./classroom.bg",
    resource_type="background_pack",
)
```

## 接进度条

```python
def on_progress(p):
    print(f"{p.stage} {p.percent:.1f}% {p.message}")

client.upload_resource(
    name="七海千秋",
    filepath="./nanami.char",
    resource_type="character_pack",
    progress=on_progress,
)
```

常用进度字段：`stage`、`percent`、`message`、`part_number`、`total_parts`、`chunk_speed_mbps`、`avg_speed_mbps`。

## 断点续传

同一个文件上传中断后，再调用一次 `upload_resource(...)` 就会自动续传。SDK 会读取服务端返回的 `parts_done`，跳过已经上传完成的分片。

## 查询和放弃未完成上传

```python
pending = client.list_pending()

client.delete_pending(pending_id)
```

## 小脚本测试

仓库里带了 `upload_apikey.py`，可以直接改里面的 `API_KEY` 和 `UPLOADS` 后运行：

```bash
python -X utf8 upload_apikey.py
```

## 参数说明

`upload_resource(...)` 常用参数：

| 参数 | 说明 |
|---|---|
| `name` | 资源名称 |
| `filepath` | 本地文件路径 |
| `resource_type` | `character_pack` 或 `background_pack` |
| `uploader` | 上传者名称，可不填 |
| `description` | 资源说明，可不填 |
| `progress` | 进度回调函数，可不填 |

上传成功后会返回服务端资源信息，其中最常用的是 `result["url"]`。
