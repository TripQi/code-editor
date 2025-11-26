## code-editor-mcp

面向多客户端并发、安全沙箱、编码感知的代码编辑 MCP 服务器。与 `code-index-mcp` 搭配使用：索引/导航交给 index，精确读写和补丁交给本服务。

### 特性
- 根目录沙箱：阻止路径穿越，所有操作限定在 `CODE_EDIT_ROOT` 或启动时的 CWD。
- 编码感知：所有读写支持 `encoding` 参数，默认 UTF-8，解码/编码错误会抛出带编码信息的异常。
- 乐观锁：写类操作支持 `expected_mtime`，防止多客户端互相覆盖（默认容差 0.01 秒，兼容 Windows 时间分辨率）。
- 严格补丁：统一 diff 校验旧/新侧行数，任何 hunk 不匹配即失败，原文件不改。
- 可控列目录：`list_files` 支持忽略模式，避免噪声目录。

### 环境要求
- Python 3.11+（推荐 3.13）。
- 依赖通过 `uv` 或 `pip` 安装：`mcp>=1.22.0`。

### 安装与运行
```bash
# 安装依赖（uv）
uv sync

# 运行服务（使用虚拟环境）
uv run python server.py
```

可选环境变量：
- `CODE_EDIT_ROOT`：根目录沙箱，未设置时默认为启动命令的 CWD。

### 工具清单
所有涉及内容读写的接口均有 `encoding="utf-8"` 可选，抛异常时由 MCP 客户端转化为 Tool Error。

- `list_files(directory_path, ignore_patterns=None) -> list`
  - 单层列目录，按名称排序。
  - 忽略模式（fnmatch），默认：`.git`、`__pycache__`、`node_modules`、`.DS_Store`。
- `get_file_info(file_path) -> dict`
  - 返回 `path`、`is_dir`、`size`、`mtime` (float，用于锁)、`last_modified` (ISO 字符串)、`ctime`。
- `read_file(file_path, encoding="utf-8") -> str`
- `read_range(file_path, start_line, end_line, encoding="utf-8") -> str`
  - 1-based 闭区间，越界抛错。
- `create_file(file_path, content, encoding="utf-8") -> str`
  - 已存在报错，自动创建父目录。
- `write_file(file_path, content, encoding="utf-8", expected_mtime=None) -> str`
  - 全量覆盖，乐观锁检查（若提供 `expected_mtime`）。
- `append_file(file_path, content, encoding="utf-8", expected_mtime=None) -> str`
  - 追加写入，自动建父目录，返回追加行数。
- `edit_lines(file_path, start_line, end_line, new_content, encoding="utf-8", expected_mtime=None) -> str`
  - 1-based 闭区间替换。
- `insert_at_line(file_path, line_number, content, encoding="utf-8", expected_mtime=None) -> str`
  - 在指定行后插入（0 表示文件头）。
- `replace_string(file_path, search_string, replace_string, encoding="utf-8", expected_mtime=None) -> str`
  - 仅允许唯一命中，>1 次直接报错，避免误伤。
- `apply_unified_diff(file_path, diff_content, encoding="utf-8", expected_mtime=None) -> str`
  - 单文件统一 diff，校验旧/新行数，任一 hunk 不匹配则失败，原子写回。
- `delete_file(file_path, expected_mtime=None) -> str`
  - 仅文件，乐观锁可选。
- `copy_file(source_path, destination_path, expected_mtime=None) -> str`
  - 拷贝文件，目标存在报错；源可选锁。
- `move_file(source_path, destination_path, expected_mtime=None) -> str`
  - 移动/重命名文件或目录，源可选锁，目标存在报错。
- `delete_directory(directory_path, expected_mtime=None) -> str`
  - 递归删除目录，禁止删除 ROOT 本身。

### 乐观锁用法
1. 调用 `get_file_info` 获取目标文件的 `mtime`（float）。
2. 写操作传入 `expected_mtime=该值`。
3. 若期间文件被他人修改，抛出 `RuntimeError: Conflict...`，调用方可重读后再试。
4. 容差 `0.01` 秒以兼容 Windows 时间分辨率。

### 编码自适应建议
默认 UTF-8。若读取出现 `UnicodeError: File could not be read as utf-8`，请用 `encoding='gbk'`、`'utf-8-sig'` 等重试；写入同理。

### 安全注意
- 所有路径经 `_validate_path` 检查，拒绝越过根目录。
- `replace_string` 限制唯一命中；补丁应用全量校验，失败不落盘。

### 典型调用示例（伪代码）
- 读取片段：`read_range("src/app.py", 10, 30)`
- 带锁写入：先 `info = get_file_info("src/app.py")`，再 `write_file("src/app.py", content, expected_mtime=info["mtime"])`
- 应用补丁：`apply_unified_diff("src/app.py", diff_text, expected_mtime=info["mtime"])`
- 控制噪声目录列表：`list_files(".", ignore_patterns=[".git", "node_modules", "*.log"])`

### 协同使用
搭配 `code-index-mcp` 获取符号/位置后，再用本服务执行精确读写与补丁，降低误改风险。默认 stdio 传输，客户端启动命令示例：`uv run python server.py`。
