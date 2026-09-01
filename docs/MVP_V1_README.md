# MVP v1 薄纵切原型

这是一个实验性原型，用来验证最小数据流，不代表正式评价 Prompt。

## 运行

先导入 TXT，并选择目标专家：

```powershell
python mvp_v1.py ingest "C:\path\to\conversation.txt" --out mvp_runs --target-speaker "AI猎头"
```

命令会生成：

- 原始 TXT 快照和 `sha256`
- `utterances.json`
- `evidence.json`
- `candidates.json`
- `task.json`

检查候选后，执行最小人工批准：

```powershell
python mvp_v1.py approve "mvp_runs\task_xxxxxxxxxxxxxxxx" --reviewer "operator"
```

批准后会生成 `knowledge-v1.json` 和 `release/manifest.json`、`release/PROMPT.prototype.md`。

原型刻意保守：没有目标专家选择就不能批准；没有证据回链就不会产生可批准候选；发布文件带有 `prototype_only=true`，不能当作生产评价规则。
