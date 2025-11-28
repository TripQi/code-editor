## code-editor

面向多客户端并发、安全沙箱、编码感知的代码编辑 MCP 服务器。与 `code-index-mcp` 搭配：索引/导航交给 index，精读精写、补丁和路径切换交给本服务。
> 路径访问更新：采用 allowedDirectories 校验，默认允许用户主目录。CODE_EDIT_ROOT 仅作相对路径基准，不再作为访问边界或自动切根；需要访问额外目录请用 set_root_path 加入允许列表。


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
- `CODE_EDIT_ROOT`：相对路径基准（默认启动时的 CWD），不做访问边界。
- `CODE_EDIT_ALLOWED_ROOTS_FILE`：允许目录持久化 JSON，默认 `tools/.code_edit_roots.json`。
- `CODE_EDIT_ALLOWED_DIRECTORIES`：逗号分隔允许目录列表（兼容旧的 `CODE_EDIT_ALLOWED_ROOTS`）。

环境变量一览：

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `CODE_EDIT_ROOT` | 相对路径基准（非访问边界） | 当前工作目录 |
| `CODE_EDIT_ALLOWED_ROOTS_FILE` | 允许目录持久化文件 | `tools/.code_edit_roots.json` |
| `CODE_EDIT_ALLOWED_DIRECTORIES` (兼容 `CODE_EDIT_ALLOWED_ROOTS`) | 额外允许目录（逗号分隔） | 空 |
| `CODE_EDIT_FILE_READ_LINE_LIMIT` | `read_file` 最大行数 | 1000 |
| `CODE_EDIT_FILE_WRITE_LINE_LIMIT` | `write_file` 行数警戒 | 50 |

### 设计要点
- 允许目录列表：默认允许用户主目录；路径需落在允许目录内，否则拒绝。`set_root_path` 仅将目录加入允许列表并设置相对基准，不自动切换执行。
- 持久允许目录：`set_root_path` 成功后写入 JSON，可跨会话复用。
- 乐观锁：写/删类支持 `expected_mtime`，防止并发覆盖。
- 编码感知：默认 UTF-8，读写失败请尝试 `encoding='gbk'` 等。
- 安全删除：禁止删除当前根/其祖先/关键系统目录。

### MCP 工具（code-editor）

#### 文件系统工具

| 工具 | 功能 | 主要参数/说明 | 常见误用 |
| --- | --- | --- | --- |
| `set_root_path(root_path)` | 加入允许目录并设置相对路径基准 | 目录必须存在；更新 CODE_EDIT_ROOT 相对基准；可先看 `list_allowed_roots` | 传不存在/非目录路径；未先将目录加入允许列表 |
| `list_allowed_roots()` | 返回当前允许目录列表 | 合并环境变量与持久化 JSON | 以为会调整 CODE_EDIT_ROOT（不会） |
| `get_file_info(file_path)` | stat 信息（小文件含行数等） | 路径可为文件/目录；需在允许目录内 | 假设一定返回行数（大文件不会） |
| `read_file(file_path, offset=0, length=None)` | 流式读取文本/图片 | offset<0 读尾部行，offset>=0 读指定行开始；length 为最大行数 | 传 URL；非整数 offset/length；不在允许目录 |
| `create_directory(dir_path)` | 递归建目录 | 需在允许目录内 | 传文件路径 |
| `list_directory(dir_path, depth=2, format="tree"|"flat", ignore_patterns=None)` | 列目录 | tree 返回字符串列表；flat 返回字典列表，支持忽略模式 | 用不支持的 format；depth<=0；ignore_patterns 非字符串列表 |
| `write_file(file_path, content, mode="rewrite"/"write"/"append", expected_mtime=None)` | 覆盖/追加写入 | 非法 mode 抛错；expected_mtime 乐观锁 | `mode="w"`/`"replace"`；mtime 过期 |
| `delete_file(file_path, expected_mtime=None)` | 删文件 | 仅文件；乐观锁；需在允许目录内 | 目标是目录 |
| `move_file(source_path, destination_path, expected_mtime=None)` | 移动/重命名 | 目标不能存在；相对路径基准由当前 CODE_EDIT_ROOT 决定 | 未先调整相对基准；目标已存在 |
| `copy_file(source_path, destination_path, expected_mtime=None)` | 复制文件 | 源必须是文件；目标不存在；相对路径基准由当前 CODE_EDIT_ROOT 决定 | 源是目录；目标已存在 |
| `delete_directory(directory_path, expected_mtime=None)` | 递归删目录 | 禁删当前根/祖先/关键目录；必须是目录 | 传文件；试图删根或系统目录 |

#### 代码精准编辑工具

| 工具 | 功能 | 主要参数/说明 | 常见误用 |
| --- | --- | --- | --- |
| `edit_lines(file_path, start_line, end_line, new_content, expected_mtime=None, encoding="utf-8")` | 按行替换 | 1-based 闭区间；行越界抛错；乐观锁 | start/end 反向；越界 |
| `insert_at_line(file_path, line_number, content, expected_mtime=None, encoding="utf-8")` | 插入行 | line_number>=0；乐观锁 | 行号越界 |
| `edit_block(file_path, old_string, new_string, expected_replacements=1, expected_mtime=None)` | 精确替换，行尾规范化 | 计数不符/空搜索/仅模糊命中会抛异常；乐观锁 | 期望计数错；指望模糊自动替换 |
| `replace_string(file_path, search_string, replace_string, expected_mtime=None)` | 兼容单次替换包装 | 等价 `edit_block(..., expected_replacements=1)` | 空搜索；多处命中 |
| `apply_unified_diff(file_path, diff_content, encoding="utf-8", expected_mtime=None)` | 应用统一 diff | 校验 hunk 头/行数，不匹配直接失败；乐观锁 | diff 无 @@；行数不符 |

### 使用示例
- 查看允许目录并设置相对基准：`list_allowed_roots` → 若未包含目标，调用 `set_root_path("/data/project")`。
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
- 路径验证：所有操作都要求落在允许目录列表内；CODE_EDIT_ROOT 仅作为相对路径基准，不再做访问边界，也不会自动切换执行。
- 删除防护：`delete_directory` 拒绝删除当前 root、其祖先和关键系统目录（/ /home /root /Users C:\\）。
- 相对路径基准：copy/move 等相对路径操作仍受当前 CODE_EDIT_ROOT 影响，若需以其他目录为基准，请先 `set_root_path`；访问权限仍由允许目录列表决定。
