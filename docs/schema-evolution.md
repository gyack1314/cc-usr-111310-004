# Schema 演进层规则

本层在不重写历史数据文件的前提下，把旧 writer 行按 reader schema 投影。所有判断基于全表唯一且永不复用的整数 `field_id`（含 root、struct 字段、list element、map key/value），名字只作显示。

## 身份与匹配

- 节点身份 = `field_id` + 原父子关系。重命名同 ID 同父子 → 兼容，差异标 `renamed`。
- 同 ID 但父节点变化 = 跨父移动，编译期拒绝，不能仅凭同 ID 当兼容。
- 同名不同 ID 不是重命名：差异里分别标 `dropped` 与 `added`，并带 `name_reused` 互相指向；旧字段值进入删除损失。
- `retired_ids` 中的 ID 被任何新 schema 复用、未知类型、schema 内 ID 重复，在校验阶段先拒绝。

## 允许与拒绝的变更

| 变更 | 处理 |
| --- | --- |
| 新增字段，带冻结常量 `default` | 允许；默认值只在字段物理缺席时写入 |
| 新增可选字段无 default | 允许；缺席读为 null |
| 新增必选字段无 default | 拒绝 |
| 删除字段 | 允许但记信息损失，计划 `dropped[].loss` 列出整棵子树 ID |
| int32 → int64 | 允许，数值精确保留 |
| 其它原语类型互转（含 bool 当整数） | 拒绝 |
| decimal 提升 | 仅当新 scale ≥ 旧 scale 且新整数位容量（precision−scale）≥ 旧值 |
| decimal 降低 scale / 缩整数位 | 拒绝（需要舍入或会超界） |
| 可选 → 必选 | 条件允许：对绑定文件集所有行所有实际路径扫描证明非 null |
| map 键类型/身份变化 | 拒绝；键必须是必选 string |
| 容器类型互换 | 拒绝 |

## 空值与容器形状

- 显式 null 与物理缺席分开：null 不被默认值替代；缺席的可选无 default 字段读 null。
- null struct 不生成任何子默认字段；空 struct 的缺席子字段按各自规则投影。
- list 区分 null 列表、`[]`、null 元素、`[{}]`，四者互异。
- map 的 value 为 null 时键仍然存在（value 缺失不等于键不存在）。
- map 同键重复在 JSON 解析期即判键碰撞，拒绝静默覆盖。
- decimal 以精确十进制字符串存储与往返；投影不做 float 中转，超界直接拒绝不截断。

## 投影计划与逐值来源

- `compile_plan` 输出可序列化计划树（struct/list/map/leaf 节点 + additions + dropped），与 schema 差异、数据文件相互独立。
- 投影逐节点新建容器（结构化重建，非通用 JSON 深拷贝），源容器不会被共享或改写。
- 每个投影值带 lineage：`target_path`/`source_path` 为 `root.2.3[].5[k].7` 形式的 ID 路径，`status` 区分 `projected`、`explicit_null`、`absent_as_null`、`added_default`、`added_optional_null`、`dropped_value_lost`、`null_list`、`empty_list`、`null_element`、`null_map_value` 等。

## 提案、提交与快照

1. `propose`：校验两个 schema → 编译计划 → 按 manifest 资产摘要（bytes/sha256/rows）核对并读取绑定文件 → 必选证明扫描 → 全量投影试跑（decimal 超界在此暴露），产出绑定文件集摘要与 schema 摘要。
2. `commit`：重新计算当前 schema 摘要与文件集摘要，与提案绑定值比对；检查 `append_after_validation` 是否有验证后并发到达的旧 schema 文件；再核对磁盘字节并复跑扫描与投影。任一不符即拒绝，原 manifest 不变，输出经临时文件原子替换。
3. 读取：无 plan 的旧 manifest 按 writer 原编码直读（旧快照继续用旧 schema）；带 plan 的新 manifest 统一投影到 reader。
