# Ledger Vault

A dependency-free Python reference implementation for security, audit, append-only.

Run with: python3 demo.py
Tests: python3 -m unittest discover -s tests -v

## Scope

实现一个仅依赖 Python 标准库的版本化配置仓：所有写入以追加记录保存，支持按名称读取指定版本、列出版本、查询当前活动版本和整体重载。重载必须先完整校验记录链与摘要，任何损坏都不能替换当前内存快照；读者只能看到重载前或重载后的完整状态。公开接口、磁盘格式、异常类型和命令行行为必须稳定，全部结果可由离线脚本独立验收。

## 全仓历史快照

- `snapshot_at(version=None)`：按全局记录序号重建某一时点的全仓状态。`version=0` 返回空快照；省略或传入已加载记录总数时返回当前已加载状态；取值必须是 0 到已加载记录总数之间的非负整数（布尔值不接受），否则抛出 `ValueError`。结果沿用 `versions()` 的条目结构（`name`/`version`/`value`），每个名称只保留该时点最后一条记录，按名称排序并返回深拷贝。纯读取：不写日志、不改活动版本、不触发追加；日志在最近一次成功 reload 后被外部改坏时，仍只反映内存中的完整状态直到显式 reload。
- CLI：`python3 app.py snapshot --root VAULT [--version N]`，输出可由 `json.loads` 解析、键排序确定的 JSON 文档；非法版本退出状态为 1 且不输出任何内容，`--version 0` 为合法空结果，省略时读取最近一次成功加载的状态。
