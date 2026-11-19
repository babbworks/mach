"""Repo manifest and read-only status of local clones.

repos/repos.toml in machine-flows (synced, same on every machine) lists each git repo with its folder, remote and the
machines that carry it, plus the non-git folders that Syncthing may carry. This module loads and lints that file and
looks at the clones on this machine. It never changes a checkout: status uses GIT_OPTIONAL_LOCKS=0 so even the index is
left alone, and a fetch happens only when the caller asks for one.
"""
import datetime
import os
import re
import subprocess

import mach_common as mc
import mach_subsystem as ss

SCHEMA = 1
TOP_KEYS = {'schema', 'layout', 'folders', 'repos'}
LAYOUT_KEYS = {'org_root', 'repos_dir'}
FOLDER_KEYS = {'path', 'mode', 'writer', 'machines', 'purpose'}
REPO_KEYS = {'remote', 'path', 'legacy_paths', 'branch', 'machines', 'class', 'sync', 'local_only', 'release'}
CLASSES = ('org', 'tool', 'personal', 'third-party')   # tool: machine tooling such as mach, at the home root
SYNC = ('auto', 'manual', 'off')
# both: any machine may write; single-writer: only `writer` writes, the others receive; local: this machine only, not in a Syncthing share
FOLDER_MODES = ('both', 'single-writer', 'local')
SYNCED_MODES = ('both', 'single-writer')
DEFAULT_REPOS_DIR = 'projects/repos'
NAME = re.compile(r'^[a-z0-9][a-z0-9._-]*$')
USERINFO = re.compile(r'^[a-z][a-z0-9+.-]*://[^/@]+@', re.I)
TOKEN = re.compile(r'(ghp_|gho_|ghs_|ghu_|github_pat_|glpat-|xox[bap]-)', re.I)
GIT_TIMEOUT = 60
WALK_DEPTH = 8


def identity():
    """This machine's identity in the manifest: MACH_HOST, then MACH_MACHINE, then ~/.config/mach/machine-id (machine-local,
    outside Syncthing; the inventory's explicit identity), then mach's host name. The OS hostname alone is not enough: the
    A user-defined machine ID can differ from its OS hostname."""
    for name in ('MACH_HOST', 'MACH_MACHINE'):
        if os.environ.get(name):
            return os.environ[name]
    try:
        with open(os.path.join(mc.home(), '.config', 'mach', 'machine-id'), encoding='utf-8') as fh:
            ident = fh.read().strip()
        if ident:
            return ident
    except OSError:
        pass
    return mc.host()


def manifest_path():
    return os.path.join(mc.mf_root(), 'repos', 'repos.toml')


def load(path=None):
    """The parsed manifest. A missing or malformed file raises OSError or ValueError."""
    with open(path or manifest_path(), encoding='utf-8') as fh:
        return ss.parse(fh.read())


def _table(data, key):
    t = data.get(key, {}) if isinstance(data, dict) else {}
    return t if isinstance(t, dict) else {}


def repos_dir(data):
    return str(_table(data, 'layout').get('repos_dir') or DEFAULT_REPOS_DIR)


def _rel_ok(p):
    return isinstance(p, str) and p != '' and not p.startswith(('/', '~')) and '..' not in p.split('/')


def _norm(p):
    return p.strip('/')


def _remote_key(url):
    u = str(url).strip().rstrip('/')
    if u.endswith('.git'):
        u = u[:-4]
    return u.lower()


def lint(data):
    """Findings as (level, message). FAIL stops sync later; WARN is advice."""
    out = []

    def fail(m): out.append(('FAIL', m))
    def warn(m): out.append(('WARN', m))

    if not isinstance(data, dict):
        return [('FAIL', 'manifest is not a table')]
    if data.get('schema') != SCHEMA:
        fail('schema must be %d' % SCHEMA)
    for k in sorted(set(data) - TOP_KEYS):
        fail('unknown key %s' % k)
    layout = _table(data, 'layout')
    for k in sorted(set(layout) - LAYOUT_KEYS):
        fail('layout: unknown key %s' % k)
    rdir = repos_dir(data)
    if not _rel_ok(rdir):
        fail('layout: repos_dir must be a relative path')

    folders = _table(data, 'folders')
    synced = []
    for fid, f in sorted(folders.items()):
        if not isinstance(f, dict):
            fail('folder %s: not a table' % fid)
            continue
        for k in sorted(set(f) - FOLDER_KEYS):
            fail('folder %s: unknown key %s' % (fid, k))
        p = f.get('path')
        if not _rel_ok(p):
            fail('folder %s: path must be relative to the home folder, without ..' % fid)
            continue
        if f.get('mode') not in FOLDER_MODES:
            fail('folder %s: mode must be one of %s' % (fid, ', '.join(FOLDER_MODES)))
        if f.get('mode') in SYNCED_MODES:
            synced.append((fid, _norm(p)))
        if f.get('mode') == 'single-writer' and not f.get('writer'):
            fail('folder %s: single-writer needs a writer (a machine name)' % fid)
        if 'machines' in f:
            fm = f['machines']
            if not (isinstance(fm, list) and fm and all(isinstance(m, str) and m for m in fm)):
                fail('folder %s: machines must be a non-empty list of machine names' % fid)
            elif f.get('writer') and f['writer'] not in fm:
                fail('folder %s: writer %s is not among the machines that carry it' % (fid, f['writer']))

    repos = _table(data, 'repos')
    seen = {}
    for name, r in sorted(repos.items()):
        tag = 'repo %s' % name
        if not NAME.match(name):
            fail('%s: name must be lower-case letters, digits, dot, dash or underscore' % tag)
        if not isinstance(r, dict):
            fail('%s: not a table' % tag)
            continue
        for k in sorted(set(r) - REPO_KEYS):
            fail('%s: unknown key %s' % (tag, k))
        p = r.get('path')
        if not _rel_ok(p):
            fail('%s: path must be relative to the home folder, without ..' % tag)
        else:
            np = _norm(p)
            if np in seen:
                fail('%s: duplicate path %s (also %s)' % (tag, p, seen[np]))
            seen[np] = name
            for fid, fp in synced:
                if np == fp or np.startswith(fp + '/'):
                    fail('%s: path %s is inside synced folder %s; a git repo must never sit in a Syncthing share' % (tag, p, fid))
            if r.get('class') == 'org' and np != _norm(rdir) + '/' + name:
                fail('%s: an org repo must live in %s/%s' % (tag, rdir, name))
        if r.get('class') not in CLASSES:
            fail('%s: class must be one of %s' % (tag, ', '.join(CLASSES)))
        if r.get('sync', 'auto') not in SYNC:
            fail('%s: sync must be one of %s' % (tag, ', '.join(SYNC)))
        ms = r.get('machines')
        if not (isinstance(ms, list) and ms and all(isinstance(m, str) and m for m in ms)):
            fail('%s: machines must be a non-empty list of machine names' % tag)
        remote = r.get('remote')
        if remote:
            if not isinstance(remote, str):
                fail('%s: remote must be text' % tag)
            elif USERINFO.match(remote) or TOKEN.search(remote):
                fail('%s: remote carries credentials; use the machine\'s own git/gh login' % tag)
            if r.get('local_only'):
                fail('%s: local_only is set but a remote is given' % tag)
        elif not r.get('local_only'):
            fail('%s: no remote; give one or set local_only = true' % tag)
        if 'legacy_paths' in r:
            lp = r['legacy_paths']
            if not (isinstance(lp, list) and all(_rel_ok(x) for x in lp)):
                fail('%s: legacy_paths must be relative paths' % tag)
        if r.get('local_only'):
            warn('%s: local_only; nothing backs this repo up' % tag)
    return out


def known_machines(data):
    """Every machine name any repo in the manifest names, sorted."""
    names = set()
    for r in _table(data, 'repos').values():
        if isinstance(r, dict):
            names.update(m for m in (r.get('machines') or []) if isinstance(m, str))
    return sorted(names)


def repos_for(data, machine):
    """(name, spec) for each repo the manifest assigns to this machine, sorted by name."""
    out = []
    for name, r in sorted(_table(data, 'repos').items()):
        if isinstance(r, dict) and machine in (r.get('machines') or []):
            out.append((name, r))
    return out


def _git(cwd, *args, timeout=GIT_TIMEOUT):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0', LC_ALL='C', GIT_TERMINAL_PROMPT='0')
    return subprocess.run(['git', '-C', cwd] + list(args), env=env, capture_output=True, text=True, timeout=timeout)


def _locate(spec, home):
    """(state, relative path, absolute path) for the clone: 'present', 'legacy', 'not-git' or 'missing'."""
    for i, rel in enumerate([spec.get('path')] + list(spec.get('legacy_paths') or [])):
        if not rel:
            continue
        ab = os.path.join(home, rel)
        if os.path.isdir(ab):
            if os.path.exists(os.path.join(ab, '.git')):
                return ('present' if i == 0 else 'legacy'), rel, ab
            if i == 0:
                return 'not-git', rel, ab
    return 'missing', spec.get('path'), os.path.join(home, spec.get('path') or '')


def _parse_status(text):
    info = {'branch': None, 'modified': False, 'untracked': False}
    for line in text.splitlines():
        if line.startswith('# branch.head '):
            head = line[len('# branch.head '):]
            info['branch'] = None if head == '(detached)' else head
        elif line.startswith(('1 ', '2 ', 'u ')):
            info['modified'] = True
        elif line.startswith('? '):
            info['untracked'] = True
    return info


def inspect(spec, home=None, fetch=False):
    """Facts about one clone on this machine. Writes nothing unless fetch is set (then only remote-tracking refs)."""
    home = home or mc.home()
    want = str(spec.get('branch') or 'main')
    res = {'path': spec.get('path'), 'state': 'missing', 'found_at': None, 'branch': None, 'dirty': False, 'dirty_kind': None,
           'ahead': None, 'behind': None, 'fetched': False, 'last_fetch': None, 'problems': []}
    problems = res['problems']
    if spec.get('sync') == 'off':
        res['state'] = 'unmanaged'
        return res
    state, rel, ab = _locate(spec, home)
    res['state'] = state
    if state == 'missing':
        return res
    if state == 'not-git':
        problems.append('not a git checkout')
        return res
    if state == 'legacy':
        res['found_at'] = rel
        problems.append('at legacy path %s; move pending' % rel)
    gd = _git(ab, 'rev-parse', '--absolute-git-dir')
    gitdir = gd.stdout.strip() if gd.returncode == 0 else os.path.join(ab, '.git')

    url = _git(ab, 'remote', 'get-url', 'origin')
    origin = url.stdout.strip() if url.returncode == 0 else ''
    if spec.get('remote'):
        if not origin:
            problems.append('no origin remote')
        elif _remote_key(origin) != _remote_key(spec['remote']):
            problems.append('remote differs from manifest')
    elif spec.get('local_only'):
        problems.append('no remote: not backed up')

    if fetch and origin:
        try:
            f = _git(ab, 'fetch', '--prune', '--quiet')
            res['fetched'] = f.returncode == 0
            if f.returncode != 0:
                problems.append('fetch failed: ' + ((f.stderr.strip().splitlines() or ['unknown'])[0])[:80])
        except subprocess.TimeoutExpired:
            problems.append('fetch failed: timed out')

    st = _git(ab, 'status', '--porcelain=v2', '--branch', '--untracked-files=normal')
    info = _parse_status(st.stdout if st.returncode == 0 else '')
    res['branch'] = info['branch']
    if info['modified']:
        res['dirty'], res['dirty_kind'] = True, 'modified'
        problems.append('dirty')
    elif info['untracked']:
        res['dirty'], res['dirty_kind'] = True, 'untracked'
        problems.append('untracked files')

    if info['branch'] is None:
        problems.append('detached')
    elif info['branch'] != want:
        problems.append('not on %s' % want)
    ref = 'origin/%s' % (info['branch'] or want)
    ab_count = _git(ab, 'rev-list', '--left-right', '--count', 'HEAD...' + ref)
    if ab_count.returncode == 0:
        a, b = ab_count.stdout.split()
        res['ahead'], res['behind'] = int(a), int(b)
        if res['ahead'] and res['behind']:
            problems.append('diverged')
        elif res['ahead']:
            problems.append('unpushed commits')

    if os.path.exists(os.path.join(gitdir, 'MERGE_HEAD')):
        problems.append('merge in progress')
    if os.path.isdir(os.path.join(gitdir, 'rebase-merge')) or os.path.isdir(os.path.join(gitdir, 'rebase-apply')):
        problems.append('rebase in progress')
    if os.path.exists(os.path.join(gitdir, 'index.lock')):
        problems.append('index.lock present')
    try:
        t = os.path.getmtime(os.path.join(gitdir, 'FETCH_HEAD'))
        res['last_fetch'] = datetime.datetime.fromtimestamp(t).astimezone().isoformat(timespec='seconds')
    except OSError:
        pass
    return res


def check_folders(data, home=None, machine=None):
    """Findings for the non-git folders this machine carries: absent here (WARN) or a .git inside a synced folder (FAIL)."""
    home = home or mc.home()
    out = []
    for fid, f in sorted(_table(data, 'folders').items()):
        if not isinstance(f, dict) or f.get('mode') not in SYNCED_MODES or not _rel_ok(f.get('path')):
            continue
        if machine and f.get('machines') and machine not in f['machines']:
            continue
        base = os.path.join(home, f['path'])
        if not os.path.isdir(base):
            out.append(('WARN', 'folder %s: %s does not exist on this machine' % (fid, f['path'])))
            continue
        base_depth = base.rstrip('/').count('/')
        for dp, dn, fn in os.walk(base):
            if dp.count('/') - base_depth >= WALK_DEPTH:
                dn[:] = []
            hit = '.git' in dn or '.git' in fn
            if hit:
                rel = os.path.relpath(os.path.join(dp, '.git'), home)
                out.append(('FAIL', 'folder %s: .git found at %s inside synced folder %s' % (fid, rel, f['path'])))
                dn[:] = []
                continue
            dn[:] = [d for d in dn if d != 'node_modules']
    return out
