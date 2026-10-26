# 宝塔远程网页部署资源

此目录对应“Windows 本地 AstrBot/NapCat 执行群功能，Linux/宝塔仅运行网页配置中心”的推荐架构。

首次部署可先在本机浏览器打开包内的 deploy/linux/setup.html，填写 MySQL、网页端口、管理员和同步参数，下载 joker-install.env 后按页面命令执行安装。网页安装器会生成并保护正式 .env；升级已有网页时保留现有配置，不要重新生成或覆盖 .env。

- `install_web.sh`：创建独立 `.web-venv`、安装依赖、生成但不覆盖 `.env`、执行导入前语法检查。
- `quick_install.sh`：宝塔新服务器交互式一键安装源文件；发布包中会命名为根目录 `install.sh`。
- `health_check.sh`：检查 `.env` 占位符和本机 `/health`。
- `joker-web.service.example`：systemd 常驻模板；强制网页进程只监听 `127.0.0.1:8765`。
- `nginx-web.conf.example`：不绑定域名的 Nginx HTTPS 模板。安装器会按实际域名生成可直接使用的反代配置，包含 ACME 例外、HTTP→HTTPS 跳转和更新包上传限制。
- `render_nginx_config.py`：只由安装器调用，将模板中的安全占位符替换为实际域名、证书目录、站点目录和网页安装目录。
- `joker-webctl --apply-nginx`：管理员明确执行后，备份并替换对应的宝塔 vhost；只有 `nginx -t` 成功且重载成功才保留新配置。

服务器不需要安装或运行 AstrBot、Joker 或 NapCat，也不需要上传本地 `plugin_data`。网页配置先写入 MySQL，电脑上的机器人通过 HTTPS 主动访问 `/api/sync/v1/*` 拉取并应用。电脑关闭时网页保存的配置会等待同步；群机器人不会在服务器上运行。

网页同步按“网页登录账号绑定的群”划分范围。Windows 启动后第一轮只协商范围，不会先上传所有本地群的完整配置、运行摘要或消息记录；Linux 返回绑定群后，Windows 只持续同步这些群。未绑定群只保留首次卡密登记所需的脱敏授权握手（不含卡密原文），不会参与配置、运行数据、消息记录或网页修改同步。解除绑定后会在下一次心跳停止该群持续通联，重新绑定后自动补传当前配置。

上传的 `Joker_Baota_Remote_v*.zip` 仅更新这个 Linux 网页端，绝不会包含或覆盖 Windows 的 `main.py`、`qqadmin`、`auth`、`scum`、NapCat 或 `plugin_data`。Windows 端机器人功能需要单独更新本地插件并重载 AstrBot。发布时优先执行 `deploy/build_release.ps1 -Version X.Y.Z`，它会用同一版本同时生成 Linux 网页包和 `Joker_Baota_Remote_v*-windows-plugin.zip`，并输出联合校验清单；也可以单独执行两个构建脚本。同步是 Windows 主动发起的 HTTPS 请求，不需要给电脑开放端口。

最简单安装方式：在宝塔创建 MySQL 数据库，上传并解压唯一的发布 ZIP，然后进入解压目录执行 `bash install.sh`。脚本会完成文件安装、虚拟环境、随机密钥、`.env`、数据库初始化、管理员账号、`joker-webctl` 自检命令和 systemd 开机自启。旧版网页若安装在 AstrBot 目录，脚本会保留原配置并迁移到独立的 `/www/wwwroot/joker-web`；不会删除旧目录。

同一个包用于首次安装和升级：首次执行 `bash install.sh`；首次无交互安装必须提供 `JOKER_PORTAL_DOMAIN=你的域名`。已有网页执行 `bash install.sh --non-interactive` 即会沿用服务器保存的域名；只有更换域名时才额外传入新的 `JOKER_PORTAL_DOMAIN=新域名`。首次若需完全无交互，可提供 `JOKER_MYSQL_HOST`、`JOKER_MYSQL_PORT`、`JOKER_MYSQL_DATABASE`、`JOKER_MYSQL_USER`、`JOKER_MYSQL_PASSWORD`、`JOKER_ADMIN_USER`、`JOKER_ADMIN_PASSWORD` 后加 `--non-interactive`。安装完成后可执行 `joker-webctl --check` 检查服务，执行 `joker-webctl --show-sync` 显示 Windows 同步参数。

手动升级时必须把 ZIP 解压到独立目录，不能解压到运行中的 `/www/wwwroot/joker-web`：

```bash
UPDATE_DIR="$(mktemp -d /tmp/joker-web-update.XXXXXX)"
unzip -q Joker_Baota_Remote_vX.Y.Z-all-in-one.zip -d "$UPDATE_DIR"
bash "$UPDATE_DIR/install.sh" --non-interactive
rm -rf -- "$UPDATE_DIR"
```

安装过当前版本后，也可以在宝塔终端直接更新 ZIP，不需要手动解压：

```bash
joker-webctl --update /www1/joker-install/Joker_Baota_Remote_vX.Y.Z-all-in-one.zip
joker-webctl --show-version
```

如果服务器存在独立数据盘 `/www1`，当前安装器会优先使用 `/www1/joker-install` 保存后台上传包和更新任务；上面的旧路径仍兼容旧服务器。网页后台显示的实际上传目录会以服务器检测结果为准，手动更新时也建议使用 `joker-webctl --update /www1/joker-install/Joker_Baota_Remote_vX.Y.Z-all-in-one.zip`。该命令只允许 root/宝塔终端执行，会在临时目录校验 ZIP 清单、路径、符号链接和解压大小，然后调用同一个原子升级安装器；临时目录会在成功或失败时清理。

升级会在旁路目录完成依赖和健康检查，最后保持 `/www/wwwroot/joker-web` 这个稳定路径并清理成功升级留下的旧发布目录；不会把 `01_plugin_source` 嵌套进运行目录。更新完成后默认只保留最近 2 个可回滚网页版本和最近 3 个 `.env` 备份，避免每次 Linux 更新都在磁盘残留旧数据；可在执行前设置 `JOKER_RELEASE_BACKUPS_KEEP=0..20` 调整网页版本备份数量。

每次健康检查成功后，安装器会在运行目录写入不含密钥的 `.joker-web-release.json`，记录版本、部署包文件名、清单哈希和安装时间。管理员后台“网页更新”页读取该标记显示当前实际运行版本，并单独显示更新包清单中的目标版本；因此不会把“上传了哪个 ZIP”误认为“当前已经运行哪个版本”。

若首次安装在 `[5/8]` 报 MySQL 1045（用户被拒绝），先在宝塔核对数据库用户密码、主机匹配项和目标数据库权限，再使用当前包重新执行 `bash install.sh`。安装器会把未完成配置备份到 `/root/joker-web.env.failed.<时间>`，仅在新的 MySQL 参数验证成功后写入，并保留既有会话密钥、卡密指纹盐和同步令牌；不要删除数据库或把密码发送给任何人。

证书签发、续签或更换域名后，执行 `joker-webctl --render-nginx` 可按服务器保存的实际域名和宝塔当前证书重新生成 Nginx 配置；无需手工编辑或在脚本中写死域名。要让配置真正成为正在使用的站点配置，执行 `joker-webctl --apply-nginx`；该命令会先备份 `/root/joker-web-nginx-backups/`，发现 Nginx 校验失败或同域名 vhost 冲突时自动恢复。

安装末尾会生成 `/root/joker-web-local-sync.txt`。在电脑端执行 AstrBot 的 Joker 插件时，只需把该文件显示的网页地址、同步令牌、卡密指纹盐和同步间隔填入插件设置，然后开启“远程网页同步”。电脑使用 HTTPS 主动拉取配置，因此不用在家用电脑开放端口。

### SCUM 查询

SCUM 查询由 Windows 插件通过 `scum.query_relay_url` 调用本网页的 HTTPS 同步接口。中转接口必须与 Windows 使用相同的网页同步令牌、Card Pepper 和机器人客户端绑定。Linux 网页端只通过 `web_client/.env` 中的 `SCUM_RELAY_KEY` 调用 BattleMetrics 中转站；该值对应中转站配置的 `RELAY_API_KEY`。只启用关键词搜索和服务器 ID 详情，群内关键词与在线指令可在网页分群设置中自定义。更新网页后，Windows 插件也需要更新并重载；无需部署独立的 8766 服务。

新服务器，或从 v1.10.2 及更早版本升级时，必须先手动解压当前完整部署包并执行一次 `bash install.sh`；旧版没有当前版本需要的 root 安全更新接收器，不能先从旧网页后台上传升级。该次安装会创建根目录授权码 `/root/joker-web-update-key.txt` 与独立更新任务。之后管理员可在网页“更新”页上传后续部署 ZIP 包、输入该授权码并点击确认；网页进程只负责限制上传大小、临时保存并在响应发出后排队，不会在浏览器请求中完整校验或解压 ZIP。真正的 ZIP 路径、符号链接、大小、清单哈希验证和安装均由独立 root systemd 任务在受保护目录中完成。授权码不会保存在 MySQL、网页日志或部署包中。手动更新始终可用：解压部署包后执行 `bash install.sh`。

网页更新的上传队列默认使用 `/www1/joker-install`（没有 `/www1` 时回退到旧路径 `/www/wwwroot/joker-install`）：上传文件先进入 `incoming/`，root worker 校验后临时复制为带任务 ID 的受控 ZIP 放在该目录根部，再从校验后的任务 payload 执行 `install.sh`。安装成功或失败都会删除本次 ZIP 和 `processing/<任务ID>`；异常中断留下的上传文件和任务目录会在更新前自动清理。每次成功或失败都会写入该目录的 `update-history.json`，后台“更新”和“更新日志”页面会显示最近 30 次部署结果。更新不会替换或重建 `joker-install`，也不会改变现有 `/www/wwwroot/joker-web` 项目路径。更新过程日志保留在 `/var/log/joker-web-updates/<任务ID>.log`，当前状态在对应更新目录的 `last-status.json`。

部署包内的 `install.sh` 和其他 shell 脚本必须使用 LF 换行。root 校验器会在停止旧服务前拒绝 CRLF 包，并把具体原因写入 `last-status.json`，避免出现 `set: pipefail\r` 后才发现网页返回 502。

分群页面错误中的“请求编号”用于定位网页日志。服务器上执行 `journalctl -u joker-web -n 200 --no-pager -o cat` 查看最近日志，或执行 `journalctl -u joker-web -f` 实时跟踪；按编号精确筛选可用：`REQ=751f979ae5fb444f2; journalctl -u joker-web --no-pager -o cat | grep -A30 -B5 "$REQ"`。分群页会记录认证、群权限、配置投影、同步状态和模板渲染阶段；反向代理只会转发响应，页面不会把数据库密码、同步令牌或配置内容显示到浏览器。

上传包只应包含 `01_plugin_source` 和部署说明。`02_existing_data`、`.env`、浏览器资料、群消息记录、授权记录和本地备份都不能上传到服务器。

## Nginx、HTTPS 与 EdgeOne

网页进程不是公网服务。`joker-web.service` 和 `.env` 都应保持
`JOKER_WEB_HOST=127.0.0.1`、`JOKER_WEB_PORT=8765`；不要在防火墙或宝塔中向公网开放 `8765`。确认方式：

```bash
ss -ltnp | grep 8765
curl -fsS http://127.0.0.1:8765/health/live
```

第一条命令应只出现 `127.0.0.1:8765`（或 `[::1]:8765`，如果自行启用了 IPv6 回环），而不是 `0.0.0.0:8765`。

安装器会在默认安装目录生成 `/www/wwwroot/joker-web/deploy/linux/nginx-web-你的域名.conf`（如自定义 `JOKER_INSTALL_DIR`，则位于对应安装目录）。如果尚未签发 SSL，这是一份只开放 ACME 验证的 HTTP 引导配置；将它应用到宝塔站点并签发证书后，执行 `joker-webctl --apply-nginx` 生成并直接应用 HTTPS 反代配置。证书已存在时也可直接执行该命令。它只允许替换 `/www/server/panel/vhost/nginx/<域名>.conf`，会备份旧文件、拒绝同域名重复 vhost，并在 `nginx -t` 或重载失败时恢复。若需要只生成不应用，继续使用 `joker-webctl --render-nginx`：

```bash
joker-webctl --apply-nginx
```

WebSocket 入口必须经过这一步才会真正进入宝塔正在使用的 vhost。仅解压或升级网页包不会替换现有 vhost。升级后如果后台“检测 Windows 入口”返回 404，执行：

```bash
joker-webctl --apply-nginx
systemctl reload nginx
curl -fsS http://127.0.0.1:8765/health/ready
```

然后确认宝塔站点的 HTTPS vhost 同时包含精确路径 `/api/sync/v1/ws` 和 `/api/game/v1/ws`，并保留 `proxy_http_version 1.1`、`Upgrade`、`Connection "upgrade"`。浏览器能打开网页不代表 WebSocket 反代已经生效。

## Upgrade safety

The live project path remains `/www/wwwroot/joker-web` (or the value saved in
`JOKER_INSTALL_DIR`). An update is unpacked into a sibling `.joker-web-stage.*`
directory, verified, and atomically switched into that stable path. The prior
release is first moved to a versioned rollback directory, then to
`.joker-web-backups/joker-web-v<previous-version>-<timestamp>-<pid>` only after
the new service passes `/health/ready`; a failed switch automatically restores
the previous directory. Older installations without a release marker use the
`unknown` label. Orphaned stage/rollback directories are archived after a
healthy update, and the oldest managed backups are pruned. The installer never
removes arbitrary directories in the backup folder.

`joker-webctl` is installed outside the project tree and reads the saved target
path, so it continues to work after the web updater removes its temporary
payload. `joker-webctl --show-version` and the administrator update page read
the installer-written `.joker-web-release.json` marker from the running
release, rather than the name of an uploaded ZIP.

模板本身不含任何固定公网域名。渲染后的 80 端口仅保留 ACME 验证，其余请求固定跳转到本次安装所填的公开域名；443 端也只向网页传递该域名作为 `Host` 和 `X-Forwarded-Host`，避免 CDN 的源站 IP Host 把用户重定向到 IP。默认使用宝塔路径 `/www/wwwroot/你的域名` 与 `/www/server/panel/vhost/cert/你的域名`；若你的宝塔路径不同，安装时可传 `JOKER_NGINX_SITE_ROOT` 与 `JOKER_NGINX_CERT_DIR`。更新 ZIP 本体上限为 80 MiB；Nginx 设为 82 MiB，只是为 multipart 表单边界预留空间，超过应用上限仍由网页拒绝。

EdgeOne 的 **525** 表示 EdgeOne 到源站的 TLS 握手失败，发生在 FastAPI/Joker 代码之前，不能靠网页代码修复。按以下顺序排查：

1. 在源站执行 `nginx -t` 并重载，确认 443 的 `server_name`、证书链和私钥路径正确。
2. 从源站或可信机器验证 SNI：`openssl s_client -connect 源站IP:443 -servername 你的域名`；证书 SAN 必须包含该域名，且 TLS 1.2/1.3 可协商。
3. 在 EdgeOne 源站设置中使用 HTTPS、端口 443，并将回源 Host/SNI 设置为网页域名；不要把回源 Host 改成 IP。
4. 使用 Full/Strict 一类的 HTTPS 回源模式，确保源站防火墙允许 EdgeOne 回源到 443。不要使用仅 HTTP 的回源方式。
5. 直连验证通过后再开启 CDN：`curl --resolve 你的域名:443:源站IP https://你的域名/health/live`。若这一步失败，先修复源站证书或 Nginx，再检查 EdgeOne 控制台。
