# CodeSync

基于 rsync 的代码仓库同步 Web 管理工具。

## 快速开始

```bash
# 安装依赖
pip install flask

# 启动
python app.py

# 打开浏览器访问
open http://localhost:7788
```

## 功能

- **多服务器管理** — 保存多台服务器的 SSH 配置（IP、端口、用户名、密钥路径）
- **多仓库管理** — 配置多个本地仓库与远程路径的映射关系
- **一键同步** — 选择仓库和目标服务器，实时查看 rsync 输出流
- **同步选项** — 支持 `--delete`、`--dry-run` 预演、传输压缩、自动读取 `.gitignore` 排除规则
- **批量同步** — 一次同步所有仓库到指定服务器
- **脚本生成** — 根据配置生成可直接使用的 shell 脚本，支持下载
- **历史记录** — 保存最近 50 条同步记录

## 配置文件

配置自动保存在 `~/.codesync/config.json`，可手动编辑：

```json
{
  "servers": [
    {
      "id": "...",
      "name": "生产服务器",
      "host": "10.0.0.1",
      "port": 22,
      "user": "ubuntu",
      "key": "~/.ssh/prod_rsa"
    }
  ],
  "repos": [
    {
      "id": "...",
      "name": "my-project",
      "local": "~/projects/my-project",
      "remote": "/opt/my-project",
      "excludes": ["*.log", ".env", "node_modules/"]
    }
  ]
}
```

## 前提条件

- Python 3.8+
- `rsync` 已安装（macOS/Linux 自带，Windows 需要 WSL 或 Cygwin）
- 已配置 SSH 免密登录（推荐），否则同步时需要输入密码

## SSH 免密配置

```bash
ssh-keygen -t ed25519 -C "codesync"
ssh-copy-id -i ~/.ssh/id_ed25519.pub user@your-server
```
