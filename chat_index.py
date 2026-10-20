#!/usr/bin/env python3
"""Local, incremental full-text search across coding assistant conversations."""
import argparse
import curses
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import textwrap
from datetime import datetime


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path)
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    db.executescript('''
      CREATE TABLE IF NOT EXISTS chats (
        key TEXT PRIMARY KEY, provider TEXT, sid TEXT, title TEXT, cwd TEXT,
        modified REAL, body TEXT, source TEXT, stamp TEXT);
      CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
        title, body, cwd, tokenize='porter unicode61 remove_diacritics 2');
    ''')
    return db


def content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(content(x) for x in value if isinstance(x, (dict, str)))
    if isinstance(value, dict) and value.get('type') in ('text', 'input_text', 'output_text'):
        return value.get('text', '')
    return ''


def records(path):
    with path.open(errors='replace') as stream:
        for line in stream:
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    yield obj
            except ValueError:
                continue  # Writers may be in the middle of appending a record.


def parse_jsonl(path, provider):
    sid, cwd, title = path.stem, '', ''
    messages = []
    for row in records(path):
        kind = row.get('type')
        if provider == 'codex':
            data = row.get('payload', {})
            if kind == 'session_meta':
                sid = data.get('id') or data.get('session_id', sid)
                cwd = data.get('cwd', '')
            if kind != 'response_item' or data.get('type') != 'message':
                continue
            role = data.get('role')
            body = content(data.get('content'))
        else:
            sid = row.get('sessionId', sid)
            cwd = row.get('cwd') or cwd
            if kind == 'custom-title':
                title = row.get('customTitle') or title
            if kind not in ('user', 'assistant'):
                continue
            data = row.get('message', {})
            role = data.get('role', kind)
            body = content(data.get('content'))
        if role in ('user', 'assistant') and body.strip():
            if provider == 'codex' and role == 'user' and body.lstrip().startswith((
                    '<environment_context>', '# AGENTS.md instructions',
                    '<permissions instructions>', '<turn_aborted>')):
                continue
            if not title and role == 'user':
                title = ' '.join(body.split())[:180]
            messages.append(f'{role}: {body}')
    return sid, title or sid, cwd, '\n\n'.join(messages)


def put(db, provider, sid, title, cwd, modified, body, source, stamp):
    key = f'{provider}:{sid}'
    old = db.execute('SELECT rowid FROM chats WHERE key=?', (key,)).fetchone()
    if old:
        db.execute('DELETE FROM search WHERE rowid=?', (old[0],))
        db.execute('DELETE FROM chats WHERE rowid=?', (old[0],))
    cur = db.execute('INSERT INTO chats VALUES (?,?,?,?,?,?,?,?,?)',
                     (key, provider, sid, title, cwd, modified, body, source, stamp))
    db.execute('INSERT INTO search(rowid,title,body,cwd) VALUES (?,?,?,?)',
               (cur.lastrowid, title, body, cwd))


def refresh(db, args):
    changed = 0
    warnings = []
    seen = set()
    old = {r['source']: r['stamp'] for r in db.execute('SELECT source,stamp FROM chats')}
    for provider, root, pattern in [('claude', args.claude_home / 'projects', '*/*.jsonl'),
                                     ('codex', args.codex_home / 'sessions', '**/*.jsonl'),
                                     ('codex', args.codex_home / 'archived_sessions', '**/*.jsonl')]:
        for path in root.glob(pattern):
            source = str(path)
            seen.add(source)
            try:
                stat = path.stat()
                stamp = f'{stat.st_mtime_ns}:{stat.st_size}'
                if old.get(source) == stamp:
                    continue
                sid, title, cwd, body = parse_jsonl(path, provider)
                put(db, provider, sid, title, cwd, stat.st_mtime, body, source, stamp)
                changed += 1
            except (OSError, ValueError, TypeError) as exc:
                warnings.append(f'{path}: {exc}')
    path = args.opencode_home / 'opencode.db'
    if path.exists():
        try:
            with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as src:
                src.row_factory = sqlite3.Row
                for row in src.execute('SELECT * FROM session'):
                    sid = row['id']
                    source = f'{path}#{sid}'
                    seen.add(source)
                    # Parts can change without the parent session timestamp changing.
                    stat = src.execute('SELECT count(*),max(time_updated) FROM part WHERE session_id=?', (sid,)).fetchone()
                    stamp = f'{row["time_updated"]}:{stat[0]}:{stat[1]}'
                    if old.get(source) == stamp:
                        continue
                    texts = []
                    for part in src.execute('SELECT data FROM part WHERE session_id=? ORDER BY time_created,id', (sid,)):
                        try:
                            data = json.loads(part[0])
                            if data.get('type') == 'text' and data.get('text'):
                                texts.append(data['text'])
                        except (ValueError, AttributeError):
                            continue
                    put(db, 'opencode', sid, row['title'], row['directory'], row['time_updated']/1000,
                        '\n\n'.join(texts), source, stamp)
                    changed += 1
        except sqlite3.Error as exc:
            warnings.append(f'{path}: {exc}')
            seen.update(s for s in old if s.startswith(f'{path}#'))
    # Remove stale entries only for sources within this invocation's roots.
    roots = [str(args.claude_home / 'projects') + '/', str(args.codex_home / 'sessions') + '/',
             str(args.codex_home / 'archived_sessions') + '/', str(path) + '#']
    for row in db.execute('SELECT rowid,source FROM chats').fetchall():
        if any(row['source'].startswith(root) for root in roots) and row['source'] not in seen:
            db.execute('DELETE FROM search WHERE rowid=?', (row['rowid'],))
            db.execute('DELETE FROM chats WHERE rowid=?', (row['rowid'],))
    db.commit()
    return changed, warnings


def expression(query):
    # Literal terms ANDed together; quoted phrases stay together. Final term is a prefix.
    tokens = re.findall(r'"([^"\n]+)"|(\S+)', query)
    terms = []
    for phrase, word in tokens:
        value = phrase or word.rstrip('*')
        if value:
            terms.append('"' + value.replace('"', '""') + '"' + ('*' if word else ''))
    return ' AND '.join(terms)


def search(db, query='', provider=None, limit=1000, raw=False):
    match = query if raw else expression(query)
    if not match.strip():
        return []
    where, params = [], []
    if provider:
        where.append('c.provider=?')
        params.append(provider)
    if match:
        where.append('search MATCH ?')
        params.append(match)
        sql = "SELECT c.*,snippet(search,1,'[',']',' … ',36) AS excerpt FROM search JOIN chats c ON c.rowid=search.rowid"
        order = 'bm25(search,8.0,1.0,2.0), c.modified DESC'
    else:
        sql = "SELECT c.*,substr(body,1,350) AS excerpt FROM chats c"
        order = 'c.modified DESC'
    return db.execute(sql + (' WHERE ' + ' AND '.join(where) if where else '') +
                      ' ORDER BY ' + order + ' LIMIT ?', params + [limit]).fetchall()


def recent_chats(db, limit=1000):
    return db.execute(
        "SELECT *,substr(body,1,350) AS excerpt FROM chats "
        "ORDER BY modified DESC,key LIMIT ?", (limit,)
    ).fetchall()


def resume_command(row):
    return {'claude': ['claude', '--resume', row['sid']],
            'codex': ['codex', 'resume', row['sid']],
            'opencode': ['opencode', '--session', row['sid']]}[row['provider']]


def resume(row):
    command = resume_command(row)
    if not shutil.which(command[0]):
        raise RuntimeError(f'{command[0]} is not installed or is missing from PATH')
    if row['cwd']:
        if not Path(row['cwd']).is_dir():
            raise RuntimeError(f'Original project directory no longer exists: {row["cwd"]}')
        os.chdir(row['cwd'])
    os.execvp(command[0], command)


def clean(text):
    return ''.join(c if c.isprintable() or c == '\n' else ' ' for c in text)


def interface(screen, db, args):
    from concurrent.futures import ThreadPoolExecutor
    import time

    curses.curs_set(1)
    screen.timeout(25)
    colors = {}
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:
            background = curses.COLOR_BLACK
        for number, (name, fg, bg) in enumerate([
                ('accent', curses.COLOR_CYAN, background),
                ('muted', curses.COLOR_BLUE, background),
                ('claude', curses.COLOR_YELLOW, background),
                ('codex', curses.COLOR_GREEN, background),
                ('opencode', curses.COLOR_MAGENTA, background),
                ('selected', curses.COLOR_BLACK, curses.COLOR_CYAN),
                ('button', curses.COLOR_BLACK, curses.COLOR_WHITE),
                ('error', curses.COLOR_RED, background)], 1):
            curses.init_pair(number, fg, bg)
            colors[name] = curses.color_pair(number)
    def style(name):
        return colors.get(name, curses.A_REVERSE if name in ('selected', 'button') else curses.A_NORMAL)

    # Each worker owns its SQLite connection. Typing never waits for a query.
    db_path = db.execute('PRAGMA database_list').fetchone()[2]
    def lookup(query, provider):
        with sqlite3.connect(Path(db_path).as_uri() + '?mode=ro', uri=True) as connection:
            connection.row_factory = sqlite3.Row
            if provider == 'recent':
                return recent_chats(connection)
            return search(connection, query, provider, raw=args.raw)

    query, selected, scroll, provider = args.query, 0, 0, args.provider
    rows, error, pending, future = [], '', bool(args.query.strip()), None
    revision, submitted_revision = 0, -1
    due = time.monotonic() + 0.2
    preview_key, lines = None, []
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        while True:
            if future is not None and future.done():
                if submitted_revision == revision:
                    try:
                        rows, error = future.result(), ''
                    except sqlite3.Error as exc:
                        rows, error = [], str(exc)
                    selected, scroll = 0, 0
                future = None
            if pending and future is None and time.monotonic() >= due:
                future = executor.submit(lookup, query, provider)
                submitted_revision, pending = revision, False
            busy = pending or future is not None
            screen.erase()
            height, width = screen.getmaxyx()
            def draw(y, x, text, attr=0, max_width=None):
                if 0 <= y < height and 0 <= x < width-1:
                    try:
                        screen.addnstr(y, x, clean(text).replace('\n', ' '),
                                       max(0, min(width-x-1, max_width if max_width is not None else width)), attr)
                    except curses.error:
                        pass
            def box(y, x, h, w, label):
                if h < 3 or w < 4:
                    return
                draw(y, x, '╭' + '─'*(w-2) + '╮', style('muted'))
                draw(y, x+2, ' ' + label + ' ', style('accent') | curses.A_BOLD, w-4)
                for line in range(y+1, y+h-1):
                    draw(line, x, '│', style('muted'))
                    draw(line, x+w-1, '│', style('muted'))
                draw(y+h-1, x, '╰' + '─'*(w-2) + '╯', style('muted'))

            draw(0, 1, 'WUTCHATWUSDAT', style('accent') | curses.A_BOLD)
            x = 16
            for client in [None, 'recent', 'claude', 'codex', 'opencode']:
                label = ' ' + (client or 'all') + ' '
                draw(0, x, label, style('selected') if provider == client else style(client or 'muted'))
                x += len(label)+1
            recent = provider == 'recent'
            box(2, 0, 3, width-1, 'Recent · all tools · newest first' if recent else 'Search')
            visible_query = '' if recent else query[-max(1,width-8):]
            draw(3, 2, '❯ ', style('accent') | curses.A_BOLD)
            draw(3, 4, 'Type to switch to search' if recent else visible_query, style('muted') if recent else 0)
            status = ('Loading…' if recent else 'Searching…') if busy else (
                f'{len(rows)} ' + ('recent chats' if recent else 'matches') +
                (' · limit 1000' if len(rows) == 1000 else ''))
            draw(5, 1, status, style('accent'))
            if height >= 15 and width >= 45:
                # Wide terminals get a full-height list beside the preview.
                area_height = height-9
                if width >= 110:
                    list_width = width//2
                    list_height = area_height
                    py, px, ph, pw = 6, list_width+1, area_height, width-list_width-2
                else:
                    list_width = width-1
                    list_height = max(4, area_height*2//3)
                    py, px, ph, pw = 6+list_height, 0, area_height-list_height, width-1
                box(6, 0, list_height, list_width, 'Conversations')
                count = max(1,list_height-2)
                start = max(0, selected-count+1)
                for i, row in enumerate(rows[start:start+count], start):
                    y = 7+i-start
                    active = i == selected
                    attr = style('selected') if active else 0
                    draw(y, 1, ' '*(list_width-2), attr)
                    draw(y, 2, '▸' if active else ' ', attr)
                    draw(y, 4, row['provider'][:8].ljust(8), attr if active else style(row['provider']))
                    title = row['title']
                    if recent:
                        title = datetime.fromtimestamp(row['modified']).strftime('%m-%d %H:%M  ') + title
                    draw(y, 13, title, attr | (curses.A_BOLD if active else 0), list_width-15)
                if not rows:
                    message = error or ('Loading…' if busy else (
                        'No indexed chats.' if recent else (
                            'No matching chats.' if query.strip() else 'Type to search your chats.')))
                    draw(7, 2, message, style('error' if error else 'muted'), list_width-4)
                box(py, px, ph, pw, 'Preview · PgUp/PgDn')
                if rows and ph >= 4:
                    row = rows[selected]
                    date = datetime.fromtimestamp(row['modified']).strftime('%Y-%m-%d')
                    draw(py+1, px+2, date + '  ' + row['cwd'], style('accent'), pw-4)
                    key = (row['key'], row['modified'], row['excerpt'], pw)
                    if key != preview_key:
                        preview = row['excerpt'] + '\n\n' + row['body']
                        lines = [line for para in clean(preview).splitlines()
                                 for line in (textwrap.wrap(para, max(1,pw-4)) or [''])]
                        preview_key = key
                    visible = max(0,ph-3)
                    scroll = min(scroll, max(0,len(lines)-visible))
                    for i, line in enumerate(lines[scroll:scroll+visible]):
                        draw(py+2+i, px+2, line, 0, pw-4)
            else:
                draw(7, 1, 'Enlarge terminal to view results.', style('muted'))
            x, button_y = 1, height-2
            for key_label, label in [('Enter','open'), ('↑↓','select'), ('Tab','view'), ('Ctrl-U','clear'), ('Ctrl-R','refresh'), ('Esc','quit')]:
                if x+len(key_label)+len(label)+4 >= width:
                    x, button_y = 1, button_y+1
                if button_y >= height:
                    break
                draw(button_y, x, ' '+key_label+' ', style('button') | curses.A_BOLD)
                x += len(key_label)+3
                draw(button_y, x, label, style('accent'))
                x += len(label)+2
            if height > 4 and width > 8:
                screen.move(3, min(width-4,4+len(visible_query)))
            screen.refresh()
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            if key in ('\x1b', '\x03'):
                return None
            if key in ('\n', '\r', curses.KEY_ENTER) and rows and not busy:
                return rows[selected]
            changed = False
            if key == curses.KEY_UP:
                selected, scroll = max(0,selected-1), 0
            elif key == curses.KEY_DOWN:
                selected, scroll = min(max(0,len(rows)-1),selected+1), 0
            elif key == curses.KEY_NPAGE:
                scroll += max(1,height//2)
            elif key == curses.KEY_PPAGE:
                scroll = max(0,scroll-height//2)
            elif key in ('\t', curses.KEY_BTAB):
                providers = [None,'recent','claude','codex','opencode']
                step = -1 if key == curses.KEY_BTAB else 1
                provider = providers[(providers.index(provider)+step)%len(providers)]
                rows, selected, scroll = [], 0, 0
                changed = True
            elif key == '\x12':
                refresh(db, args)
                changed = True
            elif key in (curses.KEY_BACKSPACE,'\x7f','\b'):
                query, changed = query[:-1], True
            elif key == '\x15':
                query, changed = '', True
            elif isinstance(key,str) and key.isprintable():
                if provider == 'recent':
                    provider, query = None, ''
                    rows, selected, scroll = [], 0, 0
                query, changed = query+key, True
            if changed:
                revision += 1
                pending = provider == 'recent' or bool(query.strip())
                due = time.monotonic() + (0 if provider == 'recent' else 0.2)
                error = ''
                if not pending:
                    rows, selected, scroll = [], 0, 0
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser(prog="wutchatwusdat", description=__doc__)
    parser.add_argument('query', nargs='?', default='')
    parser.add_argument('--provider', choices=['claude','codex','opencode'])
    parser.add_argument('--json', action='store_true', help='Print search results instead of opening the terminal UI')
    parser.add_argument('--index-only', action='store_true')
    parser.add_argument('--no-refresh', action='store_true')
    parser.add_argument('--raw', action='store_true', help='Use SQLite FTS5 query syntax (OR, NOT, NEAR, etc.)')
    parser.add_argument('--db', type=Path, default=Path(os.environ.get('XDG_CACHE_HOME', str(Path.home()/'.cache')))/'wutchatwusdat/index.sqlite3')
    parser.add_argument('--claude-home', type=Path, default=Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home()/'.claude'))))
    parser.add_argument('--codex-home', type=Path, default=Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))))
    parser.add_argument('--opencode-home', type=Path, default=Path(os.environ.get('XDG_DATA_HOME', str(Path.home()/'.local/share')))/'opencode')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        db = connect(args.db.expanduser())
        if not args.no_refresh:
            print('Updating chat index…', file=sys.stderr)
            changed, warnings = refresh(db, args)
            print(f'{db.execute("SELECT count(*) FROM chats").fetchone()[0]} chats indexed; {changed} updated.', file=sys.stderr)
            for warning in warnings:
                print('Warning: ' + warning, file=sys.stderr)
        if args.index_only:
            return
        if args.json:
            print(json.dumps([dict(r) for r in search(db, args.query, args.provider, raw=args.raw)], ensure_ascii=False))
        else:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                parser.error('Interactive search needs a terminal; use --json for scripting.')
            row = curses.wrapper(interface, db, args)
            db.close()
            if row:
                resume(row)
    except (OSError, sqlite3.Error, RuntimeError, curses.error) as exc:
        print(f'wutchatwusdat: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
