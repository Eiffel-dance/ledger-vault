# Ledger Vault

A dependency-free Python reference implementation for security, audit, append-only.

Run with: python3 demo.py
Tests: python3 -m unittest discover -s tests -v

## Scope

实现一个仅依赖 Python 标准库的版本化配置仓：所有写入以追加记录保存，支持按名称读取指定版本、列出版本、查询当前活动版本和整体重载。重载必须先完整校验记录链与摘要，任何损坏都不能替换当前内存快照；读者只能看到重载前或重载后的完整状态。公开接口、磁盘格式、异常类型和命令行行为必须稳定，全部结果可由离线脚本独立验收。
