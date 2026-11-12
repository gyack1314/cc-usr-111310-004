"""核对原始配置及数量，不执行仓库变换。"""
from .io import asset
def summarize(v,root):
    if v['writer_schema']['type']!='struct' or v['reader_schema']['type']!='struct':raise ValueError('顶层非struct')
    for f in v['files']:asset(root,f['asset'])
    return {'files':len(v['files']),'retired_ids':len(v['retired_ids']),'reader_top_fields':len(v['reader_schema']['children'])}
