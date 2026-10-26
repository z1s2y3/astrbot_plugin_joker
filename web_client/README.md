# Joker 群管客户中心

独立的客户网页后台。Linux 服务器只保存网页账号和可编辑的群配置；电脑上的 AstrBot/Joker 主动拉取配置并执行群功能。客户使用邮箱验证码注册独立网页账号；群授权卡密只在登录后用于登记群聊。卡密续费或更换不会影响客户账号。

推荐使用“本地主控 + 远程网页”模式：Windows 本地 AstrBot 是唯一群功能执行端，主动连接服务器网页上传状态并拉取客户改动。服务器无需访问你的电脑，也不要求公网 IP、端口映射或内网穿透。电脑关闭时网页仍能保存配置，待 AstrBot 恢复运行后自动应用；电脑关闭期间机器人不会处理群消息。

管理员后台的“执行端”页面提供 Linux 迁移预留开关，以及受同步令牌保护的 `GET /api/runner/v1/capabilities` 接口。该开关目前只记录迁移计划，**不会**在 Linux 启动机器人，也不会停止 Windows；未来 Linux 运行器会通过此接口识别环境并完成独立的接管确认。

管理员后台的“更新”页面支持上传后续 Linux 网页部署 ZIP 包。网页不会直接执行或完整解压上传文件：它只限制请求大小、检查扩展名并以私有权限暂存，随后在页面响应发出后创建由 root systemd 任务处理的更新请求。root 安全接收器才是权威验证点，会在 root 专用目录中检查 ZIP 路径、符号链接、文件数量、解压大小和清单 SHA-256，再执行安装。提交时必须填写服务器 `/root/joker-web-update-key.txt` 中的独立授权码；该授权码不写入数据库或网页日志。更新仅替换 Linux 网页服务，Windows AstrBot/NapCat、群配置权威数据和本地缓存不会被上传或改写。

管理员“网页更新”页面会同时显示当前正在运行的版本、实际部署包文件名、安装完成时间、更新任务状态和已通过清单校验的目标版本。当前版本来自安装器在健康检查成功后写入的 `.joker-web-release.json`，不会根据上传文件名猜测；旧安装尚未写入该标记时显示“未记录”，升级一次完整部署包后即可补齐。

## 两种数据模式

- `remote`（推荐）：网页可单独部署到宝塔。MySQL 保存账号、分群配置投影、脱敏授权摘要、同步状态和操作日志。本地插件每隔一段时间主动同步。
- 同步范围按网页账号绑定群划分：Windows 首轮只协商范围，绑定群才持续上传配置、运行摘要和消息记录；未绑定群仅发送首次卡密登记所需的脱敏授权投影，不上传完整分群配置。
- `local`（兼容）：网页和 AstrBot 在同一台机器，直接读取 `plugin_data`。仅适合本机调试或同机部署。

远程模式不会上传完整签到历史、原始授权卡密、授权密钥、主人账号或 NapCat/QQ 登录资料。Windows 会按心跳上传受限的黑名单/统计只读摘要，供分群页面查看；这些运行数据独立存储，网页保存配置不会覆盖它们。自动回复、定时任务、分群管理员等客户可编辑配置会同步。

启用后台“群消息记录显示”后，Windows 会上传脱敏动作摘要（时间、群号、动作、用户号、消息类型、消息 ID 和简短状态）。管理员可再单独开启“同步已授权群的消息接收与机器人回复”：此时仅同步当前已授权分群的可读文字、@ 和 `[图片]` 等安全媒体标记，客户只能查看自己已绑定群的记录。图片 URL、Windows 路径、卡密和私聊内容不会上传；关闭该子开关后 Windows 不再保存或上传新的消息正文。

网页自动回复可添加文字、图片或两者。Linux 会校验并保存图片，分群配置只保留图片资源 ID 和哈希；Windows 机器人在下一次 HTTPS 同步时使用现有同步令牌主动下载、校验哈希并缓存到本地后发送。因此无需向 Windows 暴露公网端口，也不会把图片、令牌或固定域名写入规则。

网页还提供一个可选的低延迟游戏操作通道：兼容的 Windows 执行端使用独立游戏 WebSocket 入口令牌连接
`/api/sync/v1/ws`（别名 `/api/game/v1/ws`），首帧发送
`{"type":"hello","protocol":"joker-game-v1","client_id":"..."}`。受信任的后端可向
`/api/sync/v1/game/call` 提交 `client_id`、`operation` 和 `payload`，由 Windows 在本地执行
并通过 WebSocket 返回结果。SCUM 服务器查询则走受同步令牌保护的
`/api/sync/v1/scum/query/{group_id}`，Linux 网页端再通过 `SCUM_RELAY_KEY`
调用 BattleMetrics 中转站；Windows 只转发命令并回显结果。该通道不会让网页服务器访问群消息或 QQ 接口。
未配置游戏 WebSocket 入口令牌时，游戏操作连接会被拒绝。

`JOKER_WEB_SYNC_TOKEN` 与游戏 WebSocket 入口令牌完全分离：前者只用于 Linux 网页与 Windows 插件的 HTTPS 配置同步，必须与 Windows `web_sync.sync_token` 相同；后者只能在管理员“游戏 WebSocket”页面生成、加密保存，并在一次已认证同步后自动写入 Windows `web_sync.game_websocket_token`。不要把两个令牌设为同一值，也不要把游戏入口令牌填回 `web_sync.sync_token`。

管理员可直接打开后台“游戏 WebSocket”页面设置 Windows 执行端开关、im3 Socket.IO 网站地址/路径/身份和默认游戏插件 WebSocket 链接。Linux-Windows 内部公网入口、固定路径和入口令牌由网页保存时自动生成，不需要后台手填。网页同步令牌仍只在 Linux `.env` 和 Windows 插件设置中配对。im3 按文档使用 `join_room` 的用户网站卡 `key` 和后台配置的 `type`，游戏执行链接按文档使用 `ws(s)://主机:端口/ws?apiKey=...`。
游戏 WebSocket 入口令牌和游戏插件 API Key 在网页数据库中加密保存；网页同步令牌只保留在 Linux 环境变量和 Windows 初始配置。用户网站卡密按群加密保存，Windows 收到已认证同步响应后自动写入运行配置，网页不会把凭据写入审计日志。首次建立 HTTPS 通联前，执行端仍必须获得网页地址、网页同步令牌和 Card Pepper，这是出站连接的安全引导信息。

多台 SCUM 服务器推荐直接在 Linux 网页的分群配置“游戏服务器”中维护，每个群可保存多条
插件 WebSocket 档案。网页数据库使用会话密钥加密保存 API Key，页面不回显明文；Windows 心跳拉取当前群
配置通过已认证同步下发到本地执行端。实际连接优先使用 Linux 分群配置档案，没有匹配时使用 Linux 页面的全局回退。多台 Windows 电脑则分别运行插件、使用各自生成的持久 `client_id`
连接同一个网页，在后台 WebSocket 设置中把最大执行端数量调到不小于在线电脑数。

Windows 网页同步会复用 HTTPS keep-alive 连接，配置保存后的唤醒请求不再重复建立 DNS、TCP 和 TLS
连接。在分群“游戏服务器”卡片中保存档案后，等待 Windows 插件完成一次配置同步，再填写档案 ID
并点击“检测 Windows 插件 WebSocket 连接”。网页只通过已认证控制 WebSocket 发起检测请求，插件
WebSocket 主机、端口和 API Key 由对应群的 Windows 执行端使用；Windows 只发送认证握手，不执行游戏指令。成功或失败结果会回到网页，失败时优先根据安全错误代码检查 Windows CMD 中的连接日志、插件端口、防火墙和 API Key。

## 已有功能

## 游戏连接操作

后台管理员在“游戏 WebSocket”页面配置 im3 Socket.IO 的 `server_url/socketio_path/bot_type`，以及默认游戏插件的完整 `ws(s)://主机:端口/ws?apiKey=...` 链接。网页会自动管理 Linux-Windows 内部入口，保存后点击检测，再等待 Windows 执行端在线。后台配置的是网站和执行端入口，不代替用户卡密。

用户端不填写 WebSocket 地址、`apiKey` 或同步令牌。主人生成的 `JG...` 游戏卡密只用于用户游戏心跳授权；用户在群详情页先输入 JG 卡，再输入小程序/WebSocket 网站发放的实际卡密（例如网站提供的 32 位十六进制卡），网页会向后台配置的网站提交 `join_room`，并要求明确的授权字段。当前公开 im3 只返回普通入房回执而不返回独立卡密授权结果，因此用户卡验证会失败且不会保存；要区分两个合法格式的卡密是否有效，必须由 im3 服务端返回允许/拒绝字段。

- 邮箱验证码注册、邮箱找回密码、账号密码登录、退出和修改密码
- 后台可配置任意标准 SMTP 邮箱（QQ、163、Gmail、Outlook 等），并发送测试邮件
- 客户可提交网页/同步/群管反馈；反馈先入库，后台可记录处理状态与意见，并异步邮件通知管理员
- 支持多个客户账号独立登录，登录会话和账号密码互相独立
- 一个账号可以登记多个已经在群聊完成授权的群
- 授权到期后仍可登录和查看，但配置只读；在群聊激活新卡密后自动恢复
- 网页开机/关机
- 入群审核、欢迎、黑名单、禁言、踢出、撤回、消息记录、签到统计、关键词过滤、文转图等功能配置
- Cron 定时播报、定时全体禁言/解禁
- 精确、模糊、正则自动回复
- 分群管理员和用户专属回复
- 网页数据库（MySQL/SQLite）配置快照与操作审计
- 分群娱乐支持自定义点歌候选等待选歌时间（10-3600 秒）
- 管理员后台统一配置音乐平台 Cookie、音乐名片签名网站和可选凭据；Cookie 与签名凭据加密保存并只随认证心跳传给 Windows，用户端只选择音乐平台和验证对应卡密
- 在目标分群的“服务器与数据库”中独立保存 FTP/FTPS 连接，再让服务器档案选择连接并浏览或选择 SQLite 数据库文件；管理员不提供公共连接表单
- CSRF 防护、签名会话、scrypt 密码哈希和登录频率限制
- 浏览器登录 Cookie 有效期可由 `JOKER_WEB_SESSION_MAX_AGE` 设置（300 秒至 30 天），也可在管理员后台“安全设置”中持久化调整；管理员和客户分别使用 `joker_admin_session`、`joker_user_session`，登录状态互不覆盖
- 独立管理员后台：用户、角色、账号状态、密码、分群、日志和全局配置管理

## 多用户与分群隔离

- MySQL 中每位客户对应一条独立用户记录，可以同时登录使用。
- 同一群配置更新按群行锁串行化；MySQL 连接池大小、临时连接数和等待时间可通过 `JOKER_WEB_DATABASE_POOL_*` 环境变量按服务器规格调整。
- 卡密激活记录决定账号可以登记的群，网页请求不能自行指定群号；账号身份由已验证邮箱决定。
- 每次查看和修改都会同时校验登录用户 ID 与群归属关系。
- 远程模式下网页先保存待同步版本，本地机器人拉取并合并到对应群配置；本地运行数据不会被网页覆盖。
- 同一账号可以管理多个自己的群；同一个群不能同时归属不同客户账号。
- 客户之间不能查看群列表、功能配置、定时任务、回复内容或操作日志。

## 外部业务连接（FTP 与数据库）

客户登录后进入目标分群的“服务器与数据库”，先维护独立 FTP 连接，再由服务器档案选择其中一条连接并配置数据库文件。一个群可保存多条 FTP 和多套服务器档案；保存并绑定后，页面只浏览当前服务器所选 FTP 的受限目录：

- FTP/FTPS：服务器 IP 或域名、端口（默认 `21`）、账号、密码、远程目录、TLS 和主动/被动模式。
- SQLite：从 FTP 目录选择 `.db`、`.sqlite` 或 `.sqlite3` 文件，也可以手动填写路径；“自动选择”只查找 `beijixiong.db`，不会猜测其他文件。SQLiteStudio 是打开 SQLite 文件的桌面工具，不是数据库连接类型。
- 旧版本保存的 MySQL 连接记录只保留兼容读取；新页面不再要求填写独立数据库服务器，统一从 FTP 选择 SQLite 文件。

这不是网页配置中心自己的数据库。网页主数据库仍由 `JOKER_WEB_DATABASE_URL` 和
`.env` 中的设置决定；外部业务连接只保存到 `portal_settings` 的
`external_integrations:user:<用户ID>` 配置项的 `ftp_connections[群号][FTP连接ID]` 与 `profiles[群号][服务器档案ID]` 下；服务器档案只保存 FTP 连接引用与数据库文件选择，后续业务只读取当前群选中的档案。群与群之间即使使用相同 ID 也完全隔离；旧版顶层 FTP/数据库记录不会再被任何分群继承或同步。启用某一连接时必须填写完整参数，
端口必须在 `1-65535` 范围内，数据库名称只允许字母、数字、下划线、点和短横线；FTP 路径不允许 `..` 穿越。

保存时密码会使用 `JOKER_WEB_SESSION_SECRET` 派生的 Fernet 密钥加密，模板、JSON 响应、
审计日志和 Windows 同步数据都不会返回明文；密码输入框留空表示保留已经保存的密码。
如果更换 `JOKER_WEB_SESSION_SECRET`，旧密码将无法解密，需要在此页面重新填写并保存。
页面提供“测试 FTP”“测试数据库”和“测试全部”按钮，测试使用独立线程、短连接和 10 秒网络超时，
不会长期占用网页进程的连接池。SQLite 测试只下载不超过 256 MB 的临时副本并在测试后删除，
不会把数据库文件保存到网页主数据库。MySQL 启用前请确认服务器允许网页主机访问对应端口，
且数据库账号具有所需权限。

## 1. 准备 MySQL

已有 MySQL 时，创建一个 UTF-8 数据库和专用账号即可：

```sql
CREATE DATABASE joker_portal CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'joker_web'@'127.0.0.1' IDENTIFIED BY '替换为强密码';
GRANT ALL PRIVILEGES ON joker_portal.* TO 'joker_web'@'127.0.0.1';
FLUSH PRIVILEGES;
```

也可以只用附带的 Compose 启动 MySQL。在执行前先设置两个密码环境变量：

```powershell
$env:JOKER_WEB_MYSQL_PASSWORD="替换为数据库用户强密码"
$env:JOKER_WEB_MYSQL_ROOT_PASSWORD="替换为数据库root强密码"
docker compose -f web_client/docker-compose.mysql.yml up -d
```

## 2. 安装和配置

远程模式的服务器网页可以使用独立 Python 虚拟环境；本地插件使用 AstrBot 当前 Python 环境。

```powershell
python -m pip install -r web_client/requirements.txt
Copy-Item web_client/.env.example web_client/.env
```

编辑 `web_client/.env`，至少修改：

- `JOKER_WEB_MYSQL_PASSWORD`
- `JOKER_WEB_SESSION_SECRET`
- `JOKER_WEB_CARD_PEPPER`
- `JOKER_WEB_DATA_MODE=remote`
- `JOKER_WEB_SYNC_TOKEN`
- `JOKER_WEB_ADMIN_PATH`（可选，默认 `/admin`；例如 `/control-panel`）
- `SCUM_RELAY_KEY`（中转站 `.env` 中 `RELAY_API_KEY` 的值）

`SCUM_RELAY_URL` 默认为 `https://ba.yooyun.top`。SCUM 服务器搜索和 ID 详情
只使用该中转站；`SCUM_RELAY_KEY` 只保存在 Linux 的 `web_client/.env`，不要写入
Windows 插件设置、网页前端或群配置。只开放关键词搜索和服务器 ID 详情查询，旧版 IP、
无关键词排行和资讯操作已从查询接口移除，不会切换到其他数据源。SCUM 关键词与在线查询
命令可在网页的分群设置中分别配置。

两个密钥应使用不同的随机值，可分别生成：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

共生成三个不同随机值：会话密钥、卡密 Pepper、同步令牌。服务器 `.env` 的 `JOKER_WEB_CARD_PEPPER` 和 `JOKER_WEB_SYNC_TOKEN` 还要分别填入本地 AstrBot 插件设置的“远程网页同步 -> 卡密指纹盐/同步令牌”。

本地插件设置：

```text
启用网页同步：是
网页地址：https://你的网页域名
同步令牌：与服务器 JOKER_WEB_SYNC_TOKEN 相同
卡密指纹盐：与服务器 JOKER_WEB_CARD_PEPPER 相同
同步间隔：30
```

保存后同步任务会自动启动或按新配置重建；若当前 AstrBot 版本没有触发配置热加载，再手动重载一次 Joker 插件。首次同步成功后，网页后台会出现群和脱敏授权状态。网页保存任何群配置后，电脑通常会在一个同步间隔内（默认 30 秒）自动拉取并应用。

## 3. 启动

首次使用先创建一个后台管理员：

```powershell
python -m web_client create-admin admin
```

命令会安全读取密码，不会把密码显示在终端或写入命令历史。已有同名账号会被提升为管理员并重置密码。

然后启动网页服务：

在 Joker 插件目录执行：

```powershell
python -m web_client
```

默认访问 `http://127.0.0.1:8765`。数据库表会在首次启动时自动创建。

管理员登录后可从左侧导航进入“管理后台”，可以：

- 查看、创建、启用或停用客户与管理员账号
- 提升/降低管理员角色，重置用户密码
- 远程模式仅查看本地同步的脱敏已使用卡密；生成和删除卡密在本地主人命令完成
- 在“邮箱设置”中配置 SMTP 服务器、端口、加密方式、授权码、发件人和反馈通知邮箱；SMTP 授权码会加密保存且不会显示
- 在“用户反馈”中查看客户意见、邮件投递结果、处理状态和处理意见
- 查看全部群归属、脱敏卡密、授权、开机和功能状态
- 进入任意有效授权群查看或修改分群配置
- 解除网页账号与群的绑定，同时保留机器人授权和分群数据
- 查看全站操作日志及登录来源 IP
- 本机直连模式可修改群管全局基础设置；远程模式必须在本地 AstrBot 插件设置中修改

后台不会显示或覆盖授权密钥、主人账号、数据库密码等敏感字段。不能停用或降级当前管理员自己，也不能移除最后一个有效管理员。

## 4. 客户注册流程

1. 管理员先在网页“邮箱设置”填写 SMTP 参数并发送测试邮件；QQ、163 等邮箱请使用 SMTP 授权码，不要使用网页登录密码。
2. 客户打开网页 `/register`，填写邮箱并获取验证码，设置网页账号和密码；登录页“忘记密码”可用同一邮箱验证码重置密码。
3. 机器人主人在 QQ 中生成卡密；客户在目标群发送 `/激活卡密 卡密内容`，由机器人完成群授权。
4. 客户登录网页，在“登记已授权群组”填写当前已激活卡密。网页自动识别群号，不允许客户手动指定或更换群号。
5. 授权续费更换卡密后，客户只需重新登记新卡密；邮箱、账号和已保存的分群配置不会改变。

网页不会激活、延长或减少授权，只读取群聊产生的授权记录。同一个群只能归属于一个网页客户账号。授权过期后，需要先回到群聊激活新卡密；已有网页账号不需要重新注册。若要给账号增加另一个群，也必须先在另一个群激活卡密，再到控制台登记该卡密。

当本地插件按过期清理策略同时删除某群授权记录和群配置时，下次同步会删除服务器上的配置投影与卡密指纹。网页账号、群归属关系和审计日志保留，但该群会显示无授权且不能编辑，直到本地重新授权并同步。

## 生产部署注意

- 在 Nginx、Caddy 等 HTTPS 反向代理之后运行，并将 `JOKER_WEB_SECURE_COOKIES=true`。
- 生产服务保持 `JOKER_WEB_HOST=127.0.0.1`；反向代理需保留 `Host`、`X-Forwarded-Host` 和 HTTPS 协议头，不能将网页直接公开在 `8765`。Linux 安装会自动写入 `JOKER_WEB_PUBLIC_ORIGIN=https://你的域名`，仅在 CDN 丢失转发头时供 Windows 游戏 WebSocket 同步地址回退使用，无需在网页或 Windows 手工填写。宝塔/EdgeOne 的完整 Nginx 与 525 排查步骤见 [`deploy/linux/README.md`](../deploy/linux/README.md)。
- 远程模式不上传 `plugin_data`，网页只通过带 Bearer 令牌的 HTTPS 同步 API 接收脱敏投影。
- 不要把 `web_client/.env` 提交到 Git 或发送给客户。
- 只向外开放 HTTPS 端口，不要把 MySQL 端口暴露到公网。
- 本地 `data/plugin_data/auth` 与 `data/plugin_data/qqadmin` 才是权威数据，必须定期备份；MySQL 也应备份账号和网页审计数据。
- 同步令牌和 Card Pepper 泄露后应同时在服务器和本地更换；两端值不一致会导致同步或群卡密登记失败。不要更换会话密钥；SMTP 授权码使用该密钥加密，若确实更换需要在后台重新填写 SMTP 授权码。

# Route namespaces

- Administrator sign-in: `JOKER_WEB_ADMIN_PATH/login`; administrator home and settings use
  `JOKER_WEB_ADMIN_PATH/...`（默认 `/admin`）。访问旧 `/admin` 会跳转到自定义路径。
- Customer sign-in: `/user/login`; registration and customer settings use `/user/...`.
- `/login`, `/register`, `/forgot-password`, `/dashboard`, and `/groups/...` remain compatibility routes. Successful sign-in always redirects by the account role.
- In remote mode, `User`, `LoginAttempt`, `AuditLog`, verification codes, and group access records are stored in the configured Linux MySQL database. Passwords are stored only as hashes and the session cookie contains only the user id.
