# EdgeOne 与域名跳转故障排查

## 现象与判断

如果源站本机检查正常，而公网请求返回：

```text
HTTP/2 525
server: TencentEdgeOne
```

这不是 FastAPI 路由返回的跳转，而是 EdgeOne 连接源站时没有完成 TLS 握手。修改 `JOKER_WEB_HOST` 不能修复 525；该变量只控制 Uvicorn 的监听地址。

如果访问 `https://你的域名/` 被跳到源站 IP，先在源站执行：

```bash
joker-webctl --apply-nginx
curl -skD - -o /dev/null --max-redirs 0 \
  --resolve 你的域名:443:127.0.0.1 https://你的域名/
curl -skD - -o /dev/null --max-redirs 0 https://你的域名/
```

第一条 `curl` 只测本机 Nginx，根路径应返回相对 `Location: /user`，绝不能是 `Location: http://源站IP/...`。如果本机正常而公网仍跳 IP，跳转来自 EdgeOne 的 URL 重定向/缓存规则；删除该规则、清除重定向缓存，并把回源 Host 与 SNI 都设为网页域名。若本机也跳 IP，说明旧宝塔 vhost 仍在生效；`--apply-nginx` 会备份、替换并校验默认宝塔同域名 vhost，若报告重复域名则先删除或更正另一个旧站点配置。

## 源站部署要求

1. Uvicorn 只监听 `127.0.0.1:8765`，不要把 8765 暴露到公网。
2. Nginx 监听 80/443，并使用 `nginx-web.conf.example` 中的域名虚拟主机。
3. 443 的证书 `fullchain.pem` 和私钥必须匹配，并且 SAN 包含你安装时填写的网页域名。
4. 源站防火墙/安全组放行 80、443；不需要放行 8765。
5. 80 端口只把请求规范化到安装器生成配置中的网页域名，不要使用 `$host` 生成重定向地址。

## EdgeOne 控制台设置

在站点的源站配置中使用以下值：

| 项目 | 值 |
| --- | --- |
| 源站地址 | 源站公网 IPv4（不要填你的网页域名，避免回源到 EdgeOne 自身） |
| 回源协议 | HTTPS |
| 回源端口 | 443 |
| 回源 Host | 你安装时填写的网页域名 |
| SNI/证书校验域名 | 你安装时填写的网页域名 |
| 客户端到 EdgeOne | HTTPS；开启强制 HTTPS 即可 |

如果 EdgeOne 控制台分别提供“回源 Host”和“SNI”字段，两者都填安装时填写的网页域名。不要把源站 Host 改成 IP，也不要在源站证书校验关闭时长期运行；临时关闭校验只能用于定位证书链问题。

## 两阶段验证

在源站上先验证 Nginx 和证书（把 `ORIGIN_IP` 替换为源站公网 IP）：

```bash
nginx -t
curl --fail --silent --show-error --resolve 你的域名:443:ORIGIN_IP \
  https://你的域名/health/live
```

应得到 `{"status":"ok"}`。随后从任意公网机器验证：

```bash
curl -I https://你的域名/health/live
```

如果第一步成功、第二步仍为 525，问题只在 EdgeOne 源站地址、SNI/Host、证书校验或源站安全组；如果第一步失败，先修复源站 Nginx/证书，再检查 CDN。

修复后建议恢复并确认：

```ini
JOKER_WEB_HOST=127.0.0.1
JOKER_WEB_PORT=8765
```

然后重启 `joker-web`，并再次确认 `ss -lntp | grep 8765` 只显示 `127.0.0.1:8765`。
