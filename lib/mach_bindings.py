"""Bindings keyed by stable system IDs: per-machine checkout paths and routing for one subsystem.

subsystems/<id>/bindings.toml sits beside subsystem.toml. Shared facts (a unit's name, kind, repo, folder) live in the
subsystem manifest; this file holds only paths per machine and routing. `legacy` is the manifest key. While the manifest
still drives routing, drift between the two is a warning, never a failure.
"""
import os
import re

import mach_subsystem as ss

UNIT_ID = re.compile(r'^U-\d{4,}$')
REPO_ID = re.compile(r'^R-\d{4,}$')
COMPANY_FACTS = {'name', 'kind', 'parent', 'repo', 'folder', 'verified', 'notes', 'types', 'remote', 'visibility', 'purpose'}
UNIT_KEYS = {'legacy', 'roles', 'routines', 'agent_files', 'recipes', 'todo', 'special'}
REPO_KEYS = {'legacy', 'paths'}
TOP_KEYS = {'schema', 'assembly_remote', 'assembly_root', 'repos', 'units'}
ROUTING_LISTS = ('routines', 'agent_files', 'recipes')


def path(sid):
    return os.path.join(ss.root(), sid, 'bindings.toml')


def load(sid):
    """The parsed file, or None when the subsystem has no bindings. A malformed file raises ValueError."""
    p = path(sid)
    if not os.path.isfile(p):
        return None
    with open(p, encoding='utf-8') as fh:
        return ss.parse(fh.read())


def is_unit_id(s):
    return isinstance(s, str) and bool(UNIT_ID.fullmatch(s))


def _table(b, name):
    t = b.get(name, {}) if isinstance(b, dict) else {}
    return t if isinstance(t, dict) else {}


def unit_legacy(b, uid):
    u = _table(b, 'units').get(uid)
    return u.get('legacy') if isinstance(u, dict) else None


def _by_legacy(b, table, legacy):
    for k, v in _table(b, table).items():
        if isinstance(v, dict) and v.get('legacy') == legacy:
            return k
    return None


def unit_id(b, legacy):
    return _by_legacy(b, 'units', legacy)


def repo_id(b, legacy):
    return _by_legacy(b, 'repos', legacy)


def repo_path(b, rid, machine):
    """The checkout path recorded for this machine, or None (unbound is not an error here; the caller decides)."""
    r = _table(b, 'repos').get(rid)
    paths = r.get('paths') if isinstance(r, dict) else None
    v = paths.get(machine) if isinstance(paths, dict) else None
    return v if isinstance(v, str) and v else None


def lint(b, manifest):
    out = []
    def fail(msg): out.append(('FAIL', msg))
    def warn(msg): out.append(('WARN', msg))
    if not isinstance(b, dict):
        return [('FAIL', 'bindings must be a table')]
    if b.get('schema') != 1: fail('bindings schema must be 1')
    for k in b:
        if k not in TOP_KEYS: fail('bindings: unknown key ' + k)
    root = b.get('assembly_root', {})
    if not isinstance(root, dict) or not all(isinstance(v, str) and v for v in root.values()): fail('bindings: assembly_root must map machine names to paths')
    repos, units = b.get('repos', {}), b.get('units', {})
    if not isinstance(repos, dict) or not isinstance(units, dict):
        fail('bindings: repos and units must be keyed tables')
        return out
    m_units, m_repos, services = (manifest.get(k, {}) for k in ('units', 'repos', 'services'))
    seen = {}
    def legacy_of(kind, key, entry):
        lg = entry.get('legacy')
        if not isinstance(lg, str) or not lg:
            fail('%s %s needs a legacy key (its key in the manifest)' % (kind, key)); return None
        if (kind, lg) in seen: fail('legacy %s is used twice (%s, %s)' % (lg, seen[(kind, lg)], key))
        seen[(kind, lg)] = key
        return lg
    def unknown(kind, key, entry, allowed):
        for k in entry:
            if k in COMPANY_FACTS: fail('%s %s has %s: company facts belong in the Assembly System profile, not in bindings' % (kind, key, k))
            elif k not in allowed: fail('%s %s has unknown key %s' % (kind, key, k))
    for rid, r in repos.items():
        if not REPO_ID.fullmatch(rid) or not isinstance(r, dict): fail('bad repo id %s (expected R-0001)' % rid); continue
        unknown('repo', rid, r, REPO_KEYS)
        legacy_of('repo', rid, r)
        paths = r.get('paths', {})
        if not isinstance(paths, dict) or not all(isinstance(v, str) and v for v in paths.values()): fail('repo %s paths must map machine names to path strings' % rid)
    for uid, u in units.items():
        if not UNIT_ID.fullmatch(uid) or not isinstance(u, dict): fail('bad unit id %s (expected U-0001)' % uid); continue
        unknown('unit', uid, u, UNIT_KEYS)
        lg = legacy_of('unit', uid, u)
        if lg and lg not in m_units: fail('unit %s: legacy %s is not a unit in the manifest' % (uid, lg))
        for k in ROUTING_LISTS:
            if k in u and (not isinstance(u[k], list) or not all(isinstance(x, str) for x in u[k])): fail('unit %s %s must be a string array' % (uid, k))
        roles = u.get('roles', {})
        if not isinstance(roles, dict): fail('unit %s roles must be a table' % uid); continue
        for role, dest in roles.items():
            if role in ss.ROLE_META: continue
            if not ss.dest_ok(dest): fail('unit %s role %s has an invalid destination' % (uid, role))
            elif dest.startswith('grokbot:') and dest not in services: fail('unit %s destination %s lacks a service profile in the manifest' % (uid, dest))
        mu = m_units.get(lg) if lg else None
        if isinstance(mu, dict):
            if roles != mu.get('roles', {}): warn('unit %s (%s): roles differ from the manifest (the manifest still drives routing)' % (uid, lg))
            for k in ROUTING_LISTS + ('todo', 'special'):
                if u.get(k) != mu.get(k): warn('unit %s (%s): %s differs from the manifest' % (uid, lg, k))
    for key in m_units:
        if ('unit', key) not in seen: warn('unit %s has no bindings entry' % key)
    for key in m_repos:
        if ('repo', key) not in seen: warn('repo %s has no bindings entry' % key)
    return out
