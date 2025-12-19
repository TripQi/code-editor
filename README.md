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
| `CODE_EDIT_FILE_WRITE_LINE_LIMIT` | `file_ops` 写入行数警戒 | 50 |

### 设计要点
- 允许目录列表：默认允许用户主目录；路径需落在允许目录内，否则拒绝。`set_root_path` 仅将目录加入允许列表并更新安全标记，不做路径拼接。
- 持久允许目录：`set_root_path` 成功后写入 JSON，可跨会话复用。
- 乐观锁：写/删类支持秒或纳秒级 `expected_mtime`，10ms 容忍；写入走原子写避免部分落盘。
- 默认忽略：`.git`、`__pycache__`、`node_modules`、`.DS_Store`、`.env*`、`.venv`、`*.log`、`*.pem`；`ignore_patterns` 传空字符串/空列表可关闭默认忽略。
- 编码感知：`read_file` 默认自动探测/复用编码（utf-8/gb2312/gbk），基于最新 mtime 刷新的元信息缓存；如自动结果乱码，可显式传 `encoding` 覆盖。
- 安全删除：禁止删除当前根/其祖先/关键系统目录。

### MCP 工具（code-editor）

#### 文件系统工具

| 名称 | 工具 | 功能 | 主要参数/说明 | 常见误用 |
| --- | --- | --- | --- | --- |
| `set_root_path` | `set_root_path(root_path)` | 加入允许目录（并更新安全标记） | 必须绝对且存在的目录；可先看 `list_allowed_roots` | 传相对路径/不存在路径 |
| `list_allowed_roots` | `list_allowed_roots()` | 返回当前允许目录列表 | 合并环境变量与持久化 JSON | 以为会调整 CODE_EDIT_ROOT（不会） |
| `get_file_info` | `get_file_info(file_path)` | stat + 编码(置信度) + 行数(小文件)；含 mtime/size | 绝对路径；文件或目录；需在允许目录内；用于大文件预检查或元信息获取 | 假设一定返回行数（大文件不会）；当作必需前置步骤 |
| `read_file` | `read_file(file_path, offset=0, length=None, encoding=None)` | 流式读取文本/图片，自动探测/缓存编码（utf-8/gbk/gb2312），二进制返回提示 | 绝对路径；offset<0 读尾；length 最大行数（超过 `CODE_EDIT_FILE_READ_LINE_LIMIT` 会截断）；乱码时显式传 `encoding` 覆盖 | 传 URL；非整数 offset/length；相对路径；超大文件无范围读取 |
| `dir_ops` | `dir_ops(action, dir_path, depth=2, format="tree"|"flat", ignore_patterns=None, max_items=1000, expected_mtime=None, confirm_token=None, allow_nonempty=None)` | 统一目录操作 | `action`=create/list/delete；绝对路径；list: tree 返回字符串列表、flat 返回字典列表；`ignore_patterns` 为 None 用默认忽略，空字符串/空列表关闭默认忽略；flat 下 `max_items` 限制返回条数；delete: 必须提供 `expected_mtime`、`confirm_token`、`allow_nonempty`，`confirm_token`=`delete:<normalized_abs_path>`（Path.resolve + os.path.normcase） | action 不支持/参数缺失；delete 未显式 allow_nonempty；confirm_token 不匹配 |
| `file_ops` | `file_ops(action, file_path=None, content=None, source_path=None, destination_path=None, expected_mtime=None, encoding="utf-8")` | 综合文件操作：write/append/copy/move/delete | 所有路径必须绝对且在允许目录内；write 覆盖、append 追加；write/append 需 file_path+content；copy/move 需 source_path+destination_path；delete 需 file_path；encoding 仅写入使用；expected_mtime：写/删校验目标文件，拷贝/移动校验源文件 | action 不支持或参数缺失；copy 目标已存在；delete 目标是目录 |
| `convert_file_encoding` | `convert_file_encoding(file_paths, source_encoding, target_encoding, error_handling="strict", mismatch_policy="warn-skip")` | 批量转码并覆盖写回 | 绝对路径列表；utf-8/gbk/gb2312；错误处理 strict/replace/ignore；编码检测( charset-normalizer )，策略 fail-fast / warn-skip(默认) / force；结果返回 detectedEncoding/Confidence/mismatch；内置别名兼容 utf8/utf_8/cp936/gb-2312 | 相对路径；二进制文件；未在白名单 |

#### dir_ops 参数要点（避免误调用）
- `create`：仅 `dir_path` 必须；其余参数会被忽略。
- `list`：`format` 仅支持 `tree`/`flat`；`max_items` 必须为正整数或 None。
- `delete`：必须同时提供 `expected_mtime`、`confirm_token`、`allow_nonempty`（显式 True/False）。
- `confirm_token` 生成规则（严格匹配）：
```python
from pathlib import Path
import os

normalized = os.path.normcase(str(Path(dir_path).resolve()))
confirm_token = f"delete:{normalized}"
```

#### 代码精准编辑工具

| 名称 | 工具 | 功能 | 主要参数/说明 | 常见误用 |
| --- | --- | --- | --- | --- |
| `edit_block` | `edit_block(file_path, old_string, new_string, expected_replacements=1, expected_mtime=None, ignore_whitespace=False, normalize_escapes=False, encoding="utf-8")` | 精确替换，行尾规范化；>10MB 自动走流式精确匹配 | 绝对路径且在允许目录内；old_string 为搜索文本，new_string 为替换文本；计数不符/空搜索/仅模糊命中会抛异常；大文件仅支持严格字面匹配 | 期望计数错；空搜索；大文件下使用忽略空白/转义归一会报错 |
- 查看并新增允许目录：`list_allowed_roots` → 若未包含目标，调用 `set_root_path("/data/project")`。
- 带锁写入：`info = get_file_info("/abs/path/src/app.py")` → `file_ops(action="write", file_path="/abs/path/src/app.py", content=content, expected_mtime=info["modified"])`。
- 精确替换：`edit_block("/abs/path/src/app.py", "old", "new", expected_replacements=1, expected_mtime=info["modified"])`。
- 批量转码：`convert_file_encoding(["/abs/a.txt", "/abs/b.txt"], "gb2312", "utf-8", error_handling="replace", mismatch_policy="warn-skip")`。
- 列目录（扁平）：`dir_ops(action="list", dir_path="/abs/path", format="flat", ignore_patterns=[".git", "node_modules"])`。
- 删除目录（显式确认）：`info = get_file_info("/abs/path")` → `normalized = os.path.normcase(str(Path("/abs/path").resolve()))` → `token = f"delete:{normalized}"` → `dir_ops(action="delete", dir_path="/abs/path", expected_mtime=info["modified"], confirm_token=token, allow_nonempty=True)`。

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
- 删除防护：`dir_ops` 的 delete 行为拒绝删除当前 root、其祖先和关键系统目录（/ /home /root /Users C:\\）。
- 允许目录管理：如需访问新路径，先 `set_root_path` 加入白名单；不在白名单的绝对路径会被拒绝。
