# Code-Editor MCP 优化计划

## 高优先级问题修复

### 问题 1：编码检测缓存
**当前问题：**
- `get_file_info` 每次调用都读取 200KB 检测编码
- 重复调用浪费 I/O 和 CPU

**解决方案：**
- 实现基于 mtime 的内存缓存
- 缓存结构：`{file_path: (mtime_ns, encoding, confidence)}`
- mtime 变化时自动失效

**影响文件：**
- `tools/filesystem.py` - 添加缓存层

**预期收益：**
- 重复 `get_file_info` 调用性能提升 90%+
- 减少磁盘 I/O

---

### 问题 2：大文件行数计算优化
**当前问题：**
- `read_file` 对所有文件调用 `_get_file_line_count()`
- 大文件完整扫描仅为了显示总行数
- `_read_file_with_smart_positioning` (line 337) 每次读取都计算

**解决方案：**
- 仅对 < 10MB 文件计算总行数
- 大文件返回 None，状态消息改为：`[Reading 100 lines from line 500]`
- 保留小文件的友好提示

**影响文件：**
- `tools/filesystem.py` - `_read_file_with_smart_positioning()`

**预期收益：**
- 大文件首次读取性能提升 50-80%
- 降低内存占用

---

### 问题 3：append 模式原子化
**当前问题：**
- `write_file` 的 append 模式直接写入（line 446）
- 不使用临时文件，可能导致文件损坏
- 与 rewrite 模式的原子性保证不一致

**解决方案：**
- append 模式也通过 `_atomic_write()` 实现
- 先读取现有内容，拼接后原子写入
- 保持 API 不变

**影响文件：**
- `tools/filesystem.py` - `write_file()` 函数

**预期收益：**
- 提高 append 操作的可靠性
- 防止并发追加时的数据损坏

**权衡：**
- append 大文件时需要完整读取，性能略降
- 但安全性提升更重要

---

## 实施顺序

1. ✅ **问题 2** - 大文件行数计算优化（风险最低，收益明显）
2. ✅ **问题 1** - 编码检测缓存（需要测试缓存失效逻辑）
3. ✅ **问题 3** - append 原子化（需要权衡性能）

---

## 测试策略

### 问题 1 测试：
- 重复调用 `get_file_info` 验证缓存命中
- 修改文件后验证缓存失效
- 多进程场景验证（缓存隔离）

### 问题 2 测试：
- 小文件（< 10MB）仍显示总行数
- 大文件（> 10MB）不计算行数
- 状态消息格式正确

### 问题 3 测试：
- append 操作中断时文件完整性
- 并发 append 的 mtime 冲突检测
- 大文件 append 性能测试

---

## 向后兼容性

所有修改保持 API 完全兼容：
- ✅ 函数签名不变
- ✅ 返回值结构不变
- ✅ 仅内部实现优化

---

## 性能基准

### 优化前（预期）：
- `get_file_info` 重复调用：~200ms（每次读取 200KB）
- `read_file` 大文件（100MB）首次读取：~500ms（完整扫描计数）
- `write_file` append 模式：~5ms

### 优化后（目标）：
- `get_file_info` 重复调用：~0.5ms（缓存命中）
- `read_file` 大文件首次读取：~50ms（跳过计数）
- `write_file` append 小文件：~10ms（原子化开销）

---

## 回滚计划

每个修改都创建 git commit，方便独立回滚：
1. `feat(perf): 优化大文件行数计算`
2. `feat(perf): 添加编码检测缓存`
3. `feat(reliability): append 模式原子化`
