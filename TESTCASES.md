# Shinsekai Upload SDK 测试设计与线上准备

本文档按网站源码、SDK 源码和 README 对齐，描述要实现的 EXE 上传体验、离线测试覆盖、线上冒烟测试、破坏性全量线上测试，以及还需要人工观察的页面行为。

## 目标行为

EXE 用户点击上传按钮时，SDK 不在本地生成六位绑定码。SDK 只生成并长期保存稳定的 `device_id` 文件，然后调用 `/auth/device`。服务端用这个 `device_id` 创建或找回 `role=device` 游客身份，并返回固定的 `bind_code`。同一个 `device_id` 之后再次认证时，应拿到同一个 `bind_code`；如果服务端不再返回明文 API Key，SDK 会使用 `access_token` 继续上传。

上传文件走 `multipart/start -> multipart/presign -> PUT R2 -> multipart/report-part -> multipart/complete`。SDK 会在设备认证模式下把当前 `bind_code` 放进 `multipart/start` 和 `multipart/complete` 的 JSON 里，便于服务端后续校验或审计。按当前网站源码，资源归属仍由 Bearer 认证身份决定，`bind_code` 暂时不写进 `.char` / `.bg` 文件本体，也不参与当前入库归属判断。Private 新分支在入库阶段支持 `tags`，SDK 会在 `multipart/complete` 里提交用户自定义标签，并过滤空标签、重复标签和系统类型词。

EXE 的“进入社区资源页”按钮应使用 `client.community_bind_url(...)` 生成 `/resources?bind=XXXXXX`。网站前端读取 `?bind=` 后，如果当前浏览器已有 `access_token` 或 `device_api_key`，会调用 `/auth/device/claim` 建立共享管理关系；如果浏览器还没有身份，则调用 `/auth/device` 携带 `bind_code` 做预绑定。两条路径都应让用户在网页端看到并管理 EXE 已上传资源；区别是 claim 不改资源原归属，预绑定会让网页直接成为 EXE 同一身份。

## 源码确认点

| 功能点 | 源码行为 | SDK 对应 |
|---|---|---|
| 设备入口 | `/auth/device` 先处理有效 `bind_code`，再按 `device_id`、`user_devices`、`fingerprint_hash` 查找，否则创建游客 | `from_device(...)` / `from_device_file(...)` |
| 绑定码来源 | 服务端为没有 `bind_code` 的用户生成 6 位码 | `client.bind_code` 只读取服务端返回值 |
| API Key 复用 | 已有 `device-key` 时 `/auth/device` 可能返回空 `api_key`，但仍返回 `access_token` | SDK 允许 `api_key=""`，上传时用可用 token |
| 指纹同步 | 前端先 SHA-256 原始 WebGL 指纹，后端再对收到值 SHA-256 入库 | SDK 对原始 EXE 指纹先规整为 64 位 hex |
| URL 自动绑定 | `/resources?bind=XXXXXX` 有 token 时走 claim，无 token 时走预绑定；claim 后可编辑删除已认领资源 | `community_bind_url(...)` / `build_bind_url(...)` / `edit_resource(...)` / `delete_resource(...)` |
| 上传并行 | 网站分片上传固定 `CONC = 5`，每批 `Promise.all` | SDK `parallel_uploads=5` 用 `ThreadPoolExecutor` |
| 模型参数 | `verified_models` 只对 `character_pack` 有意义 | SDK 只允许 `.char` 传模型参数，`.bg` 直接拒绝 |
| 用户标签 | `/api/resources/confirm` 和 `/api/resources/multipart/complete` 接收 `tags`；`/api/tags` 返回已有用户标签 | SDK `upload_resource(tags=...)` 只在入库阶段提交标签，`list_tags()` 读取自动补全建议 |

## 离线 pytest 覆盖矩阵

默认测试不访问线上服务，全部使用模拟服务端和内存业务模型。

```powershell
python -m pytest tests -q
```

| 分类 | 用例 | 覆盖点 |
|---|---|---|
| SDK 参数 | `test_validation_and_helpers` | API Key、并发数、文件路径、绑定 URL、指纹标准化、进度百分比 |
| 设备文件 | `test_device_id_file_create_and_reuse` | `device_id` 文件首次创建和复用 |
| 设备认证 | `test_device_auth_from_device_and_file` | 普通 `/auth/device`，返回 `bind_code` |
| Token 兼容 | `test_device_auth_without_api_key_uses_access_token_for_upload` | 空 `api_key` 时用 `access_token` 上传 |
| 预绑定 | `test_device_auth_prebind` | EXE 已知主绑定码时直连主身份 |
| 认领 | `test_claim_bind_code` | `/auth/device/claim` 后 SDK 更新认证状态 |
| 旧兼容 | `test_merge_compatibility` | 保留 `/auth/device/merge` 兼容入口 |
| 上传链路 | `test_sequential_upload_full_chain` | start、presign、PUT、report、complete |
| 绑定元数据 | `test_device_upload_includes_bind_code_metadata` | 设备上传 payload 自动携带 `bind_code` |
| 模型参数 | `test_character_upload_includes_verified_models` | `.char` 携带模型参数并去重 |
| 用户标签 | `test_upload_includes_user_tags_only_on_complete` | 上传标签只在 complete 提交，过滤空值、重复值和系统类型词 |
| 模型限制 | `test_background_upload_rejects_verified_models` | `.bg` 禁止传模型参数 |
| 并行上传 | `test_parallel_upload_full_chain` | 多分片并发上传、结果排序 |
| 断点续传 | `test_resume_upload_skips_finished_parts` | 跳过 `parts_done`，合并完整 parts |
| Pending 管理 | `test_pending_list_and_delete` | list/delete pending 接口 |
| 资源管理 | `test_resource_management_methods` | `/api/my-uploads`、`/api/tags`、资源编辑、资源删除 |
| 错误路径 | `test_error_paths` | HTTP、JSON、PUT、ETag、空文件等异常 |

## 绑定业务场景

| 编号 | 用例 | 预期 |
|---|---|---|
| Q1 | 浏览器游客上传 -> EXE 用浏览器 `bind_code` 预绑定 -> EXE 上传 | 两边 `my_uploads` 都能看到浏览器和 EXE 文件 |
| Q2 | 浏览器 A 上传 -> 浏览器 B 同指纹认证 | 找回同一游客身份，`bind_code` 和文件都一致 |
| Q2b | 浏览器发 hash 指纹，EXE 发原始指纹经 SDK 规整 | 两边命中同一后端 `fingerprint_hash` |
| Q3 | 游客注册成正式用户 | `bind_code` 不变，旧游客 API Key 仍可用，文件不丢 |
| Q4 | EXE 首传 -> 打开 `/resources?bind=EXE码`，浏览器无身份 | 网页预绑定到 EXE 游客，看到 EXE 文件 |
| Q4b | EXE 首传 -> 已有浏览器游客打开 `/resources?bind=EXE码` | 网页走 claim，当前浏览器可见并可编辑/删除 EXE 文件，但资源原归属仍是 EXE 身份 |
| Q5 | 同设备多次认证、游客注册、第三设备预绑定 | 对外展示的主 `bind_code` 保持稳定 |
| E1 | 旧游客 API Key 注册升级后继续上传 | 上传仍归升级后的用户 |
| E2 | 同设备重复预绑定 | 不创建多余活跃用户 |
| E3 | 已认领设备再次被同一用户认领 | 幂等返回当前用户 |
| E3b | 已认领游客码被另一用户认领 | A 和 B 都可见并可管理该游客资源，但 A/B 自己的私有资源不会互相暴露 |
| E4 | 没有 `bind_code` | 正常创建新游客 |
| E4b | 传入不存在但格式像 6 位码的 `bind_code` | 按当前后端逻辑会退回普通游客认证，不会合并资源 |
| E5 | 游客上传 -> EXE 认领该游客码 -> EXE 上传 | EXE 可见并可管理游客文件和自己的文件；游客仍只拥有自己的文件 |

## 线上 pytest 准备

线上测试文件在每个 pytest 用例里用 `tmp_path` 全新生成，文件名和内容都带唯一 UUID，不会复用旧 SHA-256。测试完成后会尽量调用 `DELETE /api/resources/{id}` 清理自己上传的资源。

默认不会跑线上测试：

```powershell
python -m pytest tests/test_online_smoke.py -q
```

启动线上冒烟测试：

```powershell
$env:SHINSEKAI_ONLINE_TEST = "1"
$env:SHINSEKAI_BASE_URL = "https://api.end0rph1n.icu"
$env:SHINSEKAI_WEB_URL = "https://shinsekai.end0rph1n.icu"
python -m pytest tests/test_online_smoke.py -q -s
```

可选：验证传统 API Key 上传路径。

```powershell
$env:SHINSEKAI_API_KEY = "sk-sn-..."
python -m pytest tests/test_online_smoke.py::test_online_api_key_character_upload -q -s
```

可选：验证超过 20MB 的真实多分片并行上传。

```powershell
$env:SHINSEKAI_ONLINE_LARGE = "1"
python -m pytest tests/test_online_smoke.py::test_online_large_parallel_upload -q -s
```

启动破坏性全量线上测试：

```powershell
$env:SHINSEKAI_ONLINE_FULL = "1"
python -m pytest tests/test_online_full.py -q -s
```

破坏性全量线上测试会创建游客、注册用户、API Key、绑定关系和测试资源。资源会尽量删除，但用户和绑定表记录不会自动清理，适合测试环境或允许污染的线上排查。

## 线上自动用例

| 用例 | 访问线上接口 | 文件 |
|---|---|---|
| `test_online_device_bind_code_stable_across_reauth` | `/auth/device` | 不上传，只验证同 `device_id` 的 `bind_code` 稳定 |
| `test_online_device_character_upload_bind_url_and_my_uploads` | `/auth/device`、multipart 全链路、`/api/my-uploads`、删除 | 新 `.char`，带 `verified_models` |
| `test_online_device_background_upload` | `/auth/device`、multipart 全链路、`/api/my-uploads`、删除 | 新 `.bg`，不带模型参数 |
| `test_online_sdk_resource_management_owner_roundtrip` | SDK `list_my_uploads`、`edit_resource`、`delete_resource` | 新 `.char`，编辑后删除 |
| `test_online_prebind_second_device_syncs_uploads` | 两个 `/auth/device`，第二个携带第一个 `bind_code` | 两个新 `.char` |
| `test_online_claim_guest_bind_code_syncs_uploads` | 两个游客上传，当前游客 `claim_bind_code(...)` | 两个新 `.char` |
| `test_online_api_key_character_upload` | 传统 API Key Bearer 上传 | 新 `.char`，需要 `SHINSEKAI_API_KEY` |
| `test_online_large_parallel_upload` | >20MB multipart，`parallel_uploads=5` | 新大 `.char`，需要 `SHINSEKAI_ONLINE_LARGE=1` |

## 破坏性全量线上用例

| 用例 | 覆盖点 |
|---|---|
| `test_online_full_q1_browser_guest_upload_then_exe_prebind_syncs` | 浏览器游客上传后，EXE 用浏览器绑定码预绑定并同步资源。 |
| `test_online_full_q2_same_fingerprint_recovers_guest_identity` | 不同 `device_id` 但相同指纹时找回同一游客身份。 |
| `test_online_full_q3_register_keeps_bind_code_and_guest_token_uploads` | 游客注册升级后绑定码保持不变，旧游客 token 继续上传且资源归同一用户。 |
| `test_online_full_q4_exe_first_upload_then_web_prebind_syncs` | EXE 先上传，网页无身份时用 `?bind=` 预绑定并看到 EXE 文件。 |
| `test_online_full_q4_existing_browser_claims_exe_bind_code` | 已有浏览器游客用 EXE 绑定码 claim，网页可见并可管理双方资源，资源原归属不变。 |
| `test_online_full_q5_bind_code_stable_after_register_and_third_prebind` | 注册和第三设备预绑定后主绑定码保持稳定。 |
| `test_online_full_invalid_bind_code_creates_independent_guest` | 无效绑定码不会合并到主用户，会创建独立游客。 |
| `test_online_full_repeated_prebind_same_device_is_idempotent` | 同一设备重复预绑定保持同一主身份。 |
| `test_online_full_claim_self_rejected_and_already_claimed_by_other_returns_current_identity` | 认领自己应拒绝；A 和 B 可重复 claim 同一游客码并共享管理该游客资源，但不会互相看到对方私有资源；认领方可编辑/删除被认领资源。 |
| `test_online_full_unclaimed_resource_edit_delete_forbidden` | 未认领资源不能被外部身份编辑或删除。 |
| `test_online_full_registered_bind_claim_rejected_but_prebind_uploads_sync` | 注册用户绑定码不能被 `/auth/device/claim` 当作游客码认领，但可用于 `/auth/device` 预绑定并同步上传。 |
| `test_online_full_duplicate_sha256_is_rejected` | 相同文件 SHA-256 重复上传会被服务端拒绝，避免资源重复入库。 |
| `test_online_full_registered_user_can_create_api_key_and_upload` | 注册用户创建 API Key 后，传统 API Key 上传路径可用。 |
| `test_online_full_device_tts_is_forbidden` | 游客 `role=device` 调 TTS 应返回 403。 |

## 手工页面验证

自动 pytest 只能验证 API 和 SDK 行为。`?bind=` 页面交互还需要浏览器确认一次：

1. 运行 `test_online_device_character_upload_bind_url_and_my_uploads`，记录输出或在调试里打印 `client.community_bind_url(...)`。
2. 清空一个测试浏览器的 localStorage，打开 `/resources?bind=XXXXXX`。
3. 预期：页面自动去掉 URL 里的 `bind` 参数并刷新，“我的资源”能看到 EXE 上传文件。
4. 再用已有游客浏览器打开另一个 EXE 的 `/resources?bind=XXXXXX`。
5. 预期：当前浏览器身份保留，EXE 文件出现在当前浏览器的“我的资源”列表里；该文件原归属仍是 EXE 身份，但当前浏览器可以编辑和删除它。

## 线上测试配置

线上基础测试需要配置以下环境变量：

- `SHINSEKAI_BASE_URL`：API 域名，默认 `https://api.end0rph1n.icu`。
- `SHINSEKAI_WEB_URL`：社区网页域名，默认 `https://shinsekai.end0rph1n.icu`。
- `SHINSEKAI_ONLINE_FULL`：设为 `1` 时启用破坏性全量线上测试。

如需覆盖 API Key 旧接入路径，额外配置：

- `SHINSEKAI_API_KEY`：一个可上传资源的 API Key。

破坏性全量线上测试会自动生成 `codex-full-*.example.com` 形式的测试邮箱，并创建可丢弃正式账号；运行前确认线上环境允许这些污染数据。
