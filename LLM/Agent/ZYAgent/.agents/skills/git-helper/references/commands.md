# Git 常用命令速查（配合 RunCli 的 args 数组使用）

| 目的 | command | args |
|---|---|---|
| 状态速览 | git | `["status", "--porcelain"]` |
| 完整状态 | git | `["status"]` |
| 工作区差异 | git | `["diff"]` |
| 暂存区差异 | git | `["diff", "--staged"]` |
| 最近提交 | git | `["log", "--oneline", "-5"]` |
| 提交历史（含文件） | git | `["log", "--stat", "-3"]` |
| 当前分支 | git | `["branch", "--show-current"]` |
| 列出分支 | git | `["branch", "-a"]` |
| 暂存指定文件 | git | `["add", "path/to/file"]` |
| 创建提交 | git | `["commit", "-m", "说明文字"]` |

提示：

- `RunCli` 不经过 shell，不支持 `|`、`>` 等管道/重定向语法；需要管道时改用等价的 git 选项（如 `--porcelain`、`--oneline`）；
- 输出过长会被自动截断（截断处注明原始长度），如需定位特定内容可加 `-n`、路径过滤等参数缩小输出。
