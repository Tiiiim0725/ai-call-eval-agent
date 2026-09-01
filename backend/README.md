# 后端运行说明

开发服务监听 `127.0.0.1:8898`，仅供同机的 `127.0.0.1:8897`/`localhost:8897` 前端访问。当前不是多人生产部署，不把 localhost 当成用户认证；若要开放给其他机器，必须先增加会话认证和任务授权。

API Key 不写入项目文件。启动后端前在当前终端设置环境变量：

```powershell
$env:AI_CALL_EVAL_API_KEY = '新 Key'
python app.py
```

网页设置页输入的新 Key 只保存在当前后端进程，重启后失效。允许的前端 Origin 可通过 `AI_CALL_EVAL_ALLOWED_ORIGINS` 调整，多个值使用逗号分隔。
