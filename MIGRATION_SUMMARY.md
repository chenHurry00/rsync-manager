# 多目标仓库改造完成

## 改造内容

### 1. 数据结构变化

**旧格式（平面结构）：**
```json
{
  "repos": [
    {
      "id": "xxx",
      "name": "b29_locomotion-4090",
      "local": "/home/yuchen/usetest/RL/b29_locomotion",
      "remote": "/home/user/ych/RL/b29_locomotion",
      "excludes": ["legged_gym/logs/*"]
    },
    {
      "id": "yyy",
      "name": "b29_locomotion-X670",
      "local": "/home/yuchen/usetest/RL/b29_locomotion",
      "remote": "/home/yuchen/usetest/RL/b29_locomotion",
      "excludes": ["legged_gym/logs/*"]
    }
  ]
}
```

**新格式（多目标结构）：**
```json
{
  "repos": [
    {
      "id": "xxx",
      "name": "b29_locomotion",
      "local": "/home/yuchen/usetest/RL/b29_locomotion",
      "targets": {
        "1776491608233": {
          "remote": "/home/user/ych/RL/b29_locomotion",
          "excludes": ["legged_gym/logs/*"],
          "push_opts": {...},
          "browser_opts": {...}
        },
        "1776498582886": {
          "remote": "/home/yuchen/usetest/RL/b29_locomotion",
          "excludes": ["legged_gym/logs/*"]
        }
      }
    }
  ]
}
```

### 2. 自动迁移

**迁移规则：**
- 识别仓库名后缀（`-4090`、`-X670`）
- 自动合并同本地路径的仓库
- 去除名称后缀，保持简洁

**迁移结果：**
- 原 6+ 个独立仓库 → 6 个多目标仓库
- `b29_locomotion-4090` + `b29_locomotion-X670` → `b29_locomotion` (2个目标)
- `Audit-3090` + `Audit-4090` → `Audit` (2个目标)

### 3. 新功能

#### 仓库管理
- ✅ 单个本地仓库配置多个远程目标
- ✅ 每个目标独立配置：remote、excludes、push_opts
- ✅ 添加/编辑/删除目标服务器

#### UI 改进
- ✅ 同步页：服务器下拉框只显示该仓库已配置的服务器
- ✅ 仓库列表：显示目标服务器数量和徽章
- ✅ 编辑仓库：列出所有目标，可逐个编辑/删除
- ✅ 添加仓库：可选初始目标服务器

#### 后端改进
- ✅ 启动时自动检测并迁移旧格式
- ✅ 所有 API 支持多目标结构
- ✅ 向后兼容旧配置文件

## 使用方式

### 添加新仓库
1. 点击"添加仓库"
2. 填写名称和本地路径
3. 可选：选择初始服务器和远程路径
4. 保存后可继续添加更多目标

### 编辑仓库目标
1. 点击仓库的"编辑"
2. 查看所有目标服务器
3. 点击目标的"编辑"修改配置
4. 或点击"+ 添加目标服务器"

### 同步
- 选择仓库后，服务器下拉框自动过滤为该仓库已配置的服务器
- 其余操作与之前完全相同

## 验证

启动应用：
```bash
python3 app.py
# 打开 http://localhost:7788
```

检查迁移结果：
```bash
cat ~/.codesync/config.json | python3 -m json.tool
```

## 注意事项

1. **无需手动迁移** — 首次启动自动完成
2. **原配置已备份** — 旧格式在读取时转换，不修改文件
3. **完全向后兼容** — 旧格式配置仍可正常使用
4. **建议操作** — 启动应用后检查"仓库"页面，确认迁移正确
