"""schema 演进层测试：ID 身份、投影语义、拒绝条件、提交复核与往返一致。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from warehouse_data.io import load
from warehouse_data.schema import validate, diff, SchemaError
from warehouse_data import evolution
from warehouse_data.evolution import (
    compile_plan, Projector, propose, commit, CommitError, PlanError,
    ProjectionError, parse_jsonl, read_rows,
)

DATA = Path(__file__).resolve().parent.parent / 'data'
EV = DATA / 'evolution'


def load_manifest(name):
    v, _ = load(EV / name)
    return v


class SchemaValidationTests(unittest.TestCase):
    def setUp(self):
        self.m = load_manifest('decimal-boundary-ok.json')

    def test_duplicate_field_id_rejected(self):
        w = copy.deepcopy(self.m['writer_schema'])
        w['children'][1]['id'] = 2  # 与 checks 重复
        with self.assertRaises(SchemaError):
            validate(w)

    def test_retired_id_reuse_rejected(self):
        r = copy.deepcopy(self.m['reader_schema'])
        r['children'].append({'id': 11, 'name': 'ghost', 'type': 'string',
                              'required': False})
        with self.assertRaises(SchemaError):
            validate(r, retired_ids={11})

    def test_unknown_type_and_bad_decimal_rejected(self):
        w = copy.deepcopy(self.m['writer_schema'])
        w['children'][1]['type'] = 'float64'
        with self.assertRaises(SchemaError):
            validate(w)
        d = copy.deepcopy(self.m['writer_schema'])
        leaf = d['children'][0]['element']['children'][0]
        leaf['scale'] = 9
        with self.assertRaises(SchemaError):
            validate(d)

    def test_map_key_must_be_required_string(self):
        w = copy.deepcopy(self.m['writer_schema'])
        w['children'][0]['element']['children'][1]['key']['type'] = 'int64'
        with self.assertRaises(SchemaError):
            validate(w)

    def test_default_cannot_be_null_or_typed_wrong(self):
        r = copy.deepcopy(self.m['reader_schema'])
        r['children'].append({'id': 13, 'name': 'x', 'type': 'string',
                              'required': False, 'default': None})
        with self.assertRaises(SchemaError):
            validate(r)
        r2 = copy.deepcopy(self.m['reader_schema'])
        r2['children'].append({'id': 13, 'name': 'x', 'type': 'int32',
                               'required': False, 'default': '1'})
        with self.assertRaises(SchemaError):
            validate(r2)


class DiffIdentityTests(unittest.TestCase):
    def test_rename_is_identity_preserving(self):
        m = load_manifest('decimal-boundary-ok.json')
        d = diff(m['writer_schema'], m['reader_schema'])
        names = {c['id']: c['kind'] for c in d['changes']}
        self.assertEqual(names[2], 'renamed')
        self.assertIn(2, d['matched_ids'])

    def test_same_name_different_id_is_drop_plus_add(self):
        m = load_manifest('same-name-different-id.json')
        d = diff(m['writer_schema'], m['reader_schema'])
        kinds = {(c['kind'], c['id']): c for c in d['changes']}
        self.assertIn(('dropped', 9), kinds)
        self.assertIn(('added', 12), kinds)
        self.assertTrue(kinds[('added', 12)]['name_reused'])
        self.assertNotIn(12, d['matched_ids'])

    def test_cross_parent_move_rejected(self):
        m = load_manifest('decimal-boundary-ok.json')
        r = copy.deepcopy(m['reader_schema'])
        # 把嵌套叶子 id=4（reading）从 item(id=3) 下移到 root：
        # 同 ID 但父从 3 变成 1 → 跨父移动，形状合法、身份不兼容。
        item = r['children'][0]['element']
        moved = item['children'].pop(0)
        self.assertEqual(moved['id'], 4)
        r['children'].append(moved)
        validate(r, retired_ids={11})  # 形状本身合法
        with self.assertRaises(PlanError):
            compile_plan(m['writer_schema'], r)


class AllowedEvolutionTests(unittest.TestCase):
    def test_int32_to_int64_allowed_other_primitive_change_rejected(self):
        m = load_manifest('decimal-boundary-ok.json')
        w, r = m['writer_schema'], copy.deepcopy(m['reader_schema'])
        w['children'][1]['type'] = 'int32'
        r['children'][1]['type'] = 'int64'
        plan, _ = compile_plan(w, r)
        leaf = plan['entries'][1]
        self.assertEqual(leaf['transform'], 'int32_to_int64')
        # bool→int64 不兼容
        w2, r2 = copy.deepcopy(w), copy.deepcopy(r)
        w2['children'][1]['type'] = 'bool'
        with self.assertRaises(PlanError):
            compile_plan(w2, r2)

    def test_decimal_scale_lower_and_capacity_shrink_rejected(self):
        m = load_manifest('decimal-boundary-ok.json')
        r = copy.deepcopy(m['reader_schema'])
        leaf = r['children'][0]['element']['children'][0]
        leaf['precision'], leaf['scale'] = 5, 0  # scale 降低
        with self.assertRaises(PlanError):
            compile_plan(m['writer_schema'], r)
        leaf['precision'], leaf['scale'] = 6, 2  # 整数位 4 < 原 4? 等于，合法
        compile_plan(m['writer_schema'], r)
        leaf['precision'], leaf['scale'] = 5, 2  # 整数位 3 < 4
        with self.assertRaises(PlanError):
            compile_plan(m['writer_schema'], r)

    def test_added_required_without_default_rejected(self):
        m = load_manifest('decimal-boundary-ok.json')
        r = copy.deepcopy(m['reader_schema'])
        r['children'].append({'id': 14, 'name': 'mandatory', 'type': 'string',
                              'required': True})
        with self.assertRaises(PlanError):
            compile_plan(m['writer_schema'], r)

    def test_dropped_field_listed_as_information_loss(self):
        m = load_manifest('same-name-different-id.json')
        plan, _ = compile_plan(m['writer_schema'], m['reader_schema'])
        dropped = plan['dropped']
        self.assertEqual([d['id'] for d in dropped], [9])
        self.assertIn('loss', dropped[0])


class ProjectionSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.m = load_manifest('decimal-boundary-ok.json')
        self.plan, _ = compile_plan(self.m['writer_schema'], self.m['reader_schema'])
        pj = Projector(self.plan)
        self.row = {'2': None, '9': None}
        self.out = pj.project_row(self.row)

    def test_null_list_empty_list_null_element_and_empty_struct_distinct(self):
        pj = Projector(self.plan)
        cases = {
            'null_list': {'2': None},
            'empty_list': {'2': []},
            'null_element': {'2': [None]},
            'empty_struct': {'2': [{}]},
        }
        rendered = {k: pj.project_row(v)['value']['2'] for k, v in cases.items()}
        self.assertIsNone(rendered['null_list'])
        self.assertEqual(rendered['empty_list'], [])
        self.assertEqual(rendered['null_element'], [None])
        # 空 struct 仍 struct 化；其物理缺席的可选子字段按契约读为 null。
        self.assertEqual(rendered['empty_struct'], [{'4': None, '5': None}])
        self.assertEqual(len({json.dumps(x, sort_keys=True) for x in rendered.values()}), 4)

    def test_explicit_null_not_replaced_by_default(self):
        pj = Projector(self.plan)
        out = pj.project_row({'9': None})['value']
        self.assertIsNone(out['9'])           # 显式 null 保持 null
        self.assertEqual(out['10'], 'new')    # 物理缺席才用默认
        statuses = {x['field_id']: x['status'] for x in pj.project_row({'9': None})['lineage']}
        self.assertEqual(statuses[9], 'explicit_null')
        self.assertEqual(statuses[10], 'added_default')

    def test_absent_optional_reads_null_without_default(self):
        pj = Projector(self.plan)
        res = pj.project_row({})
        self.assertIsNone(res['value']['9'])
        self.assertTrue(any(x['status'] == 'absent_as_null' and x['field_id'] == 9
                            for x in res['lineage']))

    def test_null_struct_does_not_materialize_child_defaults(self):
        # r1: checks 是 null list；其内部 struct 不生成任何子字段。
        self.assertIsNone(self.out['value']['2'])
        self.assertTrue(any(x['status'] == 'null_list' for x in self.out['lineage']))

    def test_map_null_value_keeps_key(self):
        pj = Projector(self.plan)
        res = pj.project_row({'2': [{'5': {'k1': None}}]})
        self.assertEqual(res['value']['2'][0]['5'], {'k1': None})
        self.assertTrue(any(x['status'] == 'null_map_value' for x in res['lineage']))

    def test_lineage_carries_full_id_paths_two_levels_deep(self):
        pj = Projector(self.plan)
        res = pj.project_row({'2': [{'5': {'k': [None, 'x']}}]})
        paths = {x['target_path'] for x in res['lineage']}
        self.assertIn('root.2.3.5.7.8[]', paths)

    def test_projection_rebuilds_containers_not_json_deepcopy(self):
        pj = Projector(self.plan)
        src = {'2': [{'4': '1.2'}]}
        out = pj.project_row(src)['value']
        out['2'][0]['4'] = 'MUTATED'
        self.assertEqual(src['2'][0]['4'], '1.2')  # 源容器未被共享/改写

    def test_bool_not_coerced_to_int(self):
        m = load_manifest('decimal-boundary-ok.json')
        w, r = copy.deepcopy(m['writer_schema']), copy.deepcopy(m['reader_schema'])
        w['children'][1].update(type='bool')
        r['children'][1].update(type='bool')
        plan, _ = compile_plan(w, r)
        with self.assertRaises(ProjectionError):
            Projector(plan).project_row({'9': 1})

    def test_decimal_overflow_rejected_not_truncated(self):
        m = load_manifest('decimal-boundary-overflow.json')
        with self.assertRaises(ProjectionError):
            propose(m, EV)

    def test_decimal_string_roundtrip_exact(self):
        m = load_manifest('decimal-boundary-ok.json')
        proposal = propose(m, EV)
        projected = {r['row_id']: r['value'] for r in proposal['projected_rows']}
        originals = {r['row_id']: r['value'] for r in
                     [row for _, rows in evolution.load_manifest_files(m, EV) for row in rows]}
        for rid, val in originals.items():
            for i, item in enumerate(val['2']):
                if '4' in item:
                    self.assertEqual(projected[rid]['2'][i]['4'], item['4'])


class RequiredProofTests(unittest.TestCase):
    def test_proof_succeeds_when_all_non_null(self):
        m = load_manifest('required-proof-ok.json')
        proposal = propose(m, EV)
        self.assertEqual(proposal['required_proof']['proven_ids'], [9])
        self.assertEqual(proposal['required_proof']['rows_scanned'], 2)

    def test_proof_fails_on_any_null_or_absent(self):
        m, _ = load(Path(DATA) / 'required-proof-fails.json')
        with self.assertRaises(PlanError) as ctx:
            propose(m, Path(DATA))
        violations = ctx.exception.args[0]['violations']
        self.assertTrue(any(v['field_id'] == 9 for v in violations))

    def test_sample_non_null_is_not_a_proof(self):
        # 构造：第一行非 null、第二行 null；只看样例会误判兼容。
        m = load_manifest('required-proof-ok.json')
        bundle = evolution.load_manifest_files(m, EV)
        plan, tightened = compile_plan(m['writer_schema'], m['reader_schema'])
        self.assertEqual(tightened, [9])
        # 手工截成单文件单行情景后再补回 null 行，证明必须覆盖全部行。
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'rows.jsonl'
            p.write_text('{"row_id":"x","value":{"9":null}}\n', encoding='utf8')
            rows = parse_jsonl(p.read_bytes())
            with self.assertRaises(PlanError):
                evolution.prove_required(plan, [(bundle[0][0], rows)], tightened)


class CommitAndSnapshotTests(unittest.TestCase):
    def test_commit_writes_new_manifest_and_keeps_old_snapshot(self):
        m = load_manifest('required-proof-ok.json')
        proposal = propose(m, EV)
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'mf2.json'
            new = commit(proposal, m, EV, 'mf2', 'sv2')
            digest = evolution.write_manifest(out, new)
            self.assertEqual(len(digest), 64)
            old_rows = read_rows(m, EV)
            new_rows = read_rows(json.loads(out.read_text(encoding='utf8')), EV)
            self.assertEqual({r['schema'] for r in old_rows}, {'writer'})
            self.assertEqual({r['schema'] for r in new_rows}, {'reader'})

    def test_concurrent_append_after_validation_invalidates_proof(self):
        m = load_manifest('required-proof-append.json')
        proposal = propose(load_manifest('required-proof-ok.json'), EV)
        with self.assertRaises(CommitError):
            commit(proposal, m, EV, 'mf2', 'sv2')

    def test_schema_drift_between_proposal_and_commit_rejected(self):
        m = load_manifest('required-proof-ok.json')
        proposal = propose(m, EV)
        drifted = copy.deepcopy(m)
        drifted['reader_schema']['children'][1]['name'] = 'plant'
        with self.assertRaises(CommitError):
            commit(proposal, drifted, EV, 'mf2', 'sv2')

    def test_file_set_drift_rejected(self):
        m = load_manifest('required-proof-ok.json')
        proposal = propose(m, EV)
        drifted = copy.deepcopy(m)
        drifted['files'][0]['asset']['sha256'] = '0' * 64
        with self.assertRaises((CommitError, ValueError)):
            commit(proposal, drifted, EV, 'mf2', 'sv2')

    def test_failed_commit_does_not_modify_current_version(self):
        m = load_manifest('required-proof-append.json')
        before = json.dumps(m, sort_keys=True)
        proposal = propose(load_manifest('required-proof-ok.json'), EV)
        with self.assertRaises(CommitError):
            commit(proposal, m, EV, 'mf2', 'sv2')
        self.assertEqual(json.dumps(m, sort_keys=True), before)


class EndToEndNormalManifestTests(unittest.TestCase):
    def test_normal_manifest_full_pipeline(self):
        m, _ = load(DATA / 'normal.json')
        proposal = propose(m, DATA)
        # 往返：所有保留字段在投影后精确一致。
        pj = Projector(proposal['plan'])
        for f, rows in evolution.load_manifest_files(m, DATA):
            for row in rows:
                projected = pj.project_row(row['value'])['value']
                for key, val in row['value'].items():
                    self.assertEqual(projected[key], val)
        # 新增默认字段存在。
        self.assertTrue(all(r['value'].get('10') == 'new'
                            for r in read_rows({**m, 'plan': proposal['plan']}, DATA)))


if __name__ == '__main__':
    unittest.main()
