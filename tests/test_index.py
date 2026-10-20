import argparse
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import chat_index as app


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.args = argparse.Namespace(claude_home=root/'claude', codex_home=root/'codex', opencode_home=root/'opencode')
        self.db = app.connect(root/'index.db')
        self.addCleanup(self.db.close)

    def write(self, path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(r)+'\n' for r in records))

    def test_jsonl_incremental_search_delete(self):
        p = self.args.claude_home/'projects/project/abc.jsonl'
        rows = [{'type':'user','sessionId':'abc','cwd':'/tmp', 'message':{'content':'Fix the broken database migration'}},
                {'type':'assistant','message':{'content':[{'type':'text','text':'Use transaction rollback'}]}}]
        self.write(p, rows)
        self.assertEqual(app.refresh(self.db, self.args)[0], 1)
        self.assertEqual(app.refresh(self.db, self.args)[0], 0)
        for query in ['migrat', 'database rollback', '"transaction rollback"']:
            self.assertEqual(len(app.search(self.db, query)), 1)
        self.assertEqual(len(app.search(self.db, 'nonexistent')), 0)
        self.assertEqual(len(app.search(self.db, 'rollback', 'codex')), 0)
        self.write(p, rows[:1])
        app.refresh(self.db, self.args)
        self.assertEqual(len(app.search(self.db, 'rollback')), 0)
        p.unlink()
        app.refresh(self.db, self.args)
        self.assertEqual(len(app.search(self.db)), 0)

    def test_codex_excludes_system_and_duplicate_events(self):
        p = self.args.codex_home/'sessions/a.jsonl'
        self.write(p, [{'type':'session_meta','payload':{'id':'xyz','cwd':'/tmp'}},
                       {'type':'event_msg','payload':{'type':'user_message','message':'duplicate'}},
                       {'type':'response_item','payload':{'type':'message','role':'developer','content':[{'type':'input_text','text':'secret instructions'}]}},
                       {'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'actual question'}]}}])
        with p.open('a') as f:
            f.write('{unfinished')
        app.refresh(self.db, self.args)
        self.assertEqual(app.search(self.db, 'actual')[0]['sid'], 'xyz')
        self.assertFalse(app.search(self.db, 'secret'))
        self.assertFalse(app.search(self.db, 'duplicate'))

    def test_opencode_part_update(self):
        self.args.opencode_home.mkdir()
        with sqlite3.connect(self.args.opencode_home/'opencode.db') as src:
            src.executescript('CREATE TABLE session(id,title,directory,time_updated); CREATE TABLE part(id,session_id,time_created,time_updated,data);')
            src.execute('INSERT INTO session VALUES (?,?,?,?)', ('ses_1','Title','/tmp',1000))
            src.execute('INSERT INTO part VALUES (?,?,?,?,?)', ('p','ses_1',1000,1000,json.dumps({'type':'text','text':'oldword'})))
        app.refresh(self.db, self.args)
        self.assertTrue(app.search(self.db, 'oldword'))
        with sqlite3.connect(self.args.opencode_home/'opencode.db') as src:
            src.execute('UPDATE part SET time_updated=2000,data=?', (json.dumps({'type':'text','text':'newword'}),))
        app.refresh(self.db, self.args)
        self.assertTrue(app.search(self.db, 'newword'))
        self.assertFalse(app.search(self.db, 'oldword'))

    def test_resume_argv_and_directory(self):
        for provider, expected in [('claude',['claude','--resume','abc']), ('codex',['codex','resume','abc']), ('opencode',['opencode','--session','abc'])]:
            row = {'provider':provider, 'sid':'abc', 'cwd':'/tmp'}
            with patch('chat_index.shutil.which', return_value='/bin/client'), patch('chat_index.os.chdir') as cd, patch('chat_index.os.execvp') as execute:
                app.resume(row)
                cd.assert_called_once_with('/tmp')
                execute.assert_called_once_with(provider, expected)

    def test_recent_across_tools_and_empty_search(self):
        for provider, modified in [('claude', 20), ('codex', 30), ('opencode', 10)]:
            app.put(self.db, provider, 'same-id', provider, '/tmp', modified,
                    'conversation', provider, '1')
        self.assertEqual([r['provider'] for r in app.recent_chats(self.db)],
                         ['codex', 'claude', 'opencode'])
        self.assertEqual(len(app.recent_chats(self.db, limit=2)), 2)
        self.assertEqual(app.search(self.db), [])
        self.assertEqual(app.search(self.db, '   '), [])
        app.put(self.db, 'opencode', 'same-id', 'updated', '/tmp', 40,
                'new message', 'opencode', '2')
        self.assertEqual(app.recent_chats(self.db)[0]['provider'], 'opencode')
        self.assertEqual(len(app.recent_chats(self.db)), 3)

    def test_literal_punctuation_and_raw_boolean(self):
        app.put(self.db,'claude','a','Café deploy','/tmp',1,'running migrations with sqlite','source','1')
        self.assertTrue(app.search(self.db, 'cafe'))
        self.assertTrue(app.search(self.db, 'run'))
        self.assertTrue(app.search(self.db, 'sqlite OR postgres',raw=True))
        for query in ['"','*','foo:bar','a-b','(', 'NEAR(']:
            app.search(self.db, query)


if __name__ == '__main__':
    unittest.main()
