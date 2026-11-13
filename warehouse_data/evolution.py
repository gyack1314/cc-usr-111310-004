"""编译 writer→reader 投影计划，并在明确允许的变更范围内逐值迁移旧行。

设计边界：
- 身份按 field_id + 原父子关系；重命名是兼容变化，跨父移动不兼容；
- 允许：新增冻结常量默认字段/可选无默认字段、删字段、int32→int64、
  不降低 scale 且整数位容量足够的 decimal 提升、可选→必选（需扫描证明）；
- 拒绝：未知类型/已删除 ID 复用/非法 ID 重复、map 键类型变化、map 键碰撞、
  decimal 超界截断、跨父移动、不兼容的类型变化；
- 投影是结构化重建（逐节点 new 容器），不使用通用 JSON 深拷贝；
- 默认值只在字段物理缺席时使用，显式 null 保持 null；
- 结构空值层级（null struct / null list / null map / null 元素）原样保留，
  []、null、[null]、[{}] 互异。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation

from . import io as warehouse_io
from .schema import (
    SchemaError, validate, index_nodes, parent_map, diff,
)

PLAN_VERSION = 1


class PlanError(ValueError):
    """差异超出允许的演进范围，投影计划无法编译。"""


class ProjectionError(ValueError):
    """逐值投影时数据违反 reader 约束（decimal 超界、map 键碰撞等）。"""


class CommitError(ValueError):
    """提交复核失败：schema/文件集漂移，或并发新增文件使非 null 证明失效。"""


# --------------------------------------------------------------------------- #
# 计划编译
# --------------------------------------------------------------------------- #

def compile_plan(writer, reader):
    """把两个 schema 编译成可序列化的投影计划树。

    非法 schema 形状抛 SchemaError；差异不兼容抛 PlanError。
    返回 (plan_dict, required_tightened_ids)，后者是需要扫描证明的节点 ID。
    """
    validate(writer)
    validate(reader)
    if writer['id'] != reader['id']:
        raise PlanError(f'root field_id 不一致: {writer["id"]} != {reader["id"]}')
    w_idx, r_idx = index_nodes(writer), index_nodes(reader)
    w_par, r_par = parent_map(writer), parent_map(reader)
    tightened = []

    def build(wn, rn, parent_id):
        fid = rn['id']
        if fid not in w_idx:
            raise PlanError(f'field_id {fid}（{rn["name"]}）在 writer 中不存在')
        if w_par[fid] != parent_id or r_par[fid] != parent_id:
            raise PlanError(
                f'field_id {fid}（{rn["name"]}）跨父移动：writer 父 {w_par[fid]} '
                f'→ reader 父 {r_par[fid]}，同 ID 不构成兼容')
        if wn['type'] != rn['type']:
            # 唯一允许的跨类型变化：int32→int64（在 leaf 构建中再确认）。
            if not (wn['type'] == 'int32' and rn['type'] == 'int64'):
                raise PlanError(
                    f'field_id {fid} 类型变化 {wn["type"]}→{rn["type"]} 不兼容')
        if not wn['required'] and rn['required']:
            tightened.append(fid)
        if rn['type'] == 'struct':
            return _build_struct(wn, rn, parent_id)
        if rn['type'] == 'list':
            return _build_list(wn, rn, parent_id)
        if rn['type'] == 'map':
            return _build_map(wn, rn, parent_id)
        return _build_leaf(wn, rn)

    def _build_struct(wn, rn, parent_id):
        w_children = {c['id']: c for c in wn['children']}
        entries, additions, dropped_here = [], [], []
        for rc in rn['children']:
            cid = rc['id']
            if cid in w_children and w_par[cid] == rn['id']:
                entries.append(build(w_children[cid], rc, rn['id']))
            elif cid in w_idx and w_par[cid] != rn['id']:
                # ID 存在但挂在别的父下：跨父移动，不能当新增字段。
                raise PlanError(
                    f'field_id {cid}（{rc["name"]}）跨父移动：writer 父 {w_par[cid]} '
                    f'→ reader 父 {rn["id"]}，同 ID 不构成兼容')
            else:
                additions.append(_build_addition(rc))
        r_ids = {c['id'] for c in rn['children']}
        for wc in wn['children']:
            if wc['id'] not in r_ids or r_par.get(wc['id']) != rn['id']:
                dropped_here.append(_drop_entry(wc))
        return {'op': 'struct', 'id': rn['id'], 'name': rn['name'],
                'required': rn['required'], 'entries': entries,
                'additions': additions, 'dropped': dropped_here}

    def _build_list(wn, rn, parent_id):
        we, re_ = wn['element'], rn['element']
        if we['id'] != re_['id']:
            raise PlanError(
                f'list field_id {rn["id"]} 的 element ID 变化 {we["id"]}→{re_["id"]}')
        if not we['required'] and re_['required']:
            tightened.append(re_['id'])
        child = build(we, re_, rn['id'])
        return {'op': 'list', 'id': rn['id'], 'name': rn['name'],
                'required': rn['required'], 'element': child,
                'element_required': re_['required']}

    def _build_map(wn, rn, parent_id):
        wk, rk = wn['key'], rn['key']
        if wk['id'] != rk['id'] or rk['type'] != 'string' or not rk['required']:
            raise PlanError(f'map field_id {rn["id"]} 键身份/类型变化，禁止改键类型')
        wv, rv = wn['value'], rn['value']
        if wv['id'] != rv['id']:
            raise PlanError(f'map field_id {rn["id"]} value ID 变化 {wv["id"]}→{rv["id"]}')
        if not wv['required'] and rv['required']:
            tightened.append(rv['id'])
        return {'op': 'map', 'id': rn['id'], 'name': rn['name'],
                'required': rn['required'],
                'key_id': rk['id'], 'value': build(wv, rv, rn['id']),
                'value_required': rv['required']}

    def _build_leaf(wn, rn):
        wt, rt = wn['type'], rn['type']
        transform = 'keep'
        if wt == rt:
            if wt == 'decimal' and (wn['precision'], wn['scale']) != (rn['precision'], rn['scale']):
                _check_decimal_promotable(wn, rn)
                transform = 'decimal_widen'
        elif wt == 'int32' and rt == 'int64':
            transform = 'int32_to_int64'
        else:
            raise PlanError(f'field_id {rn["id"]} 原语类型变化 {wt}→{rt} 不兼容（bool 不可当整数）')
        return {'op': 'leaf', 'id': rn['id'], 'name': rn['name'],
                'required': rn['required'], 'writer_type': wt, 'reader_type': rt,
                'transform': transform,
                'reader_precision': rn.get('precision'), 'reader_scale': rn.get('scale')}

    root = build(writer, reader, None)

    # 防御：reader 中任何曾删除 ID（retired）已在 validate 阶段拒绝；
    # 这里再确认没有 writer 节点被静默丢弃在树外。
    planned = set()

    def collect(node):
        planned.add(node['id'])
        if node['op'] == 'struct':
            for e in node['entries']:
                collect(e)
        elif node['op'] == 'list':
            collect(node['element'])
        elif node['op'] == 'map':
            planned.add(node['key_id'])
            collect(node['value'])

    collect(root)
    for fid in w_idx:
        if fid not in planned and fid not in _all_dropped_ids(root):
            raise PlanError(f'field_id {fid} 未出现在投影计划中，拒绝静默丢字段')

    root['plan_version'] = PLAN_VERSION
    return root, sorted(set(tightened))


def _all_dropped_ids(root):
    out = set()

    def walk(node):
        if node['op'] == 'struct':
            for d in node['dropped']:
                _collect_subtree_ids(d['node'], out)
            for e in node['entries']:
                walk(e)
        elif node['op'] == 'list':
            walk(node['element'])
        elif node['op'] == 'map':
            walk(node['value'])

    walk(root)
    return out


def _collect_subtree_ids(node, out):
    out.add(node['id'])
    t = node['type']
    if t == 'struct':
        for c in node['children']:
            _collect_subtree_ids(c, out)
    elif t == 'list':
        _collect_subtree_ids(node['element'], out)
    elif t == 'map':
        _collect_subtree_ids(node['key'], out)
        _collect_subtree_ids(node['value'], out)


def _check_decimal_promotable(wn, rn):
    p0, s0 = wn['precision'], wn['scale']
    p1, s1 = rn['precision'], rn['scale']
    if s1 < s0:
        raise PlanError(f'field_id {rn["id"]} decimal scale 降低 {s0}→{s1}，需要舍入，拒绝')
    if p1 - s1 < p0 - s0:
        raise PlanError(f'field_id {rn["id"]} decimal 整数位容量不足：'
                        f'{p0 - s0} 位 → {p1 - s1} 位')


def _build_addition(rn):
    """reader 新增节点：允许冻结常量默认值，或可选无默认。"""
    has_default = 'default' in rn
    if rn['type'] in ('struct', 'list', 'map'):
        if has_default:
            raise PlanError(f'新增容器 field_id {rn["id"]} 不允许默认值')
        if rn['required']:
            raise PlanError(f'新增必选容器 field_id {rn["id"]} 无物理来源，拒绝')
    elif rn['required'] and not has_default:
        raise PlanError(f'新增必选字段 field_id {rn["id"]} 没有冻结默认值，拒绝')
    return {'id': rn['id'], 'name': rn['name'], 'type': rn['type'],
            'required': rn['required'],
            'has_default': has_default, 'default': rn.get('default'),
            'node': rn}


def _drop_entry(wn):
    """删除节点：记录整棵子树的信息损失，计划中显式列明。"""
    subtree = []

    def walk(n, path):
        subtree.append({'id': n['id'], 'name': n['name'], 'type': n['type'],
                        'id_path': path + [n['id']]})
        t = n['type']
        if t == 'struct':
            for c in n['children']:
                walk(c, path + [n['id']])
        elif t == 'list':
            walk(n['element'], path + [n['id']])
        elif t == 'map':
            walk(n['key'], path + [n['id']])
            walk(n['value'], path + [n['id']])

    walk(wn, [])
    return {'id': wn['id'], 'name': wn['name'], 'type': wn['type'],
            'node': wn, 'loss': subtree}


# --------------------------------------------------------------------------- #
# 逐值投影（结构化重建，非 JSON 深拷贝）
# --------------------------------------------------------------------------- #

def render_path(ids):
    """ID 路径渲染成 root.2[].3.4 形式（容器子节点带 []，map 键固定 string）。"""
    out = 'root'
    for fid in ids:
        out += f'.{fid}'
    return out


class Projector:
    """按计划把 writer 编码行投影成 reader 编码行，并产出逐值来源路径。"""

    def __init__(self, plan):
        self.plan = plan

    def project_row(self, row_value):
        if not isinstance(row_value, dict):
            raise ProjectionError('行 value 必须是 struct 对象')
        lineage = []
        out = self._struct(self.plan, row_value, [], [], lineage)
        return {'value': out, 'lineage': lineage}

    # -- struct ------------------------------------------------------------- #
    def _struct(self, node, src, t_path, s_path, lineage):
        if src is None:
            # 结构空值层级保留：null struct 不生成任何子默认字段。
            lineage.append(self._mark(node, t_path, s_path, 'null_struct'))
            return None
        if not isinstance(src, dict):
            raise ProjectionError(f'field_id {node["id"]} 期望 struct，实际 {type(src).__name__}')
        out = {}
        for entry in node['entries']:
            fid = str(entry['id'])
            child_t = t_path + [entry['id']]
            child_s = s_path + [entry['id']]
            # 匹配字段无论 null 与否都写回，结构空值层级不能丢。
            if fid in src:
                self._dispatch(entry, src[fid], out, entry['id'], child_t, child_s, lineage)
            else:
                self._absent(entry, out, entry['id'], child_t, child_s, lineage)
        for add in node['additions']:
            self._apply_addition(add, out, t_path + [add['id']], lineage)
        for d in node['dropped']:
            self._record_drop(d, src, s_path, lineage)
        return out

    def _dispatch(self, node, val, out, key, t_path, s_path, lineage):
        op = node['op']
        out_key = str(key)  # struct 物理键是字段 ID 的字符串
        if op == 'struct':
            out[out_key] = self._struct(node, val, t_path, s_path, lineage)
        elif op == 'list':
            out[out_key] = self._list(node, val, t_path, s_path, lineage)
        elif op == 'map':
            out[out_key] = self._map(node, val, t_path, s_path, lineage)
        else:
            out[out_key] = self._leaf(node, val, t_path, s_path, lineage)

    def _absent(self, node, out, key, t_path, s_path, lineage):
        """物理缺席：新增/可选读为 null；显式 null 不走这里。"""
        if node['op'] == 'leaf' and node['required']:
            raise ProjectionError(f'field_id {node["id"]} 必选字段在源行物理缺席')
        lineage.append(self._mark(node, t_path, s_path, 'absent_as_null'))
        out[str(key)] = None

    def _apply_addition(self, add, out, t_path, lineage):
        fid = str(add['id'])
        if add['has_default']:
            out[fid] = add['default']  # 冻结常量，只用于物理缺席
            lineage.append({'target_path': render_path(t_path), 'source_path': None,
                            'field_id': add['id'], 'name': add['name'],
                            'status': 'added_default', 'detail': add['default']})
        else:
            out[fid] = None
            lineage.append({'target_path': render_path(t_path), 'source_path': None,
                            'field_id': add['id'], 'name': add['name'],
                            'status': 'added_optional_null'})

    def _record_drop(self, drop, src, s_path, lineage):
        present = str(drop['id']) in src
        status = 'dropped_value_lost' if present else 'dropped_absent'
        lineage.append({'target_path': None,
                        'source_path': render_path(s_path + [drop['id']]),
                        'field_id': drop['id'], 'name': drop['name'],
                        'status': status,
                        'lost_subtree_ids': [x['id'] for x in drop['loss']]})

    # -- list --------------------------------------------------------------- #
    def _list(self, node, val, t_path, s_path, lineage):
        if val is None:
            lineage.append(self._mark(node, t_path, s_path, 'null_list'))
            return None  # null 列表与空列表互异
        if not isinstance(val, list):
            raise ProjectionError(f'field_id {node["id"]} 期望 list，实际 {type(val).__name__}')
        out = []
        for i, item in enumerate(val):
            et = t_path  # 元素 ID 路径与列表同节点后接 element id
            es = s_path
            elem_t = t_path + [node['element']['id']]
            elem_s = s_path + [node['element']['id']]
            if item is None:
                # null 元素保留；必选元素遇 null 属数据违约。
                if node['element_required']:
                    raise ProjectionError(
                        f'field_id {node["element"]["id"]} 必选元素在位置 {i} 为 null')
                lineage.append({'target_path': render_path(elem_t) + '[]',
                                'source_path': render_path(elem_s) + '[]',
                                'field_id': node['element']['id'],
                                'name': node['element']['name'],
                                'status': 'null_element', 'index': i})
                out.append(None)
            else:
                out.append(self._project_child(node['element'], item, elem_t, elem_s,
                                               lineage, index=i))
        if not val:
            lineage.append(self._mark(node, t_path, s_path, 'empty_list'))
        return out

    # -- map ---------------------------------------------------------------- #
    def _map(self, node, val, t_path, s_path, lineage):
        if val is None:
            lineage.append(self._mark(node, t_path, s_path, 'null_map'))
            return None
        if not isinstance(val, dict):
            raise ProjectionError(f'field_id {node["id"]} 期望 map，实际 {type(val).__name__}')
        out = {}
        for k, v in val.items():
            if not isinstance(k, str):
                raise ProjectionError(f'field_id {node["id"]} map 键必须是 string')
            if k in out:  # 解析期已查重，这里是运行期防线
                raise ProjectionError(f'field_id {node["id"]} map 键碰撞: {k!r}，拒绝合并')
            vt = t_path + [node['value']['id']]
            vs = s_path + [node['value']['id']]
            if v is None:
                # value 缺失（显式 null）不是键不存在：键保留，value 为 null。
                if node['value_required']:
                    raise ProjectionError(
                        f'field_id {node["value"]["id"]} 必选 map value 在键 {k!r} 为 null')
                lineage.append({'target_path': render_path(vt) + f'[{k!r}]',
                                'source_path': render_path(vs) + f'[{k!r}]',
                                'field_id': node['value']['id'],
                                'name': node['value']['name'],
                                'status': 'null_map_value', 'key': k})
                out[k] = None
            else:
                out[k] = self._project_child(node['value'], v, vt, vs, lineage, key=k)
        if not val:
            lineage.append(self._mark(node, t_path, s_path, 'empty_map'))
        return out

    def _project_child(self, child, val, t_path, s_path, lineage, index=None, key=None):
        if child['op'] == 'struct':
            return self._struct(child, val, t_path, s_path, lineage)
        if child['op'] == 'list':
            return self._list(child, val, t_path, s_path, lineage)
        if child['op'] == 'map':
            return self._map(child, val, t_path, s_path, lineage)
        return self._leaf(child, val, t_path, s_path, lineage, index=index, key=key)

    # -- leaf --------------------------------------------------------------- #
    def _leaf(self, node, val, t_path, s_path, lineage, index=None, key=None):
        if val is None:
            if node['required']:
                raise ProjectionError(f'field_id {node["id"]} 必选字段显式 null')
            lineage.append(self._mark(node, t_path, s_path, 'explicit_null', index, key))
            return None
        rt = node['reader_type']
        out = self._convert_leaf(node, val)
        mark = {'target_path': render_path(t_path), 'source_path': render_path(s_path),
                'field_id': node['id'], 'name': node['name'], 'status': 'projected',
                'transform': node['transform']}
        if index is not None:
            mark['index'] = index
        if key is not None:
            mark['key'] = key
        lineage.append(mark)
        return out

    def _convert_leaf(self, node, val):
        fid, rt = node['id'], node['reader_type']
        if rt == 'bool':
            if type(val) is not bool:
                raise ProjectionError(f'field_id {fid} 期望 bool')
            return val
        if rt == 'string':
            if type(val) is not str:
                raise ProjectionError(f'field_id {fid} 期望 string')
            return val
        if rt in ('int32', 'int64'):
            if type(val) is not int or isinstance(val, bool):
                raise ProjectionError(f'field_id {fid} 期望整数，bool 不可当整数')
            if rt == 'int32' and not (-(2 ** 31) <= val <= 2 ** 31 - 1):
                raise ProjectionError(f'field_id {fid} int32 越界')
            return val  # int32→int64 精确保留
        if rt == 'decimal':
            return self._decimal(node, val)
        raise ProjectionError(f'field_id {fid} 未知 reader 类型 {rt}')

    def _decimal(self, node, val):
        fid = node['id']
        if type(val) is not str:
            raise ProjectionError(f'field_id {fid} decimal 必须是精确十进制字符串，禁止浮点')
        body = val[1:] if val[:1] in '+-' else val
        if not body or any(ch not in '0123456789.' for ch in body) or body.count('.') > 1:
            raise ProjectionError(f'field_id {fid} decimal 形状非法: {val!r}')
        try:
            d = Decimal(val)
        except InvalidOperation:
            raise ProjectionError(f'field_id {fid} decimal 无法解析: {val!r}')
        if not d.is_finite():
            raise ProjectionError(f'field_id {fid} decimal 非有限值')
        p, s = node['reader_precision'], node['reader_scale']
        sign, digits, exp = d.as_tuple()
        # 整数位个数：规范化后小数点左侧的有效位数；纯小数为 0。
        int_digits = max(len(digits) + min(exp, 0), 0)
        frac_digits = max(-exp, 0)
        if int_digits > p - s or frac_digits > s:
            # 超界拒绝，绝不截断/舍入。
            raise ProjectionError(
                f'field_id {fid} decimal {val!r} 超出 reader({p},{s})，拒绝截断')
        return val  # 字符串原样保留，往返精确一致

    @staticmethod
    def _mark(node, t_path, s_path, status, index=None, key=None):
        m = {'target_path': render_path(t_path), 'source_path': render_path(s_path),
             'field_id': node['id'], 'name': node['name'], 'status': status}
        if index is not None:
            m['index'] = index
        if key is not None:
            m['key'] = key
        return m


# --------------------------------------------------------------------------- #
# JSONL 读取（重复 map 键即碰撞，拒绝）
# --------------------------------------------------------------------------- #

def _no_duplicate_keys(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise ProjectionError(f'map/struct 键碰撞: {k!r}，拒绝静默覆盖')
        obj[k] = v
    return obj


def parse_jsonl(raw: bytes):
    rows = []
    for i, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line, object_pairs_hook=_no_duplicate_keys,
                            parse_constant=lambda x: (_ for _ in ()).throw(
                                ValueError('非有限常量')))
        except (json.JSONDecodeError, ValueError) as e:
            raise ProjectionError(f'第 {i} 行 JSON 非法: {e}')
        if not isinstance(row, dict) or 'row_id' not in row or 'value' not in row:
            raise ProjectionError(f'第 {i} 行缺少 row_id/value 包装')
        rows.append(row)
    return rows


def load_manifest_files(manifest, base_dir):
    """按 manifest 资产摘要核对并读取全部文件，返回 [(file_ref, rows)]。"""
    bundle = []
    for f in manifest['files']:
        raw = warehouse_io.asset(base_dir, f['asset'])
        rows = parse_jsonl(raw)
        if 'rows' in f['asset'] and len(rows) != f['asset']['rows']:
            raise ProjectionError(
                f"文件 {f['id']} 行数 {len(rows)} 与 manifest 摘要 {f['asset']['rows']} 不符")
        bundle.append((f, rows))
    return bundle


# --------------------------------------------------------------------------- #
# 可选→必选：全量扫描证明（样例非 null 不算证明）
# --------------------------------------------------------------------------- #

def prove_required(plan, bundle, tightened_ids):
    """对绑定文件集的所有行、所有实际路径扫描，证明 tightened 节点全部非 null。

    父容器 null 不凭空制造子实例（不视为违约也不产生证明点）。
    返回证明记录；任何实际 null/缺席即抛 PlanError。
    """
    targets = set(tightened_ids)
    if not targets:
        return {'proven_ids': [], 'files': [f['id'] for f, _ in bundle],
                'rows_scanned': sum(len(r) for _, r in bundle), 'violations': []}
    violations = []
    rows_scanned = 0

    def leaf_check(node, val, path, row_id, loc):
        if node['id'] in targets and val is None:
            violations.append({'row_id': row_id, 'field_id': node['id'],
                               'name': node['name'], 'path': render_path(path),
                               'reason': loc})

    def walk(node, val, path, row_id):
        # 结构 null：整层跳过，不制造子实例。
        if val is None:
            if node['id'] in targets and node['op'] != 'struct':
                violations.append({'row_id': row_id, 'field_id': node['id'],
                                   'name': node['name'], 'path': render_path(path),
                                   'reason': 'explicit_null_container'})
            return
        if node['op'] == 'struct':
            for e in node['entries']:
                p = path + [e['id']]
                key = str(e['id'])
                if key not in val:
                    if e['id'] in targets:
                        violations.append({'row_id': row_id, 'field_id': e['id'],
                                           'name': e['name'], 'path': render_path(p),
                                           'reason': 'physically_absent'})
                    continue
                walk(e, val[key], p, row_id)
        elif node['op'] == 'list':
            for i, item in enumerate(val):
                p = path + [node['element']['id']]
                if item is None:
                    if node['element']['id'] in targets:
                        violations.append({'row_id': row_id,
                                           'field_id': node['element']['id'],
                                           'name': node['element']['name'],
                                           'path': render_path(p) + '[]',
                                           'reason': f'null_element_at_{i}'})
                else:
                    walk(node['element'], item, p, row_id)
        elif node['op'] == 'map':
            for k, v in val.items():
                p = path + [node['value']['id']]
                if v is None:
                    if node['value']['id'] in targets:
                        violations.append({'row_id': row_id,
                                           'field_id': node['value']['id'],
                                           'name': node['value']['name'],
                                           'path': render_path(p) + f'[{k!r}]',
                                           'reason': 'null_map_value'})
                else:
                    walk(node['value'], v, p, row_id)
        else:
            leaf_check(node, val, path, row_id, 'explicit_null')

    for f, rows in bundle:
        for row in rows:
            rows_scanned += 1
            walk(plan, row['value'], [plan['id']], row['row_id'])

    if violations:
        raise PlanError({'message': '可选→必选扫描证明失败：存在实际 null/缺席',
                         'violations': violations})
    return {'proven_ids': sorted(targets),
            'files': [f['id'] for f, _ in bundle],
            'rows_scanned': rows_scanned, 'violations': []}


# --------------------------------------------------------------------------- #
# 提案：先在固定文件集验证，再允许写新 manifest
# --------------------------------------------------------------------------- #

def propose(manifest, base_dir):
    """编译计划并对 manifest 绑定的固定文件集完成全部验证（含投影试跑）。"""
    writer, reader = manifest['writer_schema'], manifest['reader_schema']
    retired = validate(writer, manifest.get('retired_ids', ()))
    validate(reader, retired_ids=retired)
    schema_diff = diff(writer, reader)
    plan, tightened = compile_plan(writer, reader)
    bundle = load_manifest_files(manifest, base_dir)
    proof = prove_required(plan, bundle, tightened)

    projector = Projector(plan)
    projected = []
    for f, rows in bundle:
        for row in rows:
            projected.append({'file_id': f['id'], 'row_id': row['row_id'],
                              **projector.project_row(row['value'])})

    bound = [{'id': f['id'], 'asset': f['asset']} for f, _ in bundle]
    bound_digest = hashlib.sha256(
        json.dumps(bound, sort_keys=True, ensure_ascii=False).encode('utf8')).hexdigest()
    schema_digest = hashlib.sha256(
        json.dumps([writer, reader], sort_keys=True, ensure_ascii=False)
        .encode('utf8')).hexdigest()
    return {
        'plan': plan,
        'diff': schema_diff,
        'required_proof': proof,
        'projected_rows': projected,
        'bound_files': bound,
        'bound_files_digest': bound_digest,
        'schema_digest': schema_digest,
        'schema_version': manifest.get('schema_version'),
        'current_manifest': manifest.get('current_manifest'),
        'retired_ids': list(retired),
    }


# --------------------------------------------------------------------------- #
# 提交：复核当前 schema 与所验证文件集未变，并发新增文件使证明失效
# --------------------------------------------------------------------------- #

def commit(proposal, current_manifest, base_dir, new_manifest_id, new_schema_version):
    """复核后生成新 manifest；任何漂移都抛 CommitError，原 manifest 不改。"""
    writer, reader = (current_manifest['writer_schema'],
                      current_manifest['reader_schema'])
    schema_now = hashlib.sha256(
        json.dumps([writer, reader], sort_keys=True, ensure_ascii=False)
        .encode('utf8')).hexdigest()
    if schema_now != proposal['schema_digest']:
        raise CommitError('当前 schema 与提案验证时不同，拒绝提交')

    files_now = [{'id': f['id'], 'asset': f['asset']} for f in current_manifest['files']]
    now_digest = hashlib.sha256(
        json.dumps(files_now, sort_keys=True, ensure_ascii=False).encode('utf8')).hexdigest()
    if now_digest != proposal['bound_files_digest']:
        raise CommitError('文件集与提案验证时不同（新增/删除/摘要变化），拒绝提交')

    # 并发到达的旧 schema 文件：即使文件集摘要相同，append_after_validation 也使
    # 非 null 证明失效——证明只覆盖提案绑定的那些文件。
    pending = current_manifest.get('append_after_validation') or []
    if pending:
        raise CommitError({
            'message': '验证后有并发新增旧 schema 文件，非 null 证明失效，拒绝提交',
            'unvalidated_files': [f.get('id') for f in pending]})

    # 重新核对资产字节并复跑扫描，确保磁盘内容未被调换。
    bundle = load_manifest_files(current_manifest, base_dir)
    _plan, tightened = compile_plan(writer, reader)
    prove_required(_plan, bundle, tightened)
    projector = Projector(proposal['plan'])
    for f, rows in bundle:
        for row in rows:
            projector.project_row(row['value'])

    new_manifest = {
        'kind': 'schema-evolution',
        'manifest_id': new_manifest_id,
        'schema_version': new_schema_version,
        'writer_schema': writer,       # 旧快照仍按旧 schema 解释
        'reader_schema': reader,       # 新读统一按 reader
        'retired_ids': proposal['retired_ids'],
        'plan': proposal['plan'],
        'required_proof': proposal['required_proof'],
        'files': current_manifest['files'],
        'bound_files_digest': proposal['bound_files_digest'],
        'previous_manifest': current_manifest.get('current_manifest')
                            or current_manifest.get('manifest_id'),
    }
    return new_manifest


def write_manifest(path, manifest):
    """原子写：先写临时文件再替换，失败不动目标路径。"""
    from pathlib import Path
    p = Path(path)
    tmp = p.with_suffix(p.suffix + '.tmp')
    payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf8')
    tmp.write_bytes(payload)
    tmp.replace(p)
    return hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------- #
# 读取：旧快照走旧 schema，新读统一按 reader
# --------------------------------------------------------------------------- #

def read_rows(manifest, base_dir):
    """按 manifest 版本读取全部行。

    无 plan 的旧 manifest：按 writer 原编码直读（旧快照继续使用旧 schema）；
    带 plan 的新 manifest：统一投影到 reader。
    """
    bundle = load_manifest_files(manifest, base_dir)
    if 'plan' not in manifest:
        return [{'file_id': f['id'], 'row_id': r['row_id'],
                 'value': r['value'], 'lineage': [], 'schema': 'writer'}
                for f, rows in bundle for r in rows]
    projector = Projector(manifest['plan'])
    return [{'file_id': f['id'], 'row_id': r['row_id'],
             **projector.project_row(r['value']), 'schema': 'reader'}
            for f, rows in bundle for r in rows]
