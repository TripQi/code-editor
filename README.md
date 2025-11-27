## code-editor

面向多客户端并发、安全沙箱、编码感知的代码编辑 MCP 服务器。与 `code-index-mcp` 搭配：索引/导航交给 index，精读精写、补丁和路径切换交给本服务。

### 安装与运行
```bash
# 安装（pip 或 uv 均可，包名 code-editor-mcp）
pip install code-editor-mcp         # 或 uv pip install code-editor-mcp

# 直接启动 CLI 入口
code-editor

# 若从源码运行
uv sync
uv run python server.py
```

关键环境变量：
- `CODE_EDIT_ROOT`：初始根目录（默认启动时的 CWD）。
- `CODE_EDIT_ALLOWED_ROOTS_FILE`：白名单持久化 JSON，默认 `tools/.code_edit_roots.json`。
- `CODE_EDIT_ALLOWED_ROOTS`：逗号分隔额外白名单。

环境变量一览：

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `CODE_EDIT_ROOT` | 启动根目录 | 当前工作目录 |
| `CODE_EDIT_ALLOWED_ROOTS_FILE` | 白名单持久化文件 | `tools/.code_edit_roots.json` |
| `CODE_EDIT_ALLOWED_ROOTS` | 额外白名单（逗号分隔） | 空 |
| `CODE_EDIT_FILE_READ_LINE_LIMIT` | `read_file` 最大行数 | 1000 |
| `CODE_EDIT_FILE_WRITE_LINE_LIMIT` | `write_file` 行数警戒 | 50 |

### 设计要点
- 根隔离 + 自动切根：路径必须在当前根内；若落在白名单其他根，自动切换再执行，避免客户端重试。
- 持久白名单：`set_root_path` 成功后写入 JSON，可跨会话复用。
- 乐观锁：写/删类支持 `expected_mtime`，防止并发覆盖。
- 编码感知：默认 UTF-8，读写失败请尝试 `encoding='gbk'` 等。
- 安全删除：禁止删除当前根/其祖先/关键系统目录。

### MCP 工具（code-editor）

| 工具 | 功能 | 主要参数/说明 | 常见误用 |
| --- | --- | --- | --- |
| `set_root_path(root_path)` | 切换根并加入白名单 | 目录必须存在；切根会影响后续全局 root；可先看 `list_allowed_roots` | 传不存在/非目录路径；忽略切根副作用 |
| `list_allowed_roots()` | 返回当前白名单 | 合并当前根、环境变量和 JSON | 以为会切根（不会） |
| `get_file_info(file_path)` | stat 信息（小文件含行数等） | 路径可为文件/目录，自动切根 | 假设一定返回行数（大文件不会） |
| `read_file(file_path, offset=0, length=None)` | 流式读取文本/图片 | offset<0 读尾部行，offset>=0 读指定行开始；length 为最大行数 | 传 URL；非整数 offset/length；未入白名单路径 |
| `create_directory(dir_path)` | 递归建目录 | 自动切根 | 传文件路径 |
| `list_directory(dir_path, depth=2, format="tree"|"flat", ignore_patterns=None)` | 列目录 | tree 返回字符串列表；flat 返回字典列表，支持忽略模式 | 用不支持的 format；depth<=0；ignore_patterns 非字符串列表 |
| `write_file(file_path, content, mode="rewrite"/"write"/"append", expected_mtime=None)` | 覆盖/追加写入 | 非法 mode 抛错；expected_mtime 乐观锁 | `mode="w"`/`"replace"`；mtime 过期 |
| `delete_file(file_path, expected_mtime=None)` | 删文件 | 仅文件；乐观锁；自动切根 | 目标是目录 |
| `move_file(source_path, destination_path, expected_mtime=None)` | 移动/重命名 | 目标不能存在；跨根会把全局 root 切到目标 | 忽略切根副作用；目标已存在 |
| `copy_file(source_path, destination_path, expected_mtime=None)` | 复制文件 | 源必须是文件；目标不存在；跨根切到目标 | 源是目录；目标已存在 |
| `delete_directory(directory_path, expected_mtime=None)` | 递归删目录 | 禁删当前根/祖先/关键目录；必须是目录 | 传文件；试图删根或系统目录 |
| `edit_lines(file_path, start_line, end_line, new_content, expected_mtime=None, encoding="utf-8")` | 按行替换 | 1-based 闭区间；行越界抛错；乐观锁 | start/end 反向；越界 |
| `insert_at_line(file_path, line_number, content, expected_mtime=None, encoding="utf-8")` | 插入行 | line_number>=0；乐观锁 | 行号越界 |
| `edit_block(file_path, old_string, new_string, expected_replacements=1, expected_mtime=None)` | 精确替换，行尾规范化 | 计数不符/空搜索/仅模糊命中会抛异常；乐观锁 | 期望计数错；指望模糊自动替换 |
| `replace_string(file_path, search_string, replace_string, expected_mtime=None)` | 兼容单次替换包装 | 等价 `edit_block(..., expected_replacements=1)` | 空搜索；多处命中 |
| `apply_unified_diff(file_path, diff_content, encoding="utf-8", expected_mtime=None)` | 应用统一 diff | 校验 hunk 头/行数，不匹配直接失败；乐观锁 | diff 无 @@；行数不符 |

### 使用示例
- 查看白名单并切根：`list_allowed_roots` → 若未包含目标，调用 `set_root_path("/data/project")`。
- 带锁写入：`info = get_file_info("src/app.py")` → `write_file("src/app.py", content, mode="rewrite", expected_mtime=info["modified"])`。
- 精确替换：`edit_block("src/app.py", "old", "new", expected_replacements=1, expected_mtime=info["modified"])`。
- 列目录（扁平）：`list_directory(".", format="flat", ignore_patterns=[".git", "node_modules"])`。

### MCP 客户端快速配置示例

```json
{
  "mcpServers": {
    "code-editor": {
      "command": "code-editor",
      "env": {
        "CODE_EDIT_ROOT": "C:\\\\Projects\\\\repo"
      }
    }
  }
}
```

### 安全/行为提示
- 路径验证：所有操作都要求在当前根内；若路径属于白名单其他根，会自动切根后执行，否则抛错。
- 删除防护：`delete_directory` 拒绝删除当前 root、其祖先和关键系统目录（/ /home /root /Users C:\\）。
- 跨根副作用：copy/move 到其他根后，全局 root 停留在目标根，后续操作以新根为准，如需恢复请显式 `set_root_path`。
