# code-editor 工具精简方案

## 目标与范围
- 仅保留文件读写中的 `read_file`；其余四个文件读写工具（`write_file`/`copy_file`/`move_file`/`delete_file`）合并为一个“综合工具”。
- 文件编辑工具仅保留 `edit_block`；其余编辑类工具全部移除（`replace_string`/`edit_lines`/`insert_at_line`/`stream_replace`）。
- 目录与权限类工具（`set_root_path`/`list_allowed_roots`/`get_file_info`/`create_directory`/`list_directory`/`delete_directory`/`convert_file_encoding`）不在本次精简范围内，保持不变。

## 现状盘点（工具注册点）
- 工具定义集中在 `server.py`，通过 `@server.tool()` 注册。
- 当前涉及的工具：
  - 文件读写：`read_file`、`write_file`、`copy_file`、`move_file`、`delete_file`
  - 文件编辑：`edit_block`、`replace_string`、`edit_lines`、`insert_at_line`、`stream_replace`

## 方案设计
### 1) 新的“综合文件操作工具”
建议新增统一工具名（示例：`file_ops`），通过 `action` 参数区分操作类型。

**建议接口：**
```
file_ops(
  action: "write" | "append" | "copy" | "move" | "delete",
  file_path?: str,          # write/append/delete
  content?: str,            # write/append
  source_path?: str,        # copy/move
  destination_path?: str,   # copy/move
  expected_mtime?: float,   # 所有操作可选
  encoding?: str            # write/append，默认 utf-8
)
```

**行为映射：**
- `write_file(rewrite)` -> `file_ops(action="write", file_path=..., content=..., encoding=..., expected_mtime=...)`
- `write_file(append)` -> `file_ops(action="append", ...)`
- `copy_file` -> `file_ops(action="copy", source_path=..., destination_path=..., expected_mtime=...)`
- `move_file` -> `file_ops(action="move", source_path=..., destination_path=..., expected_mtime=...)`
- `delete_file` -> `file_ops(action="delete", file_path=..., expected_mtime=...)`

**异常与校验原则：**
- 复用现有路径校验与 mtime 乐观锁逻辑；错误类型和信息尽量保持兼容。
- `write/append` 仍走原子写；`copy/move` 维持“目标不能存在”的约束。

### 2) 文件编辑工具精简
- 仅保留 `edit_block` 作为唯一编辑工具。
- 删除工具：`replace_string`、`edit_lines`、`insert_at_line`、`stream_replace`。
- `edit_block` 仍保留大文件自动走流式替换的内部逻辑（不再对外暴露 `stream_replace`）。

### 3) 文档与示例更新
- README 工具表与示例更新：
  - 删除旧的四个文件读写工具条目，新增 `file_ops`。
  - 删除 `replace_string`/`edit_lines`/`insert_at_line`/`stream_replace` 说明与示例。
  - 保留并强调 `edit_block` 的唯一编辑入口。

## 修改范围（文件清单）
- `server.py`：
  - 新增 `file_ops` 工具函数并注册。
  - 删除 `write_file`/`copy_file`/`move_file`/`delete_file` 四个工具函数。
  - 删除 `replace_string`/`edit_lines`/`insert_at_line`/`stream_replace` 工具函数。
- `README.md`：
  - 更新工具表、示例与描述。
- （如有）测试用例：
  - 更新/新增 `file_ops` 的行为覆盖，移除旧工具相关测试。

## 兼容性与迁移策略
- 这是破坏性变更，需同步更新所有调用方：
  - 文档、示例、集成脚本中的工具名与参数。
  - MCP 客户端配置中的 tool schema（如有独立声明）。
- 可选的过渡策略（如需）：
  - 保留旧工具但标记废弃，并在日志提示迁移；稳定后再删除。

## 风险与回归点
- 客户端直接依赖旧工具名会失败；需要联动更新。
- `file_ops` 的参数组合更灵活，需严格校验参数缺失/冲突场景。

## 建议验证
- 快速自测：
  - `file_ops` 的 write/append/copy/move/delete 全覆盖。
  - `edit_block` 在小文件与大文件场景下的替换行为。
- 文档核对：README 工具列表与示例是否与实现一致。
