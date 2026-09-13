"""Export an existing YantrikDB store collection without loading the engine.

Usage: python export_atlas.py --stores DIRECTORY --out DIRECTORY
Reads SQLite with mode=ro + query_only, never loads models or embeddings.
"""
import argparse
import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def export(stores, out, label=None):
    paths = sorted(stores.glob('*.db'), key=lambda p: (len(p.stem), p.stem))
    if not paths:
        raise ValueError('No .db files found in the supplied directory')
    data = {'format_version': 2, 'groups': [], 'memories': [], 'claims': [], 'memberships': [],
            'revisions': [], 'tasks': [],
            'sources': [], 'exported_at': datetime.now(timezone.utc).isoformat(),
            'relationship_policy': 'Shared-entity links are derived within a database only. Separate databases do not establish cross-store identity.'}
    if label:
        data['label'] = label
    for path in paths:
        before = digest(path)
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        local = {}
        group_ids = {}
        def columns(table):
            return {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
        def select_fields(table, fields):
            available = columns(table)
            return ','.join(field if field in available else f'NULL AS {field}' for field in fields)
        def group(ns):
            if ns not in group_ids:
                group_ids[ns] = len(data['groups'])
                data['groups'].append({'name': f'{path.stem} / {ns}', 'namespace': ns, 'store': path.name, 'count': 0})
            return group_ids[ns]
        try:
            fields = select_fields('memories', ['rid','namespace','type','text','importance','created_at','metadata','domain','consolidation_status','prior_rid','resolution_kind'])
            for row in conn.execute(f"SELECT {fields} FROM memories WHERE COALESCE(consolidation_status,'active') != 'tombstoned' ORDER BY rid"):
                r = dict(row)
                ns = r['namespace']
                r['g'] = group(ns)
                r['id'] = len(data['memories'])
                r['title'] = ' '.join(r['text'].split())[:85]
                try:
                    r['metadata'] = json.loads(r['metadata'] or '{}')
                except (ValueError, TypeError):
                    pass
                local[r['rid']] = r['id']
                data['groups'][r['g']]['count'] += 1
                data['memories'].append(r)
            if 'memory_entities' in tables:
                for rid, entity in conn.execute('SELECT memory_rid,entity_name FROM memory_entities ORDER BY memory_rid,entity_name'):
                    if rid in local:
                        data['memberships'].append([local[rid], path.name, entity])
            latest_revision = {}
            if 'record_revisions' in tables:
                fields = select_fields('record_revisions', ['rid','revision_num','prior_text','reason','applied_at'])
                for row in conn.execute(f'SELECT {fields} FROM record_revisions ORDER BY rid,revision_num'):
                    revision = dict(row)
                    rid = revision.pop('rid')
                    if rid not in local:
                        continue
                    revision['memory_id'] = local[rid]
                    data['revisions'].append(revision)
                    if revision['applied_at'] is not None:
                        latest_revision[rid] = max(latest_revision.get(rid, float('-inf')), revision['applied_at'])
            if 'claims' in tables:
                fields = select_fields('claims', ['src','dst','rel_type','polarity','namespace','source_memory_rid','created_at','valid_from','valid_to','extractor'])
                for row in conn.execute(f'SELECT {fields} FROM claims WHERE tombstoned=0'):
                    c = dict(row)
                    rid = c.pop('source_memory_rid')
                    c['memory_id'] = local.get(rid)
                    c['source_revised_after_claim'] = (None if c['created_at'] is None else latest_revision.get(rid, float('-inf')) > c['created_at'])
                    c['store'] = path.name
                    data['claims'].append(c)
            if 'tasks' in tables:
                fields = select_fields('tasks', ['id','namespace','title','status','priority','created_at','updated_at'])
                for row in conn.execute(f'SELECT {fields} FROM tasks ORDER BY namespace,id'):
                    task = dict(row)
                    task['store'] = path.name
                    task['g'] = group(task['namespace'])
                    data['tasks'].append(task)
        finally:
            conn.rollback()
            conn.close()
        after = digest(path)
        if before != after:
            raise RuntimeError(f'Source changed during export: {path}. Retry after the writer settles.')
        data['sources'].append({'store': path.name, 'sha256': before, 'unchanged_after_read': True})
    out.mkdir(parents=True, exist_ok=True)
    (out / 'data.json').write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    (out / 'index.html').write_bytes(Path(__file__).with_name('index.html').read_bytes())
    summary = {k: len(data[k]) for k in ('groups','memories','claims','memberships','sources','revisions','tasks')}
    summary['exported_at'] = data['exported_at']
    summary['source_db_hashes_unchanged'] = True
    summary['model_calls'] = 0
    (out / 'export-report.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stores', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--label', help='Optional visible provenance label, such as Fictional sample')
    args = parser.parse_args()
    export(args.stores, args.out, args.label)
