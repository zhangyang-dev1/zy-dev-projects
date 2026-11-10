---
name: git-helper
description: Git 仓库操作助手：通过 RunCli 安全执行 git 命令——检查状态、查看差异、创建提交与分支。当用户要求查看或管理本地 Git 仓库（status/diff/commit/branch/log 等）时使用。
---

# Git 助手（git-helper）

你正在使用 **git-helper** 技能。所有 git 操作都通过 `RunCli` 工具执行
（例如 `command="git", args=["status","--porcelain"]`）。

## 安全规则（必须遵守）

1. 每次变更前先 `git status` + `git diff` 了解现状；
2. **禁止**执行破坏性命令：`git push --force`、`git reset --hard`、`git clean -f`、
   `git branch -D` 等——如用户明确要求，先复述风险并等待确认；
3. 提交信息用现在时祈使句，一行不超过 50 个字符；
4. 命令失败（退出码非 0）时读取 stderr 判断原因，向用户解释后再决定下一步；
5. 不要替用户执行 `push`，除非用户明确要求。

## 推荐流程

1. `git status --porcelain` —— 快速查看变更（无输出 = 工作区干净）；
2. `git diff` / `git diff --staged` —— 复核具体改动内容；
3. 需要提交时：`git add <文件>` → `git commit -m "..."`（只 add 用户认可的文件）；
4. `git log --oneline -5` —— 确认提交结果并向用户汇报。

> 常用命令速查见 `references/commands.md`；需要时用
> `loadSkill(filename="commands.md")` 按需加载（文件名在本技能内唯一，可直接传文件名）。
