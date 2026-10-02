# Ledger Vault

A dependency-free Python reference implementation for security, audit, append-only.

Run with: python3 demo.py
Tests: python3 -m unittest discover -s tests -v

## Scope

实现一个仅依赖 Python 标准库的版本化配置仓：所有写入以追加记录保存，支持按名称读取指定版本、列出版本、查询当前活动版本和整体重载。重载必须先完整校验记录链与摘要，任何损坏都不能替换当前内存快照；读者只能看到重载前或重载后的完整状态。公开接口、磁盘格式、异常类型和命令行行为必须稳定，全部结果可由离线脚本独立验收。

`snapshot_at(version=None)` 提供面向整个仓库的历史时点读取：按全局记录序号重建该时点每个名称最后一条记录（名称稳定排序、返回深拷贝）；`version=0` 为空快照，省略或传入最新序号返回当前已加载状态；类型错误、负数或超出已加载记录总数均抛出 `ValueError`。该入口只读：不写日志、不改活动版本、不触发追加；日志被外部改坏后仍只反映最近一次成功 reload 的内存状态。命令行 `snapshot` 子命令（`--root`、可选 `--version`）输出 `json.loads` 可解析、排序确定的同一结果，非法时点以状态 1 退出且不输出部分结果。

`put_batch(entries)` 一次提交多个不同配置项：`entries` 为非空 list 或 tuple，每个元素是恰好包含名称和值的二元 list 或 tuple，名称为非空字符串且批内不得重复，值沿用 `put` 的 JSON 往返可存储规则。容器、元素形状、空批次、名称非法或重复抛 `ValueError`，值无法保存抛 `TypeError`；全部校验在建目录、打开日志或改变内存快照之前完成，失败后磁盘字节、当前快照和下一版本号不变。校验通过后记录按传入顺序连续占用全局版本号，返回同序的整数版本列表；整个批次在一次写入协调内完成并由单次 reload 发布，读者只能看到提交前或提交后的完整状态。
