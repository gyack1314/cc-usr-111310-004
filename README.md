# 嵌套湖表结构演进的字段ID投影与迁移

此项目保存生产与供应链协同的湖仓原始资料。`warehouse_data.io`读取JSON清单及核对文件身份，`warehouse_data.domain`检查基本形状并输出数量，`warehouse_data.cli`展示摘要。[资料约定](docs/data-contract.md)定义字段、类型、时间、数量和版本范围。设备、批次、组织、事件和成本均为合成，不连接真实工厂或生产仓库。

Python3.11以上，无第三方Python依赖。项目根目录运行：

```sh
python -m unittest discover -s tests -v
python -m compileall -q warehouse_data tests
python -m warehouse_data.cli inspect data/normal.json
```

`inspect`只读原始资料，不生成仓库新版本或计算领域结果。`invalid-config.json`有基本形状错误；其余清单有完整资料身份，但可能包含需要业务操作判断的冲突、缺口或不满足条件的边界。null与缺字段分别保留，有理数和decimal用字符串存储，不能先经float丢精度。

## Schema 演进层

`warehouse_data.schema`校验节点形状并产出**独立schema差异**，`warehouse_data.evolution`把writer schema编译成reader投影计划、逐值投影旧行、做可选→必选的全量扫描证明，并在复核后写新manifest。身份按全表唯一`field_id`加原父子关系：重命名不改变身份，同ID跨父移动不兼容，同名不同ID按“删除+新增”处理。

```sh
python -m warehouse_data.cli diff data/normal.json
python -m warehouse_data.cli propose data/normal.json
python -m warehouse_data.cli commit data/normal.json out/mf2.json
python -m warehouse_data.cli read out/mf2.json --data-dir data
```

允许的变更：新增带冻结常量默认值的字段或可选无默认字段、删字段（损失在计划`dropped.loss`列明）、int32→int64、不降低scale且整数位容量足够的decimal提升、可选→必选（须对绑定文件集逐行扫描证明非null）。默认值只用于字段物理缺席，显式null保持null；null列表、空列表、null元素、含空struct元素四种形状互异并原样保留。reader出现retired ID复用、未知类型、非法ID重复先拒绝；map键碰撞与decimal超界不截断，直接报错。

提案先在manifest绑定的固定文件集验证（字节/sha256/行数核对），提交时复核当前schema与文件集摘要未变；`append_after_validation`里的并发新增旧schema文件会使非null证明失效并拒绝提交。旧manifest（无plan）按writer原编码直读，新manifest统一按reader投影；提交失败不修改当前版本，输出走临时文件原子替换。

`data/evolution/`是固定演示集：同名不同ID、decimal临界提升与超界、必选证明成功与验证后新文件到达、map键碰撞。演示数据由`scripts/build_demo_data.py`从JSONL重新计算字节摘要生成。[演进规则细节](docs/schema-evolution.md)。


Dockerfile固定Linux镜像，`docker build -t warehouse-material .`构建；`docker run --rm warehouse-material`显示摘要；`docker run --rm warehouse-material python -m unittest discover -s tests -v`运行已有检查。所有资料本地可读，不需要账号、外部数据库或在线数据源。
