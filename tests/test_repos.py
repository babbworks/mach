"""mach repos: manifest lint, folder rules and read-only status of local clones. Run under both TOML parsers."""
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import mach_repos as mr

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='noreply@example.org', GIT_COMMITTER_NAME='t',
               GIT_COMMITTER_EMAIL='noreply@example.org', GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null')


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd)] + list(args), env=GIT_ENV, check=True, capture_output=True, text=True).stdout


def tree_digest(path):
    """Every file under path (including .git), by relative name and content, so any write shows."""
    h = hashlib.sha256()
    for dp, dn, fn in sorted(os.walk(path)):
        dn.sort()
        for f in sorted(fn):
            p = os.path.join(dp, f)
            h.update(os.path.relpath(p, path).encode())
            if not os.path.islink(p):
                with open(p, 'rb') as fh:
                    h.update(fh.read())
    return h.hexdigest()


GOOD = '''
schema = 1

[layout]
org_root = "na"
repos_dir = "projects/repos"

[folders.drive]
path = "drive"
mode = "single-writer"
writer = "workstation-a"
machines = ["workstation-a", "workstation-b"]
purpose = "Google Drive mirror"

[folders.admin]
path = "projects/admin"
mode = "both"
writer = "any"
purpose = "governance and finance files"

[repos.mach]
remote = "https://github.com/example-org/mach.git"
path = "projects/repos/mach"
legacy_paths = ["projects/mach"]
branch = "main"
machines = ["workstation-c", "workstation-a"]
class = "org"
sync = "auto"

[repos.toolbox]
remote = "https://github.com/example-org/toolbox.git"
path = "toolbox"
machines = ["workstation-c", "workstation-a"]
class = "tool"
sync = "auto"

[repos.notes]
path = "making/notes"
machines = ["workstation-c"]
class = "personal"
local_only = true
sync = "manual"

[repos.other]
remote = "https://github.com/someone-else/other.git"
path = "github/other"
machines = ["workstation-c"]
class = "third-party"
sync = "off"
'''


def levels(results):
    return [lv for lv, _ in results]


def parse(text):
    import mach_subsystem as ss
    return ss.parse(text)


class Lint(unittest.TestCase):
    def test_good_manifest_has_no_failures(self):
        self.assertNotIn('FAIL', levels(mr.lint(parse(GOOD))))

    def bad(self, old, new, expect):
        text = GOOD.replace(old, new)
        self.assertNotEqual(text, GOOD, 'test edit did not apply')
        res = mr.lint(parse(text))
        self.assertTrue(any(lv == 'FAIL' and expect in m for lv, m in res), res)

    def test_credentials_in_remote_fail(self):
        self.bad('https://github.com/example-org/mach.git', 'https://user:tok3n' + '@github.com/example-org/mach.git', 'credentials')

    def test_token_in_remote_fails(self):
        self.bad('https://github.com/example-org/mach.git', 'https://ghp_' + 'abcdefghijklmnopqrstuvwxyz0123456789' + '@github.com/x/y.git', 'credentials')

    def test_duplicate_path_fails(self):
        self.bad('path = "making/notes"', 'path = "projects/repos/mach"', 'duplicate path')

    def test_unknown_key_fails(self):
        self.bad('branch = "main"', 'branch = "main"\ntoken = "x"', 'unknown key')

    def test_org_repo_must_live_in_repos_dir(self):
        self.bad('path = "projects/repos/mach"', 'path = "projects/mach"', 'projects/repos')

    def test_absolute_path_fails(self):
        self.bad('path = "making/notes"', 'path = "/tmp/notes"', 'relative')

    def test_dotdot_path_fails(self):
        self.bad('path = "making/notes"', 'path = "making/../../etc"', 'relative')

    def test_missing_remote_needs_local_only(self):
        self.bad('local_only = true\n', '', 'no remote')

    def test_local_only_with_remote_fails(self):
        self.bad('path = "making/notes"', 'path = "making/notes"\nremote = "https://github.com/a/b.git"', 'local_only')

    def test_bad_sync_value_fails(self):
        self.bad('sync = "auto"', 'sync = "always"', 'sync')

    def test_bad_class_fails(self):
        self.bad('class = "org"', 'class = "misc"', 'class')

    def test_empty_machines_fails(self):
        self.bad('machines = ["workstation-c", "workstation-a"]', 'machines = []', 'machines')

    def test_repo_inside_synced_folder_fails(self):
        self.bad('path = "making/notes"', 'path = "projects/admin/notes"', 'inside synced folder')

    def test_folder_mode_must_be_known(self):
        self.bad('mode = "single-writer"', 'mode = "mirror"', 'mode')

    def test_single_writer_folder_needs_a_writer(self):
        self.bad('mode = "single-writer"\nwriter = "workstation-a"\n', 'mode = "single-writer"\n', 'writer')

    def test_tool_repo_may_live_at_the_home_root(self):
        res = mr.lint(parse(GOOD))
        self.assertFalse([m for lv, m in res if lv == 'FAIL' and 'toolbox' in m], res)

    def test_folder_machines_must_be_a_list_of_names(self):
        self.bad('machines = ["workstation-a", "workstation-b"]', 'machines = []', 'machines')

    def test_writer_must_carry_the_folder(self):
        self.bad('machines = ["workstation-a", "workstation-b"]', 'machines = ["workstation-b"]', 'writer')

    def test_unmanaged_repo_with_wrong_schema_fails(self):
        self.bad('schema = 1', 'schema = 2', 'schema')


class Machines(unittest.TestCase):
    def test_repos_for_machine(self):
        data = parse(GOOD)
        self.assertEqual(sorted(n for n, _ in mr.repos_for(data, 'workstation-a')), ['mach', 'toolbox'])
        self.assertEqual(sorted(n for n, _ in mr.repos_for(data, 'workstation-c')), ['mach', 'notes', 'other', 'toolbox'])
        self.assertEqual(mr.repos_for(data, 'workstation-b'), [])


class Inspect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)
        self.origin = self.home / 'origin.git'
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(self.origin)], env=GIT_ENV, check=True)
        seed = self.home / 'seed'
        subprocess.run(['git', 'clone', '-q', str(self.origin), str(seed)], env=GIT_ENV, check=True, capture_output=True)
        (seed / 'a.txt').write_text('one\n')
        git(seed, 'add', '.')
        git(seed, 'commit', '-qm', 'one')
        git(seed, 'push', '-q', 'origin', 'HEAD:main')
        self.seed = seed
        self.clone = self.home / 'repos' / 'x'
        self.clone.parent.mkdir()
        subprocess.run(['git', 'clone', '-q', str(self.origin), str(self.clone)], env=GIT_ENV, check=True, capture_output=True)
        self.spec = {'remote': str(self.origin), 'path': 'repos/x', 'branch': 'main', 'machines': ['m'], 'class': 'personal', 'sync': 'auto'}

    def look(self, spec=None):
        return mr.inspect(spec or self.spec, home=str(self.home))

    def test_clean_and_current(self):
        r = self.look()
        self.assertEqual((r['state'], r['branch'], r['dirty'], r['ahead'], r['behind']), ('present', 'main', False, 0, 0))
        self.assertEqual(r['problems'], [])

    def test_modified_file_is_dirty(self):
        (self.clone / 'a.txt').write_text('changed\n')
        r = self.look()
        self.assertTrue(r['dirty'])
        self.assertIn('dirty', r['problems'])

    def test_untracked_only_is_dirty_with_its_own_reason(self):
        (self.clone / 'new.txt').write_text('x\n')
        r = self.look()
        self.assertTrue(r['dirty'])
        self.assertEqual(r['dirty_kind'], 'untracked')

    def test_ahead(self):
        (self.clone / 'b.txt').write_text('b\n')
        git(self.clone, 'add', '.')
        git(self.clone, 'commit', '-qm', 'local')
        r = self.look()
        self.assertEqual((r['ahead'], r['behind']), (1, 0))

    def test_behind_after_fetch(self):
        (self.seed / 'c.txt').write_text('c\n')
        git(self.seed, 'add', '.')
        git(self.seed, 'commit', '-qm', 'remote')
        git(self.seed, 'push', '-q', 'origin', 'HEAD:main')
        self.assertEqual(self.look()['behind'], 0, 'no fetch yet, so the remote-tracking ref is stale')
        r = mr.inspect(self.spec, home=str(self.home), fetch=True)
        self.assertEqual(r['behind'], 1)

    def test_diverged(self):
        (self.seed / 'c.txt').write_text('c\n')
        git(self.seed, 'add', '.')
        git(self.seed, 'commit', '-qm', 'remote')
        git(self.seed, 'push', '-q', 'origin', 'HEAD:main')
        (self.clone / 'b.txt').write_text('b\n')
        git(self.clone, 'add', '.')
        git(self.clone, 'commit', '-qm', 'local')
        r = mr.inspect(self.spec, home=str(self.home), fetch=True)
        self.assertEqual((r['ahead'], r['behind']), (1, 1))
        self.assertIn('diverged', r['problems'])

    def test_detached_head(self):
        git(self.clone, 'checkout', '-q', '--detach')
        r = self.look()
        self.assertEqual(r['branch'], None)
        self.assertIn('detached', r['problems'])

    def test_other_branch_checked_out(self):
        git(self.clone, 'checkout', '-q', '-b', 'feature')
        r = self.look()
        self.assertEqual(r['branch'], 'feature')
        self.assertIn('not on main', r['problems'])

    def test_merge_in_progress(self):
        (self.clone / '.git' / 'MERGE_HEAD').write_text(git(self.clone, 'rev-parse', 'HEAD'))
        self.assertIn('merge in progress', self.look()['problems'])

    def test_rebase_in_progress(self):
        (self.clone / '.git' / 'rebase-merge').mkdir()
        self.assertIn('rebase in progress', self.look()['problems'])

    def test_index_lock(self):
        (self.clone / '.git' / 'index.lock').write_text('')
        self.assertIn('index.lock present', self.look()['problems'])

    def test_remote_mismatch(self):
        spec = dict(self.spec, remote=str(self.home / 'elsewhere.git'))
        self.assertIn('remote differs from manifest', self.look(spec)['problems'])

    def test_missing(self):
        r = self.look(dict(self.spec, path='repos/absent'))
        self.assertEqual(r['state'], 'missing')

    def test_found_at_legacy_path(self):
        spec = dict(self.spec, path='repos/new-home', legacy_paths=['repos/x'])
        r = self.look(spec)
        self.assertEqual(r['state'], 'legacy')
        self.assertEqual(r['found_at'], 'repos/x')

    def test_present_wins_over_legacy(self):
        spec = dict(self.spec, legacy_paths=['repos/x'])
        self.assertEqual(self.look(spec)['state'], 'present')

    def test_plain_folder_is_not_git(self):
        (self.home / 'repos' / 'plain').mkdir()
        r = self.look(dict(self.spec, path='repos/plain'))
        self.assertEqual(r['state'], 'not-git')

    def test_sync_off_is_not_inspected(self):
        r = self.look(dict(self.spec, sync='off', path='repos/absent'))
        self.assertEqual(r['state'], 'unmanaged')

    def test_local_only_reports_unbacked(self):
        spec = dict(self.spec)
        del spec['remote']
        spec['local_only'] = True
        self.assertIn('no remote: not backed up', self.look(spec)['problems'])

    def test_inspect_without_fetch_writes_nothing(self):
        (self.clone / 'new.txt').write_text('x\n')
        # same content, newer mtime: a plain `git status` would rewrite .git/index to record it
        st = os.stat(self.clone / 'a.txt')
        os.utime(self.clone / 'a.txt', (st.st_atime + 100, st.st_mtime + 100))
        before = tree_digest(self.clone)
        self.look()
        self.assertEqual(tree_digest(self.clone), before)


class Folders(unittest.TestCase):
    def test_git_inside_synced_folder_is_reported(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, 'projects/admin/sub/.git'))
            os.makedirs(os.path.join(home, 'drive'))
            res = mr.check_folders(parse(GOOD), home=home)
            self.assertTrue(any(lv == 'FAIL' and '.git' in m and 'projects/admin' in m for lv, m in res), res)

    def test_clean_synced_folders_pass(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, 'projects/admin/sub'))
            res = mr.check_folders(parse(GOOD), home=home)
            self.assertNotIn('FAIL', levels(res))

    def test_folder_this_machine_does_not_carry_is_skipped(self):
        with tempfile.TemporaryDirectory() as home:
            res = mr.check_folders(parse(GOOD), home=home, machine='workstation-c')
            self.assertFalse([m for _, m in res if 'drive' in m], res)

    def test_missing_folder_is_a_warning_not_a_failure(self):
        with tempfile.TemporaryDirectory() as home:
            res = mr.check_folders(parse(GOOD), home=home)
            self.assertNotIn('FAIL', levels(res))
            self.assertIn('WARN', levels(res))


class Identity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.old = {k: os.environ.get(k) for k in ('HOME', 'MACH_HOST', 'MACH_MACHINE')}
        os.environ['HOME'] = self.home
        os.environ.pop('MACH_HOST', None)
        os.environ.pop('MACH_MACHINE', None)
        self.addCleanup(self.restore)

    def restore(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def marker(self, text):
        os.makedirs(os.path.join(self.home, '.config', 'mach'))
        with open(os.path.join(self.home, '.config', 'mach', 'machine-id'), 'w') as fh:
            fh.write(text)

    def test_machine_id_file_beats_hostname(self):
        os.environ['MACH_HOSTNAME'] = 'tpad'
        self.addCleanup(os.environ.pop, 'MACH_HOSTNAME', None)
        self.marker('workstation-c\n')
        self.assertEqual(mr.identity(), 'workstation-c')

    def test_env_beats_machine_id_file(self):
        self.marker('workstation-c\n')
        os.environ['MACH_MACHINE'] = 'workstation-a'
        self.assertEqual(mr.identity(), 'workstation-a')

    def test_mach_host_is_honoured_first(self):
        self.marker('workstation-c\n')
        os.environ['MACH_HOST'] = 'workstation-b'
        os.environ['MACH_MACHINE'] = 'workstation-a'
        self.assertEqual(mr.identity(), 'workstation-b')

    def test_falls_back_to_the_mach_host_name(self):
        os.environ['MACH_HOSTNAME'] = 'somebox'
        self.addCleanup(os.environ.pop, 'MACH_HOSTNAME', None)
        self.assertEqual(mr.identity(), 'somebox')

    def test_empty_machine_id_file_is_ignored(self):
        os.environ['MACH_HOSTNAME'] = 'somebox'
        self.addCleanup(os.environ.pop, 'MACH_HOSTNAME', None)
        self.marker('\n')
        self.assertEqual(mr.identity(), 'somebox')


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)
        (self.home / 'machine-flows' / 'repos').mkdir(parents=True)
        (self.home / 'machine-flows' / 'repos' / 'repos.toml').write_text(GOOD)
        self.env = dict(GIT_ENV, HOME=str(self.home), MACH_MF_ROOT=str(self.home / 'machine-flows'), MACH_HOST='workstation-c',
                        PYTHONDONTWRITEBYTECODE='1')

    def mach(self, *args):
        return subprocess.run([str(ROOT / 'bin' / 'mach'), 'repos'] + list(args), env=self.env, capture_output=True, text=True)

    def test_doctor_ok(self):
        p = self.mach('doctor')
        self.assertIn(p.returncode, (0, 1), p.stdout + p.stderr)
        self.assertNotIn('FAIL', p.stdout)

    def test_doctor_fails_on_bad_manifest(self):
        f = self.home / 'machine-flows' / 'repos' / 'repos.toml'
        f.write_text(GOOD.replace('https://github.com/example-org/mach.git', 'https://u:p' + '@github.com/example-org/mach.git'))
        p = self.mach('doctor')
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn('FAIL', p.stdout)

    def test_status_json_lists_this_machines_repos_and_warns_for_missing(self):
        p = self.mach('status', '--json')
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data['machine'], 'workstation-c')
        self.assertEqual(sorted(r['name'] for r in data['repos']), ['mach', 'notes', 'other', 'toolbox'])
        states = {r['name']: r['state'] for r in data['repos']}
        self.assertEqual(states, {'mach': 'missing', 'notes': 'missing', 'other': 'unmanaged', 'toolbox': 'missing'})

    def test_status_machine_flag(self):
        data = json.loads(self.mach('status', '--machine', 'workstation-a', '--json').stdout)
        self.assertEqual([r['name'] for r in data['repos']], ['mach', 'toolbox'])

    def test_status_finds_legacy_clone(self):
        legacy = self.home / 'na' / 'mach'
        legacy.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(legacy)], env=GIT_ENV, check=True)
        data = json.loads(self.mach('status', '--json').stdout)
        mach = [r for r in data['repos'] if r['name'] == 'mach'][0]
        self.assertEqual((mach['state'], mach['found_at']), ('legacy', 'projects/mach'))

    def test_unknown_machine_name_is_flagged_not_silent(self):
        self.env['MACH_HOST'] = 'tpad'
        p = self.mach('status')
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn('not named in the manifest', p.stdout)
        self.assertIn('workstation-c', p.stdout)

    def test_unknown_machine_name_in_json(self):
        self.env['MACH_HOST'] = 'tpad'
        data = json.loads(self.mach('status', '--json').stdout)
        self.assertEqual(data['repos'], [])
        self.assertIn('workstation-c', data['known_machines'])
        self.assertFalse(data['machine_known'])

    def test_missing_manifest_is_an_error(self):
        (self.home / 'machine-flows' / 'repos' / 'repos.toml').unlink()
        p = self.mach('status')
        self.assertEqual(p.returncode, 2)
        self.assertIn('repos.toml', p.stderr + p.stdout)


if __name__ == '__main__':
    unittest.main()
