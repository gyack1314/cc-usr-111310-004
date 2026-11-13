"""生成 schema 演进演示数据并把字节/sha256/行数摘要写进 manifest。

只使用标准库；数据文件写到 data/evolution/，manifest 与数据同目录，
使现有 io.asset 的相对路径解析直接可用。
"""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = ROOT / 'data' / 'evolution'


def write_asset(name, lines):
    raw = ''.join(line if line.endswith('\n') else line + '\n' for line in lines).encode('utf8')
    (OUT / name).write_bytes(raw)
    return {'path': name, 'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(), 'rows': len(lines)}


def file_entry(fid, asset):
    return {'id': fid, 'schema_id': 'writer', 'asset': asset}


def base_writer():
    return {
        'id': 1, 'name': 'root', 'type': 'struct', 'required': True,
        'children': [
            {'id': 2, 'name': 'checks', 'type': 'list', 'required': False,
             'element': {'id': 3, 'name': 'item', 'type': 'struct', 'required': False,
                         'children': [
                             {'id': 4, 'name': 'reading', 'type': 'decimal',
                              'precision': 5, 'scale': 1, 'required': False},
                             {'id': 5, 'name': 'labels', 'type': 'map', 'required': False,
                              'key': {'id': 6, 'name': 'key', 'type': 'string', 'required': True},
                              'value': {'id': 7, 'name': 'value', 'type': 'list', 'required': False,
                                        'element': {'id': 8, 'name': 'tag', 'type': 'string',
                                                    'required': False}}}]}},
            {'id': 9, 'name': 'station', 'type': 'string', 'required': False},
        ],
    }


def normal_reader():
    w = base_writer()
    w['children'][0]['name'] = 'inspections'          # id=2 重命名
    w['children'][0]['element']['children'][0].update(precision=7, scale=2)  # decimal 提升
    w['children'].append({'id': 10, 'name': 'status', 'type': 'string',
                          'required': False, 'default': 'new'})
    return w


def manifest(name, writer, reader, files, retired=(11,), append=None):
    return {
        'kind': 'schema-evolution',
        'manifest_id': name,
        'writer_schema': writer,
        'reader_schema': reader,
        'retired_ids': list(retired),
        'files': files,
        'schema_version': 'sv1',
        'current_manifest': name,
        'append_after_validation': append or [],
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)

    # 1) 同名不同 ID：reader 删除 id=9(station)，新增 id=12 也叫 station。
    same_name_asset = write_asset('same-name.jsonl', [
        '{"row_id":"s1","value":{"9":"line-a"}}',
        '{"row_id":"s2","value":{}}',
    ])
    writer_sn = base_writer()
    reader_sn = base_writer()
    reader_sn['children'] = [c for c in reader_sn['children'] if c['id'] != 9]
    reader_sn['children'].append({'id': 12, 'name': 'station', 'type': 'string',
                                  'required': False})
    (OUT / 'same-name-different-id.json').write_text(json.dumps(
        manifest('mf-sn', writer_sn, reader_sn, [file_entry('f1', same_name_asset)]),
        ensure_ascii=False, indent=2), encoding='utf8')

    # 2) decimal 临界：writer(5,1) 最大 9999.9，reader(7,2) 整数位容量 5。
    #    第一份全部可提升（含 reader 临界 99999.99）；第二份含 100000.0
    #    （整数位 6 > 5）必须拒绝截断，另有 scale 超限的 0.001。
    ok_asset = write_asset('decimal-boundary-ok.jsonl', [
        '{"row_id":"d1","value":{"2":[{"4":"9999.9"},{"4":"0.1"},{"4":"-9999.9"}]}}',
        '{"row_id":"d2","value":{"2":[{"4":"12.30"},{"4":"99999.99"}]}}',
    ])
    bad_asset = write_asset('decimal-boundary-overflow.jsonl', [
        '{"row_id":"d3","value":{"2":[{"4":"100000.0"}]}}',
        '{"row_id":"d4","value":{"2":[{"4":"99.999"}]}}',
    ])
    w_dec, r_dec = base_writer(), normal_reader()
    (OUT / 'decimal-boundary-ok.json').write_text(json.dumps(
        manifest('mf-dec-ok', w_dec, r_dec, [file_entry('f1', ok_asset)]),
        ensure_ascii=False, indent=2), encoding='utf8')
    w_dec2, r_dec2 = base_writer(), normal_reader()
    (OUT / 'decimal-boundary-overflow.json').write_text(json.dumps(
        manifest('mf-dec-bad', w_dec2, r_dec2, [file_entry('f1', bad_asset)]),
        ensure_ascii=False, indent=2), encoding='utf8')

    # 3) 可选→必选：station 提升 required，绑定文件全部非 null → 证明成功；
    #    追加文件带 null station → 验证后到达，提交拒绝。
    proof_asset = write_asset('required-proof-ok.jsonl', [
        '{"row_id":"p1","value":{"9":"station-1","2":[]}}',
        '{"row_id":"p2","value":{"9":"station-2","2":[{"4":"1.2"}]}}',
    ])
    append_asset = write_asset('required-proof-append.jsonl', [
        '{"row_id":"p3","value":{"2":[]}}',
    ])
    w_req, r_req = base_writer(), normal_reader()
    r_req['children'][1]['required'] = True  # id=9 station 可选→必选
    (OUT / 'required-proof-ok.json').write_text(json.dumps(
        manifest('mf-req-ok', w_req, r_req, [file_entry('f1', proof_asset)]),
        ensure_ascii=False, indent=2), encoding='utf8')
    appended = manifest('mf-req-pending', w_req, r_req,
                        [file_entry('f1', proof_asset)],
                        append=[file_entry('f2', append_asset)])
    (OUT / 'required-proof-append.json').write_text(json.dumps(
        appended, ensure_ascii=False, indent=2), encoding='utf8')

    # 4) map 键碰撞：同一 map 内重复 string 键，解析期即拒绝。
    collision_asset = write_asset('map-key-collision.jsonl', [
        '{"row_id":"m1","value":{"2":[{"5":{"dup":[null],"dup":["x"]}}]}}',
    ])
    w_map, r_map = base_writer(), normal_reader()
    (OUT / 'map-key-collision.json').write_text(json.dumps(
        manifest('mf-map', w_map, r_map, [file_entry('f1', collision_asset)]),
        ensure_ascii=False, indent=2), encoding='utf8')

    print('wrote', len(list(OUT.glob('*'))), 'files to', OUT)


if __name__ == '__main__':
    main()
