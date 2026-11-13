"""Schema 形状校验、field_id 索引与独立 schema 差异。

身份规则（见 docs/data-contract.md）：
- 全表节点（root、struct 字段、list element、map key/value）持全表唯一且不复用的整数 field_id；
- 名字仅用于显示，匹配按 ID 与原父子关系，重命名不改变身份；
- map 键必选且只能是 string。

本模块只做结构层面的事实描述，不生成投影计划；差异结果是纯数据，
可在不触碰数据文件的情况下单独检查。
"""
from __future__ import annotations

PRIMITIVES = {'bool', 'int32', 'int64', 'decimal', 'string'}
CONTAINERS = {'struct', 'list', 'map'}
ALL_TYPES = PRIMITIVES | CONTAINERS


class SchemaError(ValueError):
    """schema 形状非法（未知类型、ID 重复、retired ID 复用、map 键非法等）。"""


def validate(node, retired_ids=()):
    """递归校验 schema 节点形状，返回按 ID 去重后的 retired 集合。

    校验失败即抛 SchemaError；retired_ids 中任何 ID 被新 schema 复用也拒绝。
    """
    if not isinstance(retired_ids, (list, tuple, set)) or any(type(x) is not int for x in retired_ids):
        raise SchemaError('retired_ids 必须是整数集合')
    retired = set(retired_ids)
    seen = set()

    def walk(n, in_map_key=False):
        if not isinstance(n, dict):
            raise SchemaError('节点必须是对象')
        fid = n.get('id')
        if type(fid) is not int or fid < 0:
            raise SchemaError('field_id 必须是非负整数')
        if fid in seen:
            raise SchemaError(f'field_id {fid} 在 schema 中重复')
        seen.add(fid)
        if fid in retired:
            raise SchemaError(f'field_id {fid} 属于 retired_ids，禁止复用')
        name = n.get('name')
        if not isinstance(name, str) or not name:
            raise SchemaError(f'节点 {fid} 名字缺失')
        t = n.get('type')
        if t not in ALL_TYPES:
            raise SchemaError(f'节点 {fid} 类型未知: {t!r}')
        if type(n.get('required')) is not bool:
            raise SchemaError(f'节点 {fid} required 必须是布尔')
        if t == 'struct':
            children = n.get('children')
            if not isinstance(children, list) or not children:
                raise SchemaError(f'struct {fid} 必须有非空 children')
            for c in children:
                walk(c)
        elif t == 'list':
            walk(n['element']) if 'element' in n else _missing(fid, 'element')
        elif t == 'map':
            if 'key' not in n:
                _missing(fid, 'key')
            walk(n['key'], in_map_key=True)
            if 'value' not in n:
                _missing(fid, 'value')
            walk(n['value'])
        else:
            if t == 'decimal':
                p, s = n.get('precision'), n.get('scale')
                if type(p) is not int or type(s) is not int or p < 1 or s < 0 or s > p:
                    raise SchemaError(f'decimal {fid} precision/scale 非法')
            if 'default' in n:
                _check_default(fid, t, n['default'])
        if in_map_key and (t != 'string' or n.get('required') is not True):
            raise SchemaError(f'节点 {fid} 作为 map 键必须是必选 string')

    walk(node)
    if node.get('type') != 'struct':
        raise SchemaError('顶层 schema 必须是 struct')
    return retired


def _missing(fid, part):
    raise SchemaError(f'节点 {fid} 缺少 {part}')


def _check_default(fid, t, d):
    if d is None:
        raise SchemaError(f'字段 {fid} 冻结默认值不能为 null（显式 null 不来自默认值）')
    if t == 'bool':
        if type(d) is not bool:
            raise SchemaError(f'字段 {fid} 默认值类型不是 bool')
    elif t in ('int32', 'int64'):
        if type(d) is not int or isinstance(d, bool):
            raise SchemaError(f'字段 {fid} 默认值类型不是整数')
        if t == 'int32' and not (-(2 ** 31) <= d <= 2 ** 31 - 1):
            raise SchemaError(f'字段 {fid} int32 默认值越界')
    elif t == 'string':
        if type(d) is not str:
            raise SchemaError(f'字段 {fid} 默认值类型不是 string')
    elif t == 'decimal':
        _check_decimal_literal(fid, d)


def _check_decimal_literal(fid, d):
    if type(d) is not str or not d:
        raise SchemaError(f'字段 {fid} decimal 默认值必须是十进制字符串')
    body = d[1:] if d[0] in '+-' else d
    if not body or any(ch not in '0123456789.' for ch in body) or body.count('.') > 1:
        raise SchemaError(f'字段 {fid} decimal 默认值只能是精确十进制字符串，禁止指数/浮点')
    intpart, _, frac = body.partition('.')
    if not intpart or (frac == '' and '.' in body):
        raise SchemaError(f'字段 {fid} decimal 默认值形状非法')


def index_nodes(node):
    """返回 {field_id: node} 全表索引，含 root 与容器 element/key/value。"""
    out = {}

    def walk(n):
        out[n['id']] = n
        t = n['type']
        if t == 'struct':
            for c in n['children']:
                walk(c)
        elif t == 'list':
            walk(n['element'])
        elif t == 'map':
            walk(n['key'])
            walk(n['value'])

    walk(node)
    return out


def parent_map(node):
    """返回 {field_id: 父节点 field_id}（root 的父为 None）。"""
    parents = {}

    def walk(n, parent):
        parents[n['id']] = parent
        t = n['type']
        if t == 'struct':
            for c in n['children']:
                walk(c, n['id'])
        elif t == 'list':
            walk(n['element'], n['id'])
        elif t == 'map':
            walk(n['key'], n['id'])
            walk(n['value'], n['id'])

    walk(node, None)
    return parents


def diff(writer, reader):
    """生成独立 schema 差异（不依赖数据文件、不产生投影计划）。

    匹配按 field_id 与原父子关系；同名不同 ID 是“删除 + 新增”而非重命名，
    会额外标注 name_reused。返回纯数据字典。
    """
    w_idx, r_idx = index_nodes(writer), index_nodes(reader)
    w_par, r_par = parent_map(writer), parent_map(reader)
    changes = []

    def node_ref(n, parent):
        return {'id': n['id'], 'name': n['name'], 'type': n['type'], 'parent_id': parent}

    def record(kind, **kw):
        changes.append({'kind': kind, **kw})

    # 仅改名：同 ID、同父子关系、名字不同。
    for fid, rn in r_idx.items():
        if fid in w_idx:
            wn = w_idx[fid]
            if wn['name'] != rn['name'] and w_par[fid] == r_par[fid]:
                record('renamed', id=fid, parent_id=r_par[fid],
                       writer_name=wn['name'], reader_name=rn['name'])

    # reader 侧新增（含同 ID 跨父移动：在新父下视为新增，旧父下视为删除）。
    for fid, rn in r_idx.items():
        if fid not in w_idx or w_par[fid] != r_par[fid]:
            entry = node_ref(rn, r_par[fid])
            same_name = [w for w in w_idx.values()
                         if w['name'] == rn['name'] and w['id'] != fid]
            if same_name:
                entry['name_reused'] = True
                entry['shadows_writer_id'] = same_name[0]['id']
            record('added', **entry)

    # writer 侧删除。
    for fid, wn in w_idx.items():
        if fid not in r_idx or w_par[fid] != r_par[fid]:
            entry = node_ref(wn, w_par[fid])
            same_name = [r for r in r_idx.values()
                         if r['name'] == wn['name'] and r['id'] != fid]
            if same_name:
                entry['name_reused'] = True
                entry['shadowed_by_reader_id'] = same_name[0]['id']
            record('dropped', **entry)

    # 同 ID 同父子的类型/参数/required 变化。
    for fid, rn in r_idx.items():
        if fid not in w_idx or w_par[fid] != r_par[fid]:
            continue
        wn = w_idx[fid]
        if wn['type'] != rn['type']:
            record('type_changed', id=fid, parent_id=r_par[fid],
                   writer_type=wn['type'], reader_type=rn['type'])
            continue
        if wn['type'] == 'decimal' and (wn['precision'], wn['scale']) != (rn['precision'], rn['scale']):
            record('decimal_widening', id=fid, parent_id=r_par[fid],
                   writer_precision=wn['precision'], writer_scale=wn['scale'],
                   reader_precision=rn['precision'], reader_scale=rn['scale'])
        if wn.get('required') != rn.get('required'):
            record('required_tightened' if rn['required'] else 'required_loosened',
                   id=fid, parent_id=r_par[fid],
                   writer_required=wn['required'], reader_required=rn['required'])
        if wn.get('default') != rn.get('default'):
            record('default_changed', id=fid, parent_id=r_par[fid],
                   writer_default=wn.get('default'), reader_default=rn.get('default'))

    order = {'renamed': 0, 'added': 1, 'dropped': 2, 'type_changed': 3,
             'decimal_widening': 4, 'required_tightened': 5, 'required_loosened': 6,
             'default_changed': 7}
    changes.sort(key=lambda c: (order[c['kind']], c.get('id', -1)))
    return {
        'writer_root_id': writer['id'],
        'reader_root_id': reader['id'],
        'matched_ids': sorted(i for i in r_idx if i in w_idx and w_par[i] == r_par[i]),
        'changes': changes,
    }
