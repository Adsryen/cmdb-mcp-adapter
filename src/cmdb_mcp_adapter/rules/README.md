# rules/ — 规则文件位（包内＝卫生版・真源⛔入仓）

git 里提交的 `cmdb_rules.json` 是**卫生版**：与 ops 真源结构逐点相等、闸门阈值与
形状规则原样保留，但内网地址／云实例 ID／项目名册／工单号等现网实值全部打码
（`<ip:redacted>` 等占位）。真源本体只存 ops 仓，⛔ 进本公开仓。

真源（唯一可编辑处，位于 ops 仓）：

```
ops/VeOps/cmdb-rules/cmdb_rules.json
```

卫生版生成（ops 仓内管道・确定性・结构不变自检）：

```
ops/VeOps/cmdb-rules/redact_rules.py --in cmdb_rules.json --out public/cmdb_rules.public.json
```

运行期查找顺序（`rules_gate.resolve_rules_path()` 实现）：

1. `$CMDB_RULES_PATH` —— 显式指定＝错了就报错，不静默降级（排障入口）
2. `/opt/veops-mcp-adapter/rules/cmdb_rules.json` —— 部署机放**真源**处，仓外；
   写闸数值以这份为准
3. 包内 `src/cmdb_mcp_adapter/rules/cmdb_rules.json` —— 即卫生版（结构+阈值齐、
   实值打码），本地开发与无注入环境用
4. 都没有 ⇒ 意图级工具进入**降级不猜**模式：数值/形制/禁符闸跳过并随
   载荷回 WARN，词表校验改走 `get_models` 现读（⛔ 猜阈值、⛔ 启动失败）

构建注意：

- 镜像/wheel 从 **git 干净检出**构建 ⇒ 包内自动就是卫生版。
- 若在工作树里把真源 cp 到本目录自测，构建前务必 `git checkout -- src/.../rules/cmdb_rules.json`
  还原，或显式走部署注入（下面第二条），⛔ 把真源烘进镜像。

```
# 自测注入（改完即还原）：
cp ops/VeOps/cmdb-rules/cmdb_rules.json src/cmdb_mcp_adapter/rules/
# 部署机注入真值（/opt 面・不进包）：
install -Dm644 ops/VeOps/cmdb-rules/cmdb_rules.json \
  /opt/veops-mcp-adapter/rules/cmdb_rules.json
```

漂移与卫生自证（`tests/test_rules_vendored.py`）：

- 常驻检查：包内卫生版形状 0 命中（含 `<...:redacted>` 占位存在性）＝CI 每次必跑，
  把真源误提交进来会直接判红。
- 可选漂移：设 `CMDB_RULES_SOURCE_PATH` 指向真源、`CMDB_REDACT_SCRIPT` 指向
  `redact_rules.py`（默认取真源同目录），测试现场重跑打码器并逐字节对撞包内文件；
  任一 env 未设＝跳过（CI 无真源属预期）。
