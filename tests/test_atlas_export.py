"""Small schema fixtures verify namespace and stale-claim attribution."""
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import importlib.util

# Load the packaged exporter BY PATH, exactly as the CLI and the MCP tool run
# it (as a script in a child process), so this test never imports the
# `yantrikdb` package and its native engine.
_SCRIPT = Path(__file__).resolve().parents[1] / 'src' / 'yantrikdb' / 'atlas' / 'export_atlas.py'
_spec = importlib.util.spec_from_file_location('atlas_export_under_test', _SCRIPT)
_mod = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_mod)
export = _mod.export

class ExportTests(unittest.TestCase):
    def fixture(self, base):
        stores=base/'stores'; stores.mkdir()
        c=sqlite3.connect(stores/'sample.db')
        c.execute('CREATE TABLE memories (rid TEXT,namespace TEXT,type TEXT,text TEXT,importance REAL,created_at REAL,metadata TEXT,consolidation_status TEXT)')
        c.executemany('INSERT INTO memories VALUES (?,?,?,?,?,?,?,?)',[
            ('one','work','semantic','Shared person',.7,10,'{}','active'),
            ('two','personal','episodic','Shared person at dinner',.4,12,'{}','active'),
            ('gone','work','semantic','Deleted',.1,1,'{}','tombstoned')])
        c.execute('CREATE TABLE memory_entities (memory_rid TEXT,entity_name TEXT)')
        c.executemany('INSERT INTO memory_entities VALUES (?,?)',[('one','Person'),('two','Person'),('gone','Person')])
        return c,stores

    def test_older_schema_and_scopes(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp); c,stores=self.fixture(base); c.commit(); c.close()
            with contextlib.redirect_stdout(io.StringIO()): export(stores,base/'site')
            d=json.loads((base/'site/data.json').read_bytes())
            self.assertEqual(len(d['memories']),2)
            self.assertEqual({g['name'] for g in d['groups']},{'sample / work','sample / personal'})
            self.assertEqual(len(d['memberships']),2)
            self.assertEqual(d['revisions'],[])
            self.assertEqual(d['tasks'],[])
            self.assertIsNone(d['memories'][0]['domain'])
            self.assertNotIn('path',d['sources'][0])

    def test_revision_flag_is_chronological_not_invented_validity(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp); c,stores=self.fixture(base)
            c.execute('CREATE TABLE record_revisions (rid TEXT,revision_num INTEGER,prior_text TEXT,reason TEXT,applied_at REAL)')
            c.execute("INSERT INTO record_revisions VALUES ('one',1,'Old','Correction',20)")
            c.execute('CREATE TABLE claims (src TEXT,dst TEXT,rel_type TEXT,polarity INTEGER,namespace TEXT,source_memory_rid TEXT,created_at REAL,valid_from REAL,valid_to REAL,extractor TEXT,tombstoned INTEGER)')
            for created in (19,20,21,None):
                c.execute('INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?)',('P','Team','leads',1,'work','one',created,10,None,'agent_stated',0))
            c.execute('CREATE TABLE tasks (id TEXT,namespace TEXT,title TEXT,status TEXT,priority TEXT)')
            c.execute("INSERT INTO tasks VALUES ('task','learning','Read paper','open','low')")
            c.commit();c.close()
            with contextlib.redirect_stdout(io.StringIO()): export(stores,base/'site')
            d=json.loads((base/'site/data.json').read_bytes())
            self.assertEqual([r['source_revised_after_claim'] for r in d['claims']],[True,False,False,None])
            self.assertTrue(all(r['valid_to'] is None for r in d['claims']))
            self.assertEqual(d['revisions'][0]['memory_id'],0)
            self.assertEqual(d['groups'][d['tasks'][0]['g']]['namespace'],'learning')

    def test_single_file_exports_only_the_named_store(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp); c,stores=self.fixture(base); c.commit(); c.close()
            other=sqlite3.connect(stores/'sibling.db')
            other.execute('CREATE TABLE memories (rid TEXT,namespace TEXT,type TEXT,text TEXT,importance REAL,created_at REAL,metadata TEXT,consolidation_status TEXT)')
            other.execute("INSERT INTO memories VALUES ('s1','work','semantic','Sibling store memory',.5,5,'{}','active')")
            other.commit(); other.close()
            with contextlib.redirect_stdout(io.StringIO()): export(stores/'sample.db',base/'site')
            d=json.loads((base/'site/data.json').read_bytes())
            self.assertEqual([s['store'] for s in d['sources']],['sample.db'])
            self.assertEqual(len(d['memories']),2)
            self.assertTrue(all(g['name'].startswith('sample /') for g in d['groups']))
            with self.assertRaises(ValueError): export(stores/'notes.txt',base/'site2')

    def test_encrypted_store_is_refused_not_exported_as_ciphertext(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp); c,stores=self.fixture(base)
            c.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)')
            c.execute("INSERT INTO meta VALUES ('encryption_enabled','1')")
            c.commit(); c.close()
            with self.assertRaises(ValueError) as ctx:
                with contextlib.redirect_stdout(io.StringIO()): export(stores,base/'site')
            self.assertIn('encrypted', str(ctx.exception))
            self.assertFalse((base/'site/data.json').exists())

if __name__=='__main__': unittest.main()
