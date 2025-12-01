## code-editor

面向多客户端并发、安全沙箱、编码感知的代码编辑 MCP 服务器。与 `code-index-mcp` 搭配：索引/导航交给 index，精读精写、补丁和路径切换交给本服务。
> 路径访问更新：**所有参数必须是绝对路径**，仅校验是否落在允许目录列表内。`CODE_EDIT_ROOT` 只是安全标记，不做访问边界或相对路径解析；访问新目录请用 `set_root_path` 加入允许列表。

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
- `CODE_EDIT_ROOT`：安全标记（默认启动时的 CWD），不做访问边界，也不参与相对路径解析。
- `CODE_EDIT_ALLOWED_ROOTS_FILE`：允许目录持久化 JSON，默认 `tools/.code_edit_roots.json`。
- `CODE_EDIT_ALLOWED_DIRECTORIES`：逗号分隔允许目录列表（兼容旧的 `CODE_EDIT_ALLOWED_ROOTS`）。

环境变量一览：

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `CODE_EDIT_ROOT` | 安全标记（非访问边界，不做路径解析） | 当前工作目录 |
| `CODE_EDIT_ALLOWED_ROOTS_FILE` | 允许目录持久化文件 | `tools/.code_edit_roots.json` |
| `CODE_EDIT_ALLOWED_DIRECTORIES` (兼容 `CODE_EDIT_ALLOWED_ROOTS`) | 额外允许目录（逗号分隔） | 空 |
| `CODE_EDIT_FILE_READ_LINE_LIMIT` | `read_file` 最大行数 | 1000 |
| `CODE_EDIT_FILE_WRITE_LINE_LIMIT` | `write_file` 行数警戒 | 50 |

### 设计要点
- 允许目录列表：默认允许用户主目录；路径需落在允许目录内，否则拒绝。`set_root_path` 仅将目录加入允许列表并更新安全标记，不做路径拼接。
- 持久允许目录：`set_root_path` 成功后写入 JSON，可跨会话复用。
- 乐观锁：写/删类支持秒或纳秒级 `expected_mtime`，10ms 容忍；写入走原子写避免部分落盘。
- 默认忽略：`.git`、`__pycache__`、`node_modules`、`.DS_Store`、`.env*`、`.venv`、`*.log`、`*.pem`；`ignore_patterns` 传空字符串/空列表可关闭默认忽略。
- 编码感知：默认 UTF-8，可切换 gbk/gb2312；`get_file_info` 会尝试检测编码与置信度。
- 安全删除：禁止删除当前根/其祖先/关键系统目录。

### MCP 工具（code-editor）

#### 文件系统工具

| 名称 | 工具 | 功能 | 主要参数/说明 | 常见误用 |
| --- | --- | --- | --- | --- |
| `set_root_path` | `set_root_path(root_path)` | 加入允许目录（并更新安全标记） | 必须绝对且存在的目录；可先看 `list_allowed_roots` | 传相对路径/不存在路径 |
| `list_allowed_roots` | `list_allowed_roots()` | 返回当前允许目录列表 | 合并环境变量与持久化 JSON | 以为会调整 CODE_EDIT_ROOT（不会） |
| `get_file_info` | `get_file_info(file_path)` | stat + 编码(置信度) + 行数(小文件) | 绝对路径；可文件/目录；需在允许目录内 | 假设一定返回行数（大文件不会） |
| `read_file` | `read_file(file_path, offset=0, length=None, encoding="utf-8")` | 流式读取文本/图片 | 绝对路径；offset<0 读尾；length 最大行数；支持 gbk/gb2312 | 传 URL；非整数 offset/length；相对路径 |
| `create_directory` | `create_directory(dir_path)` | 递归建目录 | 绝对路径且在允许目录内 | 传文件路径 |
| `list_directory` | `list_directory(dir_path, depth=2, format="tree"|"flat", ignore_patterns=None)` | 列目录 | 绝对路径；tree 返回字符串列表；flat 返回字典列表；`ignore_patterns` 为空则关闭默认忽略；支持 fnmatch | 用不支持的 format；depth<=0；ignore_patterns 非字符串列表 |
| `write_file` | `write_file(file_path, content, mode="rewrite"/"write"/"append", expected_mtime=None, encoding="utf-8")` | 覆盖/追加写入 | 绝对路径；非法 mode 抛错；expected_mtime 乐观锁；rewrite 走原子写；支持 gbk/gb2312 | `mode="w"`/`"replace"`；mtime 过期；相对路径 |
| `delete_file` | `delete_file(file_path, expected_mtime=None)` | 删文件 | 绝对路径；仅文件；乐观锁 | 目标是目录；相对路径 |
| `move_file` | `move_file(source_path, destination_path, expected_mtime=None)` | 移动/重命名 | 绝对路径；目标不能存在 | 传相对路径；目标已存在 |
| `copy_file` | `copy_file(source_path, destination_path, expected_mtime=None)` | 复制文件 | 绝对路径；源必须是文件；目标不存在 | 源是目录；目标已存在；相对路径 |
| `delete_directory` | `delete_directory(directory_path, expected_mtime=None)` | 递归删目录 | 绝对路径；禁删当前 root/祖先/关键目录；必须是目录 | 传文件；试图删根或系统目录 |
| `convert_file_encoding` | `convert_file_encoding(file_paths, source_encoding, target_encoding, error_handling="strict", mismatch_policy="warn-skip")` | 批量转码并覆盖写回 | 绝对路径列表；utf-8/gbk/gb2312；错误处理 strict/replace/ignore；编码检测( charset-normalizer )，策略 fail-fast / warn-skip(默认) / force；结果返回 detectedEncoding/Confidence/mismatch；内置别名兼容 utf8/utf_8/cp936/gb-2312 | 相对路径；二进制文件；未在白名单 |

#### 代码精准编辑工具

| 名称 | 工具 | 功能 | 主要参数/说明 | 常见误用 |
| --- | --- | --- | --- | --- |
| `edit_lines` | `edit_lines(file_path, start_line, end_line, new_content, expected_mtime=None, encoding="utf-8")` | 按行替换 | 1-based 闭区间；行越界抛错；乐观锁 | start/end 反向；越界 |
| `insert_at_line` | `insert_at_line(file_path, line_number, content, expected_mtime=None, encoding="utf-8")` | 插入行 | line_number>=0；乐观锁 | 行号越界 |
| `edit_block` | `edit_block(file_path, old_string, new_string, expected_replacements=1, expected_mtime=None, ignore_whitespace=False, normalize_escapes=False)` | 精确替换，行尾规范化 | 计数不符/空搜索/仅模糊命中会抛异常；支持 `ignore_whitespace` 空白不敏感匹配；`normalize_escapes` 可将 `\"`/`\\n` 等反转义后再匹配（默认关闭） | 期望计数错；空搜索；忽略空白时匹配过宽；误开反转义导致匹配过宽 |
| `replace_string` | `replace_string(file_path, search_string, replace_string, expected_mtime=None, ignore_whitespace=False, normalize_escapes=False)` | 兼容单次替换包装 | 等价 `edit_block(..., expected_replacements=1)`；同样支持空白不敏感与可选反转义 | 空搜索；多处命中；忽略空白或反转义期开关期望计数不符 |

### 使用示例
- 查看并新增允许目录：`list_allowed_roots` → 若未包含目标，调用 `set_root_path("/data/project")`。
- 带锁写入：`info = get_file_info("/abs/path/src/app.py")` → `write_file("/abs/path/src/app.py", content, mode="rewrite", expected_mtime=info["modified"])`。
- 精确替换：`edit_block("/abs/path/src/app.py", "old", "new", expected_replacements=1, expected_mtime=info["modified"])`。
- 批量转码：`convert_file_encoding(["/abs/a.txt", "/abs/b.txt"], "gb2312", "utf-8", error_handling="replace", mismatch_policy="warn-skip")`。
- 列目录（扁平）：`list_directory("/abs/path", format="flat", ignore_patterns=[".git", "node_modules"])`。

### MCP 客户端快速配置示例

```json
{
  "mcpServers": {
    "code-editor": {
      "command": "code-editor",
      "env": {
        "CODE_EDIT_ROOT": "."
      }
    }
  }
}
```
```toml
[mcp_servers.code-editor]
command = "code-editor"
# 将工作目录指向当前项目,使 CODE_EDIT_ROOT 默认跟随启动时的 CWD
cwd = "."
startup_timeout_sec = 120
```

### 安全/行为提示
- 路径验证：所有操作要求**绝对路径**且落在允许目录列表内；`CODE_EDIT_ROOT` 仅为安全标记。
- 删除防护：`delete_directory` 拒绝删除当前 root、其祖先和关键系统目录（/ /home /root /Users C:\\）。
- 允许目录管理：如需访问新路径，先 `set_root_path` 加入白名单；不在白名单的绝对路径会被拒绝。
