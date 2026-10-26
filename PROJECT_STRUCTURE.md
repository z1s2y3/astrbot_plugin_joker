# Joker Project Structure

这是当前项目的目录参考。Linux 服务器只部署网页配置中心，Windows 电脑上的
AstrBot/Joker 才是群功能和本地数据的执行端。不要把下面的密钥、密码或运行数据
写入部署包。

## Repository (Windows plugin)

```text
astrbot_plugin_joker/
├── main.py                         # AstrBot 插件入口、命令和本地同步任务
├── auth/                           # 群聊授权卡密；原始卡密只留在 Windows
├── qqadmin/                        # 群管功能、分群配置和 Windows -> Linux 主动同步
│   ├── group_config.py             # 本地分群配置读写
│   ├── web_sync.py                 # HTTPS 主动 exchange/ACK 客户端
│   ├── portal_binding.py           # 网页地址、令牌和 Card Pepper 校验
│   └── console_log.py              # 脱敏 CMD/消息收发事件缓存
├── scum/                           # SCUM 查询功能
├── data/plugin_data/               # Windows 运行数据（禁止上传到 Linux）
│   ├── auth/                       # 授权记录和主人数据
│   └── qqadmin/                    # 群配置、缓存、日志和图片缓存
├── web_client/                     # Linux 网页应用源码（也可本地调试）
│   ├── app.py                      # FastAPI 路由、管理员/客户入口和同步 API
│   ├── services.py                 # 用户、群归属、卡密投影和配置保存
│   ├── remote_store.py             # MySQL 快照、修订号、刷新请求和 ACK
│   ├── sync_projection.py          # 只同步客户可编辑字段的投影/合并规则
│   ├── integration_service.py      # 用户级 FTP/业务 MySQL 配置、加密和连接测试
│   ├── release_service.py          # 读取安装器写入的当前网页版本标记
│   ├── self_update.py              # 后台更新上传暂存，不执行 ZIP
│   ├── cookie_service.py           # Cookie 有效期的数据库持久化
│   ├── session_middleware.py       # 按管理员/客户隔离且支持运行时有效期的签名会话
│   ├── templates/admin_*.html      # 管理员页面和独立导航
│   ├── templates/user_*.html       # 客户页面和独立导航
│   └── static/                     # CSS/JS；静态版本号随文件内容变化
├── deploy/linux/                   # Linux 安装、Nginx 和 root 更新组件
│   ├── quick_install.sh            # 首次安装和手动升级
│   ├── install_web.sh              # Python 虚拟环境与依赖
│   ├── joker-web-secure-stage.py   # root 安全接收、解压和路径检查
│   ├── joker-web-apply-update.sh   # 清单校验后原子升级并回滚
│   ├── joker-web-scan-updates.sh   # root 更新任务监听器
│   ├── verify_update_manifest.py   # ZIP/解压目录 SHA-256 清单校验
│   └── nginx-web.conf.example      # 按实际域名渲染的反代模板
├── deploy/build_release.ps1       # 同版本生成 Windows + Linux 双端发布包
├── deploy/build_baota_package.ps1 # 单独生成只含 Linux 网页的发布包
├── 堡塔/                           # 当前最新 ZIP、.sha256 和简要部署说明
└── PROJECT_STRUCTURE.md             # 本文件
```

## Linux deployment

默认安装目录如下。域名和证书路径由安装时检测或保存的配置决定，不写死在程序
代码中。

```text
/www/wwwroot/joker-web/
├── web_client/                     # 网页应用和模板
├── deploy/linux/                   # 安装后保留的诊断/更新脚本
├── feature_catalog.py              # 网页功能字段目录
├── web_client/.env                 # 数据库、会话密钥、Card Pepper、同步令牌
├── .joker-web-release.json         # 健康检查通过后写入的当前网页版本（无密钥）
└── .web-venv/                      # 独立 Python 环境

/www/wwwroot/joker-install/
├── incoming/                       # joker-web 用户只能写入待更新任务
├── processing/                     # root 私有校验和解压目录
└── last-status.json                # 管理员更新页面读取的脱敏状态

/etc/joker-web-updater.conf        # root 保存的安装目录、域名和更新参数
/root/joker-web-update-key.txt     # root-only 后台更新授权码
/root/joker-web-local-sync.txt     # 给 Windows 插件填写的同步信息
/var/log/joker-web-updates/        # root 更新日志
/www/server/panel/vhost/nginx/     # 宝塔实际 vhost（由 joker-webctl 应用）
```

## Data flow

```text
Windows AstrBot
  └─ HTTPS POST /api/sync/v1/exchange
       ├─ 上传脱敏授权摘要、分群配置投影和可选 CMD 事件
       ├─ 拉取 Linux 网页保存的 pending revision
       └─ HTTPS POST /api/sync/v1/ack 之后才标记为已同步

Linux portal
  ├─ 管理员：/admin/*（用户、卡密概览、更新、日志、安全和全局设置）
  ├─ 客户：/user/*（自己的群、配置、反馈和账号安全）
  ├─ MySQL：账号、群归属、配置投影、修订号、反馈和审计日志
  └─ 外部连接：每个用户独立保存的 FTP 与业务 MySQL 参数，供后续签到等业务使用
```

管理员和客户入口使用不同的签名 Cookie（`joker_admin_session`、
`joker_user_session`），不会因为在另一入口登录而互相覆盖；两者的有效期
由管理员“安全设置”统一调整并保存到 `portal_settings`。

网页不会反向连接 Windows，也不会通过 SSH 使用服务器账号密码。Windows 电脑
离线时网页保存的是 pending 配置；电脑下次联网会主动补传、应用并 ACK。同步失败
时检查网页域名 HTTPS、反向代理、同步令牌和 Card Pepper，525 表示 CDN 回源
TLS 问题，不是卡密格式错误。

客户可从“我的 FTP 与数据库”页面保存自己后续业务使用的 FTP/FTPS 和业务 MySQL 连接。
这些设置独立于网页自身的 `JOKER_WEB_DATABASE_URL`，不会改变账号、群配置和审计日志
所使用的主数据库。FTP、FTPS 及业务数据库密码使用现有 `JOKER_WEB_SESSION_SECRET`
派生的 Fernet 密钥加密后写入 `portal_settings`，页面只显示“已配置/未配置”；密码
不会写入审计日志、Windows 同步投影或部署包。当前连接测试支持 FTP/FTPS 和 MySQL，
后续签到等功能可通过服务层创建短连接并在操作完成后释放。

## Package boundary

`堡塔/Joker_Baota_Remote_v*.zip` 只包含 `install.sh`、`update-manifest.json`、
`01_plugin_source/astrbot_plugin_joker/web_client`、`feature_catalog.py` 和
`deploy/linux`。以下内容永远不应放入 ZIP：

- `web_client/.env`、网页主数据库密码、外部 FTP/业务数据库密码、同步令牌、Card Pepper 和更新授权码；
- `data/plugin_data/`、QQ/NapCat 登录资料、授权原文、群消息缓存和图片缓存；
- Windows 上的 AstrBot 可执行文件、浏览器资料、`.venv`、`__pycache__` 和旧 ZIP。

校验发布包：

```bash
python3 deploy/linux/verify_update_manifest.py \
  --zip Joker_Baota_Remote_vX.Y.Z-all-in-one.zip \
  --sha256 Joker_Baota_Remote_vX.Y.Z-all-in-one.zip.sha256
```
