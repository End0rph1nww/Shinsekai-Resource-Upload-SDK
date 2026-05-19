# Shinsekai Upload SDK 测试设计与线上准备

本文档按网站源码、SDK 源码和 README 对齐，描述要实现的 EXE 上传体验、离线测试覆盖、线上冒烟测试、破坏性全量线上测试，以及还需要人工观察的页面行为。

## 目标行为

EXE 用户点击上传按钮时，SDK 不在本地生成六位绑定码。SDK 只生成并长期保存稳定的 `device_id` 文件，然后调用 `/auth/device`。服务端用这个 `device_id` 创建或找回 `role=device` 游客身份，并返回固定的 `bind_code`。同一个 `device_id` 之后再次认证时，应拿到同一个 `bind_code`；如果服务端不再返回明文 API Key，SDK 会使用 `access_token` 继续上传。

`/auth/device` 只允许作为设备游客入口使用。SDK 会要求响应里 `is_guest=true`；如果服务端把同一个 `device_id` 误恢复成注册账号身份，SDK 会抛错中止，避免 EXE 或浏览器刷新绕过“网页登录后再认领”的流程。

上传文件走 `multipart/start -> multipart/presign -> PUT R2 -> multipart/report-part -> multipart/complete`。SDK 会在设备认证模式下把当前 `bind_code` 放进 `multipart/start` 和 `multipart/complete` 的 JSON 里，便于服务端做展示、排查和审计。当前网站源码用 Bearer 认证身份决定最终 owner；`user_claims` 只决定当前身份还能看到、编辑和删除哪些已认领资源。`bind_code` 不写进 `.char` / `.bg` 文件本体，也不会让 EXE 继承 master API Key。Private 新分支在入库阶段支持 `tags`，SDK 会在 `multipart/complete` 里提交用户自定义标签，并过滤空标签、重复标签和系统类型词。

EXE 的“进入社区资源页”按钮应使用 `client.community_bind_url(...)` 生成 `/resources?bind=XXXXXX`。网站前端读取 `?bind=` 后，如果当前浏览器已有登录态，会调用 `/auth/device/claim` 建立共享管理关系，让网页端看到并管理 EXE 已上传资源；如果当前浏览器是游客态，则 claim 关系会被记录，但游客不展示、不编辑、不删除认领资源，后续注册或登录后继承；如果浏览器还没有身份，则先调用 `/auth/device` 创建浏览器自己的游客身份，再调用 `/auth/device/claim`。这个流程不会迁移资源 owner，也不会继承 EXE 或 master API Key。

普通 EXE 不建议实现“管理我的资源”本地页面。用户自己的资源编辑、删除、改名和标签维护建议统一交给网站登录态完成；SDK 侧只负责上传、展示绑定码、打开社区绑定页。软件内如果只是展示或下载公开资源，应使用全站公开列表 `/api/resources?offset=0&limit=100` 和资源对象里的 `url` 直链，不需要登录态。

## 源码确认点

| 功能点 | 源码行为 | SDK 对应 |
|---|---|---|
| 设备入口 | `/auth/device` 只按 `device_id` 创建或找回 `role=device` 游客；`bind_code` 兼容参数不再改变请求身份；注册账号不能靠 `device_id` 静默恢复。旧指纹字段不作为 SDK 接入路径 | `from_device(...)` / `from_device_file(...)`，并拒绝非游客响应 |
| 绑定码来源 | 服务端为没有 `bind_code` 的用户生成 6 位码 | `client.bind_code` 只读取服务端返回值 |
| API Key 复用 | 已有 `device-key` 时 `/auth/device` 可能返回空 `api_key`，但仍返回 `access_token` | SDK 允许 `api_key=""`，上传时用可用 token |
| URL 自动绑定 | `/resources?bind=XXXXXX` 有登录 token 时走 claim 并可管理；游客 token 只记录 claim，注册/登录后继承；无 token 时先创建浏览器游客再 claim | `community_bind_url(...)` / `build_bind_url(...)` / `edit_resource(...)` / `delete_resource(...)` |
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
| SDK 参数 | `test_validation_and_helpers` | API Key、并发数、文件路径、绑定 URL、进度百分比 |
| 设备文件 | `test_device_id_file_create_and_reuse` | `device_id` 文件首次创建和复用 |
| 设备认证 | `test_device_auth_from_device_and_file` | 普通 `/auth/device`，返回 `bind_code` |
| Token 兼容 | `test_device_auth_without_api_key_uses_access_token_for_upload` | 空 `api_key` 时用 `access_token` 上传 |
| Token 优先级 | `test_device_auth_prefers_access_token_over_rotating_device_key` | 同时存在 device key 与 JWT 时优先用 `access_token`，避免旧 device key 串号 |
| 注册身份防护 | `test_device_auth_rejects_registered_identity_response` | `/auth/device` 如果返回非游客身份，SDK 拒绝，防止 `device_id` 变成注册账号登录凭证 |
| Token 刷新 | `test_device_auth_refreshes_access_token_once_after_401` | 设备 JWT 401 后只重新调用一次 `/auth/device` 并替换 token |
| Key 轮换刷新 | `test_device_auth_refresh_replaces_rotated_device_key_after_401` | 服务端轮换 device key 后，SDK 替换旧 key 与旧 JWT |
| 兼容绑定码参数 | `test_device_auth_bind_code_argument_does_not_change_exe_identity` | EXE 即使传入绑定码兼容参数，也只认证自己的 device 身份，后续上传 owner 不迁移 |
| 认领 | `test_claim_bind_code` | `/auth/device/claim` 后 SDK 更新认证状态 |
| 旧兼容 | `test_merge_compatibility` | 保留 `/auth/device/merge` 兼容入口 |
| 上传链路 | `test_sequential_upload_full_chain` | start、presign、PUT、report、complete |
| 重复上传 | `test_upload_returns_existing_resource_when_start_reports_duplicate` | `multipart/start` 返回重复资源时复用已有结果 |
| 重复入库 | `test_upload_returns_existing_resource_when_complete_reports_duplicate` | `multipart/complete` 返回重复资源时复用已有资源 |
| 绑定元数据 | `test_device_upload_includes_bind_code_metadata` | 设备上传 payload 自动携带 `bind_code` |
| 模型参数 | `test_character_upload_includes_verified_models` | `.char` 携带模型参数并去重 |
| 用户标签 | `test_upload_includes_user_tags_only_on_complete` | 上传标签只在 complete 提交，过滤空值、重复值和系统类型词 |
| 模型限制 | `test_background_upload_rejects_verified_models` | `.bg` 禁止传模型参数 |
| 并行上传 | `test_parallel_upload_full_chain` | 多分片并发上传、结果排序 |
| 断点续传 | `test_resume_upload_skips_finished_parts` | 跳过 `parts_done`，合并完整 parts |
| Pending 管理 | `test_pending_list_and_delete` | list/delete pending 接口 |
| 中断上传 | `test_abort_multipart_upload_by_key_and_upload_id` | 按 `key` 与 `upload_id` 调用 `/api/resources/multipart/abort` |
| 资源管理 | `test_resource_management_methods` | `/api/my-uploads`、`/api/tags`、资源编辑、资源删除 |
| 错误路径 | `test_error_paths` | HTTP、JSON、PUT、ETag、空文件等异常 |

## 绑定业务场景

| 编号 | 用例 | 预期 |
|---|---|---|
| Q1 | 浏览器游客上传 -> EXE 上传 -> 浏览器打开 EXE `?bind=` -> 浏览器注册/登录 | 游客 claim 会被记录但认领资源隐藏；注册/登录后能看到浏览器自己的文件和 EXE 文件；EXE 只看到自己的文件，资源原归属不变 |
| Q2 | 浏览器 A 上传 -> 浏览器 B 没有原 `device_id`，也没有 `bind_code` | 浏览器 B 得到独立游客身份，看不到 A 的文件 |
| Q3 | 游客注册升级成正式用户 | `bind_code` 不变，当前 device key 仍指向升级后的同一用户，文件不丢 |
| Q3b | 游客注册后退出登录，同一 `device_id` 刷新并再次走 `/auth/device` | 创建或找回新的游客身份，不会自动恢复注册账号 |
| Q4 | EXE 首传 -> 打开 `/resources?bind=EXE码`，浏览器无身份 | 网页创建自己的 device 游客身份并记录 claim；注册/登录前不展示或管理 EXE 文件，网页后续上传仍归属网页身份，不继承 EXE API Key |
| Q4b | EXE 首传 -> 已有浏览器游客打开 `/resources?bind=EXE码` | 网页走 claim 并记录关系；注册/登录后当前账号可见并可编辑/删除 EXE 文件，但资源原归属仍是 EXE 身份 |
| Q5 | 同设备多次认证、游客注册、第三设备传入绑定码兼容参数 | 主 `bind_code` 保持稳定；第三设备不会切换成主身份 |
| E1 | 已有账号登录并认领当前游客 | 当前游客 key 被拒绝，避免旧游客 key 串号；新 JWT 仍可正常调用 `/users/me` |
| E2 | 同设备重复认证并传入绑定码兼容参数 | 不创建多余活跃用户，也不切换身份 |
| E3 | 已认领设备再次被同一用户认领 | 幂等返回当前用户 |
| E3b | 已认领绑定码被另一用户认领 | A 和 B 都可见并可管理该绑定码 owner 的资源，但 A/B 自己的私有资源不会互相暴露 |
| E4 | 没有 `bind_code` | 正常创建新游客 |
| E4b | 传入不存在但格式像 6 位码的 `bind_code` | 按当前后端逻辑会退回普通游客认证，不会合并资源 |
| E5 | 游客上传 -> EXE 游客认领该绑定码 -> EXE 上传 -> EXE 游客升级登录 | 游客阶段只记录认领关系并保留自己的上传；升级登录后可见并可管理该绑定码 owner 的文件和自己的文件；源身份仍只拥有自己的文件 |

## 线上 pytest 准备

线上测试文件在每个 pytest 用例里用 `tmp_path` 全新生成，文件名和内容都带唯一 UUID，不会复用旧 SHA-256。测试完成后会尽量调用 `DELETE /api/resources/{id}` 清理自己上传的资源。

默认不会跑线上测试：

```powershell
python -m pytest tests/test_online_smoke.py -q
```

启动线上冒烟测试前，先复制 example 并在本地填写真实测试环境：

```powershell
Copy-Item .online_env.example.ps1 .online_env.ps1
notepad .online_env.ps1
. .\.online_env.ps1
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
| `test_online_device_reauth_rotates_key_and_rejects_stale_key` | `/auth/device`、`/api/my-uploads` | 同一设备重新认证会拿到新 device key，旧 key 失效，新 token 仍可读资源列表 |
| `test_online_abort_multipart_upload_by_key_and_upload_id` | `/auth/device`、`/api/resources/multipart/start`、`/api/resources/multipart/abort` | 只创建 pending multipart 会话并立即中断，不 PUT 文件 |
| `test_online_device_character_upload_bind_url_and_my_uploads` | `/auth/device`、multipart 全链路、`/api/my-uploads`、删除 | 新 `.char`，带 `verified_models` |
| `test_online_device_background_upload` | `/auth/device`、multipart 全链路、`/api/my-uploads`、删除 | 新 `.bg`，不带模型参数 |
| `test_online_sdk_resource_management_owner_roundtrip` | SDK `list_my_uploads`、`edit_resource`、`delete_resource` | 新 `.char`，编辑后删除 |
| `test_online_device_bind_code_argument_does_not_change_upload_owner` | 两个 `/auth/device`，第二个传入第一个 `bind_code` 兼容参数 | 两个新 `.char` |
| `test_online_guest_claim_bind_code_is_hidden_until_login` | 两个游客上传，当前游客 `claim_bind_code(...)` 后注册登录 | 两个新 `.char`；游客阶段隐藏认领资源，登录后继承 |
| `test_online_api_key_character_upload` | 传统 API Key Bearer 上传 | 新 `.char`，需要 `SHINSEKAI_API_KEY` |
| `test_online_large_parallel_upload` | >20MB multipart，`parallel_uploads=5` | 新大 `.char`，需要 `SHINSEKAI_ONLINE_LARGE=1` |

## 破坏性全量线上用例

| 用例 | 覆盖点 |
|---|---|
| `test_online_full_q1_browser_guest_upload_then_opening_exe_bind_claims_exe_files` | 浏览器游客上传后打开 EXE 绑定页，游客 claim 关系被记录；注册/登录后继承 EXE 文件，EXE 资源原归属不变。 |
| `test_online_full_q2_new_device_without_bind_is_separate_identity` | 不同 `device_id` 且没有 `bind_code` 时创建独立游客身份。 |
| `test_online_full_q3_register_keeps_bind_code_and_current_device_key_identity` | 游客注册升级后绑定码保持不变，当前 device key 仍指向升级后的同一用户，已上传资源仍归注册用户。 |
| `test_online_full_existing_user_login_jwt_survives_stale_guest_key_users_me_401` | 已有账号登录并认领当前游客时，旧游客 key 失效返回 401，但新 JWT 调 `/users/me` 仍有效。 |
| `test_online_full_logout_refresh_device_auth_returns_new_guest` | 游客升级注册后，同一 `device_id` 再次设备认证应返回新游客，不应恢复注册账号。 |
| `test_online_full_q4_exe_first_upload_then_web_claims_exe_files` | EXE 先上传，网页无身份时先创建自己的 device 游客身份，再记录 claim；注册/登录后继承 EXE 既有文件，网页后续上传仍归属网页身份。 |
| `test_online_full_q4_existing_browser_claims_exe_bind_code` | 已有浏览器游客用 EXE 绑定码 claim，游客阶段隐藏认领资源；注册/登录后可见并可管理双方资源，资源原归属不变。 |
| `test_online_full_q5_bind_code_stable_after_register_and_third_bind_argument_ignored` | 注册后主绑定码保持稳定；第三设备传入该码也不会切换身份。 |
| `test_online_full_invalid_bind_code_creates_independent_guest` | 无效绑定码不会合并到主用户，会创建独立游客。 |
| `test_online_full_repeated_device_auth_with_bind_code_argument_is_idempotent` | 同一设备重复传入绑定码兼容参数时保持幂等，不新增身份。 |
| `test_online_full_claim_self_rejected_and_already_claimed_by_other_returns_current_identity` | 认领自己应拒绝；A 和 B 可重复 claim 同一绑定码并共享管理该绑定码 owner 资源，但不会互相看到对方私有资源；注册/登录认领方可编辑/删除被认领资源，游客认领方需升级后继承。 |
| `test_online_full_unclaimed_resource_edit_delete_forbidden` | 未认领资源不能被外部身份编辑或删除。 |
| `test_online_full_registered_bind_can_be_claimed_but_device_auth_does_not_prebind` | 注册用户绑定码可被 `/auth/device/claim` 建立共享管理关系；同一个绑定码传给 `/auth/device` 不会让后续上传归到注册用户。 |
| `test_online_full_duplicate_sha256_returns_existing_resource` | 相同文件 SHA-256 重复上传会返回已有资源，避免重复入库并保持 SDK 结果可用。 |
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

- `SHINSEKAI_BASE_URL`：API 域名。仓库只提供占位值，真实地址写在本地 `.online_env.ps1`。
- `SHINSEKAI_WEB_URL`：社区网页域名。仓库只提供占位值，真实地址写在本地 `.online_env.ps1`。
- `SHINSEKAI_ONLINE_FULL`：设为 `1` 时启用破坏性全量线上测试。

如需覆盖 API Key 旧接入路径，额外配置：

- `SHINSEKAI_API_KEY`：一个可上传资源的 API Key。

破坏性全量线上测试会自动生成 `codex-full-*.example.com` 形式的测试邮箱，并创建可丢弃正式账号；运行前确认线上环境允许这些污染数据。
