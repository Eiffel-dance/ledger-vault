# Ledger Vault

A dependency-free Python reference implementation for security, audit, append-only.

Run with: python3 demo.py
Tests: python3 -m unittest discover -s tests -v

## Scope

实现一个仅依赖 Python 标准库的版本化配置仓：所有写入以追加记录保存，支持按名称读取指定版本、列出版本、查询当前活动版本和整体重载。重载必须先完整校验记录链与摘要，任何损坏都不能替换当前内存快照；读者只能看到重载前或重载后的完整状态。公开接口、磁盘格式、异常类型和命令行行为必须稳定，全部结果可由离线脚本独立验收。

`snapshot_at(version=None)` 提供面向整个仓库的历史时点读取：按全局记录序号重建该时点每个名称最后一条记录（名称稳定排序、返回深拷贝）；`version=0` 为空快照，省略或传入最新序号返回当前已加载状态；类型错误、负数或超出已加载记录总数均抛出 `ValueError`。该入口只读：不写日志、不改活动版本、不触发追加；日志被外部改坏后仍只反映最近一次成功 reload 的内存状态。命令行 `snapshot` 子命令（`--root`、可选 `--version`）输出 `json.loads` 可解析、排序确定的同一结果，非法时点以状态 1 退出且不输出部分结果。
