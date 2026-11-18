# 发布与恢复

## 发布前

1. 使用 Python 3.11+ 和 Node 22 运行 `npm ci --prefix web`、`npm test --prefix web`、`npm run build --prefix web`、`python -m unittest discover -s tests -q`。
2. 记录 Git commit 和 `web/dist/index.html` 中的 JS 文件名，保留前一个版本的代码、首页和哈希静态资源。不要提交 OSS 凭证或 runtime 数据。
3. 在服务器确认 `picture-workflow.service` 没有运行中的批次；重启会把未完成批次标为失败。保留 `runtime`、OSS 配置和服务环境。
4. 当前部署使用单个 Uvicorn 主进程和有界计算进程池，不能直接增加 Uvicorn workers；缓存和限额目前是进程内的。

## 发布检查

先上传新哈希静态资源，再更新后端、重启服务并检查本机入口，最后切换首页。不要在原地清空线上 assets。

```bash
sudo systemctl restart picture-workflow.service
python3 tools/verify_service.py http://127.0.0.1:8000 --expected-asset index-本次构建.js
python3 tools/verify_service.py https://cwb.ainiankj.com --expected-asset index-本次构建.js
sudo journalctl -u picture-workflow.service -n 100 --no-pager
```

检查脚本全程只读，检查失败退出码为 1。另用独立测试批次检查实际合成，避免用用户正在处理的素材作发布测试。发布失败时切回已保留的上一版本代码和首页后重启，保留 runtime 和 OSS 数据。回滚后再次运行同一检查脚本并指定上一版本 JS 文件名。

## 监控接入

在现有监控平台对公网 `/api/health` 和 `/api/ready` 配置定时检测，连续失败后告警。另在阿里云控制台配置实例到期/欠费、磁盘余量、内存、CPU 和证书到期通知。应用就绪检查不访问 OSS，OSS 故障通过模板/空间站接口和服务日志识别。

SSH 与网站同时不通时，通过阿里云控制台检查 ECS 状态、公网 IP 和安全组；进程内重启不能恢复云实例停机或云网络拦截。本文没有自动创建云告警或修改云控制台设置。
