# 逻辑漏洞修复方案（按优先级）

## 范围
针对 code-editor 当前暴露的核心逻辑与文件操作路径，按风险优先级给出补丁方案；不涉及 API 设计重构。

## 优先级清单

### P0（必须优先处理）
1) 超时逻辑不可靠导致“超时但已改文件”的一致性风险
- 位置：`tools/timeouts.py`（ThreadPoolExecutor + future.result 超时后仍可能继续执行）
- 风险：调用方收到超时异常，但后台线程仍继续写入/替换，导致“超时=失败”的语义失效
- 方案：
  - 方案A（推荐）：对具有副作用的操作（写/替换/删除）禁用超时或使用“进程级隔离”执行（子进程超时可强杀）
  - 方案B（可选）：保留超时但在返回值/错误中明确标注“操作可能已在后台继续执行”，并记录审计日志
- 验证：并发执行写入+超时，确认不会出现“异常返回但文件已改变”的误导情况

### P1（重要）
2) append 的“原子追加”会读取全文件，存在大文件内存风险
- 位置：`tools/filesystem.py` `write_file(..., mode="append")`
- 风险：大文件追加时加载全文件导致 OOM，形成 DoS
- 方案：
  - 方案A（推荐）：大于阈值时退化为安全的直接 append（非原子），或使用“分块追加 + 临时文件”
  - 方案B：允许 caller 指定 `append_strategy`（atomic/direct）并加大小限制
- 验证：用超大文件（>阈值）追加，确保内存占用可控且行为符合文档说明

3) list_directory flat 模式无条目上限，可能生成超大响应
- 位置：`server.py` `list_directory(..., format="flat")`
- 风险：超大目录导致响应过大/阻塞
- 方案：
  - 增加 `max_items` 参数（默认限制，如 1000）
  - 超出时返回“截断提示 + 计数信息”
- 验证：大目录下 flat 模式返回条目受限且提示完整

### P2（优化）
4) read_file length 无硬上限，可能输出过大
- 位置：`tools/filesystem.py` `_read_file_from_disk`
- 风险：大文件读取导致响应过大/长耗时
- 方案：
  - 增加 `MAX_READ_LINES`（默认与环境变量一致），在 length 过大时 clamp
  - 返回中加入“已截断”提示
- 验证：length 超过限制时，实际返回被截断且提示明确

## 变更点预期（涉及文件）
- `tools/timeouts.py`
- `tools/filesystem.py`
- `server.py`
- `README.md`（同步行为说明）

## 测试建议
- 所有新测试脚本统一放入 `tests/` 目录
- 重点覆盖：超时语义、append 大文件路径、flat 目录限制、read_file 截断


## 修复进展
- P0 已修复：`tools/filesystem.py` 中 `stream_replace` 不再走超时包装，避免“超时但文件已修改”的一致性问题。
- P1 已修复（项2）：`tools/filesystem.py` 的 append 改为“分块复制 + 临时文件 + 原子替换”，避免大文件全量读入内存；失败时回退为直接追加。
- P1 已修复（项3）：`server.py` 的 flat 目录列表新增 `max_items` 限制，超限返回截断提示。
- P2 已完成：`tools/filesystem.py` 中 `read_file` 的 length 超过限制时被截断，并在返回内容中提示。
