# Obsidian 记忆管理

## 读写配置

- **Vault 路径**: `C:\Users\016575202\Documents\Obsidian Vault`
- **全局记忆**: `Claude-Memory\Global\`
- **项目记忆**: `Claude-Memory\Projects\intent-router\`

## 读取记忆

当需要检索历史经验时，先搜索相关文件：
```bash
# 搜索全局记忆
grep -r "关键词" "C:\Users\016575202\Documents\Obsidian Vault\Claude-Memory\Global"

# 搜索项目记忆
grep -r "关键词" "C:\Users\016575202\Documents\Obsidian Vault\Claude-Memory\Projects\intent-router"
```

## 存储记忆

使用 Write 工具直接写入 Obsidian vault：

```markdown
---
tags: [lesson, <技术领域>, global/intent-router]
date: <YYYY-MM-DD>
project: global 或 intent-router
---

# <标题>

## 问题/场景
<描述>

## 解决方案/经验
<要点列表>

## 教训/注意事项
<注意事项>
```

## 存储级别判断

- **全局级**: `Claude-Memory\Global\` - 通用经验、工具技巧、最佳实践
- **项目级**: `Claude-Memory\Projects\intent-router\` - 项目特定架构、配置、业务逻辑
