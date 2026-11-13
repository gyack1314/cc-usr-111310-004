"""湖仓资料命令行：原始摘要与 schema 演进工作流。

inspect   只读原始资料（不生成新版本）
diff      输出独立 schema 差异（不触碰数据文件）
propose   编译投影计划并在 manifest 绑定的固定文件集上验证
commit    复核 schema/文件集未变后写新 manifest（失败不改当前版本）
read      旧快照按 writer 直读，新 manifest 统一按 reader 投影
"""
import argparse
import json
from pathlib import Path

from .io import load
from .domain import summarize
from .schema import SchemaError, diff as schema_diff, validate
from . import evolution


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _load(path):
    v, digest = load(path)
    return v, Path(path).parent, digest


def cmd_inspect(a):
    v, base, digest = _load(a.file)
    _print({'manifest_sha256': digest, 'summary': summarize(v, base)})


def cmd_diff(a):
    v, _, _ = _load(a.file)
    retired = validate(v['writer_schema'], v.get('retired_ids', ()))
    validate(v['reader_schema'], retired_ids=retired)
    _print(schema_diff(v['writer_schema'], v['reader_schema']))


def cmd_propose(a):
    v, base, _ = _load(a.file)
    proposal = evolution.propose(v, base)
    if a.out:
        evolution.write_manifest(a.out, proposal)
    view = {k: proposal[k] for k in
            ('diff', 'required_proof', 'bound_files', 'bound_files_digest',
             'schema_digest', 'schema_version')}
    view['plan'] = proposal['plan']
    view['projected_rows'] = proposal['projected_rows']
    _print(view)


def cmd_commit(a):
    v, base, _ = _load(a.file)
    data_base = Path(a.data_dir) if a.data_dir else base
    proposal = evolution.propose(v, base)
    try:
        new_manifest = evolution.commit(proposal, v, data_base,
                                        a.new_manifest_id, a.new_schema_version)
    except evolution.CommitError as e:
        _print({'committed': False, 'rejected': e.args[0]})
        raise SystemExit(2)
    digest = evolution.write_manifest(a.out, new_manifest)
    _print({'committed': True, 'out': a.out, 'manifest_sha256': digest,
            'manifest_id': a.new_manifest_id,
            'schema_version': a.new_schema_version})


def cmd_read(a):
    v, base, _ = _load(a.file)
    data_base = Path(a.data_dir) if a.data_dir else base
    _print(evolution.read_rows(v, data_base))


def main():
    p = argparse.ArgumentParser(prog='warehouse_data.cli')
    sub = p.add_subparsers(dest='command', required=True)

    s = sub.add_parser('inspect'); s.add_argument('file'); s.set_defaults(fn=cmd_inspect)
    s = sub.add_parser('diff'); s.add_argument('file'); s.set_defaults(fn=cmd_diff)
    s = sub.add_parser('propose'); s.add_argument('file'); s.add_argument('--out'); s.set_defaults(fn=cmd_propose)
    s = sub.add_parser('commit')
    s.add_argument('file'); s.add_argument('out')
    s.add_argument('--data-dir', help='数据文件根目录（默认取 manifest 所在目录）')
    s.add_argument('--new-manifest-id', default='mf2')
    s.add_argument('--new-schema-version', default='sv2')
    s.set_defaults(fn=cmd_commit)
    s = sub.add_parser('read'); s.add_argument('file')
    s.add_argument('--data-dir', help='数据文件根目录（默认取 manifest 所在目录）')
    s.set_defaults(fn=cmd_read)

    a = p.parse_args()
    try:
        a.fn(a)
    except (SchemaError, evolution.PlanError, evolution.ProjectionError, ValueError) as e:
        _print({'error': type(e).__name__, 'detail': e.args[0]})
        raise SystemExit(1)


if __name__ == '__main__':
    main()
