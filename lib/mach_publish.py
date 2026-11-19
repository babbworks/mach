"""mach publish: export one tier of a machine-flows tree to a staging folder after a content scan.

A tier is a filtered projection of the whole tree. A line-based manifest says which paths belong to it:

    # comment
    include ai-authored/AGENTS.md        a file, or a glob (`*` also matches `/`)
    include plans/                       a folder (trailing slash = everything below)
    exclude plans/archived/              later lines override earlier ones
    map     tiers/org/README.md README.md   export SRC under another path
    scan    secrets paths names tickets  which checks run (secrets always run)
    deny    SomeOrgName                  literal, case-insensitive; flagged by the `names` check
    allow   docs/*.md tickets            suppress one kind of finding for a glob

Nothing here runs git, pushes or commits. `--apply` only writes the export folder.
"""
import csv
import fnmatch
import hashlib
import io
import json
import os
import posixpath
import re
import stat
import sys

# never exported, whatever the manifest says
NEVER_PARTS = (".git", ".tmp", ".stfolder", ".stversions", "node_modules", "__pycache__")
NEVER_GLOBS = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "*.sqlite", "*.sqlite3",
               "*credential*", "*secret*", "*.token", "*token*.txt", ".DS_Store", "*.pyc")
MAX_SCAN_BYTES = 8000000   # larger files are reported as `unscanned`, never skipped silently

SECRET_PATTERNS = (
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b|\bAKIA[0-9A-Z]{16}\b|\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("assigned-secret", re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=]{16,}")),
)
PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9_.~-])(?:/home/[a-z][a-z0-9_-]*|/Users/[A-Za-z][A-Za-z0-9_-]*)")
TICKET_PATTERN = re.compile(r"\b[A-Z]{2,5}-\d{2,5}\b")


class ApplyError(RuntimeError):
    """A source file changed after it was scanned; nothing was written."""


class ManifestError(ValueError):
    """The manifest file is missing or has a line the parser does not understand."""


class Manifest(object):
    """Parsed tier manifest: ordered path rules, remaps, scan switches, deny words and allowances."""

    def __init__(self):
        self.rules = []     # (kind, pattern) with kind include|exclude, in file order
        self.maps = {}      # source path -> exported path
        self.scans = set(["secrets"])
        self.deny = []
        self.allow = []     # (glob, kind)


def parse_manifest(text):
    """Parse manifest text into a Manifest; raise ManifestError with the line number on a bad line."""
    man = Manifest()
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        word, args = parts[0], parts[1:]
        if word in ("include", "exclude") and len(args) == 1:
            man.rules.append((word, args[0]))
        elif word == "map" and len(args) == 2:
            if not valid_dest(args[1]):
                raise ManifestError("line %d: map destination %r must be a clean relative path (no .., no leading /)" % (number, args[1]))
            man.maps[args[0]] = args[1]
        elif word == "scan" and args:
            bad = [a for a in args if a not in ("secrets", "paths", "names", "tickets")]
            if bad:
                raise ManifestError("line %d: unknown scan check %s" % (number, bad[0]))
            man.scans.update(args)
        elif word == "deny" and args:
            man.deny.append(" ".join(args))
        elif word == "allow" and len(args) == 2:
            man.allow.append((args[0], args[1]))
        else:
            raise ManifestError("line %d: cannot parse %r" % (number, raw.strip()))
    return man


FEATURE_COLUMNS = ("feature", "paths", "tiers", "status", "note")
FEATURE_STATUSES = ("ready", "needs-edit", "org-only", "planned")


def _feature_rows(text):
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in FEATURE_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise ManifestError("features file lacks column(s): %s" % ", ".join(missing))
    rows = []
    for number, row in enumerate(reader, 2):
        status = (row["status"] or "").strip()
        if status not in FEATURE_STATUSES:
            raise ManifestError("features line %d: status %r is not one of %s" % (number, status, ", ".join(FEATURE_STATUSES)))
        paths = [p.strip() for p in (row["paths"] or "").split(";") if p.strip()]
        tiers = set(t.strip() for t in (row["tiers"] or "").split(";") if t.strip())
        rows.append({"feature": row["feature"].strip(), "paths": paths, "tiers": tiers, "status": status})
    return rows


def manifest_from_features(features_text, tier, rules_text=""):
    """Build a Manifest from a features index: rows that name this tier AND have status `ready` are included; every other
    row is excluded for this tier afterwards (so a held-back file never leaks through a broader row). `rules_text` is
    ordinary manifest syntax and supplies the scan, deny and allow lines."""
    man = parse_manifest(rules_text) if rules_text.strip() else Manifest()
    live, held = [], []
    for row in _feature_rows(features_text):
        target = live if (tier in row["tiers"] and row["status"] == "ready") else held
        target.extend(row["paths"])
    man.rules = [("include", p) for p in live] + [("exclude", p) for p in held] + man.rules
    return man


def unlisted_files(features_text, files):
    """Files that no row of the features index names (every file must be classified, even if only to hold it back)."""
    patterns = [p for row in _feature_rows(features_text) for p in row["paths"]]
    return sorted(f for f in files if not any(_matches(p, f) for p in patterns))


def dead_feature_paths(features_text, files):
    """Index paths that match no file: stale rows."""
    return sorted(p for row in _feature_rows(features_text) for p in row["paths"] if not any(_matches(p, f) for f in files))


def valid_dest(dest):
    """True for a clean relative posix path that stays below its folder: no absolute path, no `..` or `.` parts,
    no empty parts, no NUL, no backslash."""
    if not dest or dest.startswith("/") or "\0" in dest or "\\" in dest:
        return False
    return posixpath.normpath(dest) == dest and dest not in (".", "..") and not dest.startswith("../")


def _checked_target(out, dest):
    """Return the absolute path for dest below out, or raise ApplyError. Refuses an unsafe dest and any path that passes
    through a symlink inside the export folder (it could lead outside)."""
    if not valid_dest(dest):
        raise ApplyError("unsafe destination path %r" % dest)
    base = os.path.realpath(out)
    current = out
    for part in dest.split("/"):
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise ApplyError("%s passes through a symlink inside the export folder" % dest)
    target = os.path.join(out, dest)
    if os.path.commonpath([base, os.path.realpath(target)]) != base:
        raise ApplyError("%s resolves outside the export folder" % dest)
    return target


def _matches(pattern, rel):
    if pattern.endswith("/"):
        return rel.startswith(pattern)
    return fnmatch.fnmatchcase(rel, pattern)


def _never(rel):
    parts = rel.split("/")
    if any(p in NEVER_PARTS for p in parts):
        return True
    name = parts[-1]
    if name.endswith(".example") or ".example." in name:
        return False   # placeholder files such as secrets.toml.example; their content is still scanned
    return any(fnmatch.fnmatchcase(name.lower(), g.lower()) for g in NEVER_GLOBS)


def walk_tree(root):
    """Yield relative posix paths of regular files under root. Symlinks are skipped (they never travel by sync)."""
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in NEVER_PARTS)
        for name in sorted(files):
            full = os.path.join(base, name)
            if os.path.islink(full):
                continue
            yield os.path.relpath(full, root).replace(os.sep, "/")


def select(man, files):
    """Return {exported path: source path} for the files the manifest includes, after remaps and the never-list."""
    chosen = {}
    for rel in files:
        if _never(rel):
            continue
        take = False
        for kind, pattern in man.rules:
            if _matches(pattern, rel):
                take = kind == "include"
        if take:
            chosen[man.maps.get(rel, rel)] = rel
    for src, dst in man.maps.items():
        if src in files and not _never(src):
            chosen[dst] = src
    return chosen


def _mask(text):
    text = text.strip()
    return text[:3] + "***" if len(text) > 3 else "***"


def scan_text(rel, text, man):
    """Return findings [(kind, line number, masked excerpt)] for one file; secret values are never echoed in full."""
    findings = []
    deny = [d.lower() for d in man.deny]
    for number, line in enumerate(text.splitlines(), 1):
        for kind, pattern in SECRET_PATTERNS:
            found = pattern.search(line)
            if found:
                findings.append((kind, number, _mask(found.group(0))))
        if "paths" in man.scans:
            found = PATH_PATTERN.search(line)
            if found:
                findings.append(("machine-path", number, found.group(0)))
        if "tickets" in man.scans:
            found = TICKET_PATTERN.search(line)
            if found:
                findings.append(("ticket-key", number, found.group(0)))
        if "names" in man.scans:
            low = line.lower()
            for word in deny:
                if word in low:
                    findings.append(("denied-name", number, word))
    return filter_allowed(rel, findings, man)


def filter_allowed(rel, findings, man):
    """Drop findings the manifest allows for this file (`allow GLOB KIND`; KIND may be a check name or a finding kind)."""
    kind_alias = {"secrets": ("github-token", "api-key", "private-key", "assigned-secret"), "paths": ("machine-path",),
                  "tickets": ("ticket-key",), "names": ("denied-name",)}
    skip = set()
    for glob, name in man.allow:
        if fnmatch.fnmatchcase(rel, glob):
            skip.update(kind_alias.get(name, (name,)))
    return [f for f in findings if f[0] not in skip]


def read_source(path, keep=False):
    """Read a source file ONCE, never following a symlink. Returns (data, sha256 hex, size, reason).

    The hash and the scan text both come from these same bytes, so what is scanned is exactly what is hashed (and later
    verified and copied). `data` is None when the file is larger than MAX_SCAN_BYTES (hashed as a stream; pass keep=True to
    get the bytes anyway), is not a regular file, or cannot be opened; `reason` says why."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        return None, "", 0, "unreadable (%s)" % exc.__class__.__name__
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, "", 0, "not a regular file"
        digest = hashlib.sha256()
        chunks = []
        size = 0
        too_big = info.st_size > MAX_SCAN_BYTES
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            digest.update(block)
            size += len(block)
            if keep or not too_big:
                chunks.append(block)
        reason = "larger than %d bytes" % MAX_SCAN_BYTES if too_big else ""
        return (b"".join(chunks) if (keep or not too_big) else None), digest.hexdigest(), size, reason
    except OSError as exc:
        return None, "", 0, "unreadable (%s)" % exc.__class__.__name__
    finally:
        os.close(fd)


def build_plan(root, man, state):
    """Select, scan and compare with the last export. Returns a dict the printers and --apply both use."""
    files = list(walk_tree(root))
    chosen = select(man, set(files))
    entries = []
    findings = []
    for dest in sorted(chosen):
        src = chosen[dest]
        full = os.path.join(root, src)
        data, digest, size, reason = read_source(full)
        if data is None:   # fail closed: a file that cannot be scanned is a finding, never a pass
            found = filter_allowed(src, [("unscanned", 0, reason)], man)
        else:
            found = scan_text(src, data.decode("utf-8", "replace"), man)
        for kind, number, excerpt in found:
            findings.append({"file": src, "line": number, "kind": kind, "excerpt": excerpt})
        previous = state.get(dest)
        status = "new" if previous is None else ("changed" if previous != digest else "same")
        entries.append({"dest": dest, "src": src, "sha": digest, "size": size, "status": status})
    leftover = set(state) - set(chosen)
    removed = sorted(d for d in leftover if valid_dest(d))
    ignored = sorted(d for d in leftover if not valid_dest(d))   # a doctored state file can never make us delete elsewhere
    return {"entries": entries, "findings": findings, "removed": removed, "ignored_state": ignored}


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle).get("files", {})
    except (OSError, ValueError):
        return {}


def save_state(path, files):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"files": files}, handle, indent=1, sort_keys=True)


def _dir_flags():
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _have_dir_fd():
    return os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd and os.unlink in os.supports_dir_fd


def _write_below(out, dest, data):
    """Write data to out/dest without ever following a symlink: each directory is entered with O_NOFOLLOW relative to the
    previous one, so swapping a directory for a link after a check cannot redirect the write."""
    if not _have_dir_fd():   # platforms without dir_fd support: fall back to the pre-checked path
        target = _checked_target(out, dest)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(data)
        return
    parts = dest.split("/")
    fd = os.open(os.path.realpath(out), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o755, dir_fd=fd)
            except FileExistsError:
                pass
            nfd = os.open(part, _dir_flags(), dir_fd=fd)
            os.close(fd)
            fd = nfd
        wfd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644, dir_fd=fd)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(wfd, view):]
        finally:
            os.close(wfd)
    except OSError as exc:
        raise ApplyError("cannot write %s safely (%s)" % (dest, exc.__class__.__name__))
    finally:
        os.close(fd)


def _remove_below(out, dest):
    """Delete out/dest if it is a regular file, entering directories with O_NOFOLLOW; anything odd is left alone."""
    if not _have_dir_fd():
        try:
            target = _checked_target(out, dest)
        except ApplyError:
            return
        if os.path.isfile(target) and not os.path.islink(target):
            os.remove(target)
        return
    parts = dest.split("/")
    try:
        fd = os.open(os.path.realpath(out), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        for part in parts[:-1]:
            nfd = os.open(part, _dir_flags(), dir_fd=fd)
            os.close(fd)
            fd = nfd
        if stat.S_ISREG(os.stat(parts[-1], dir_fd=fd, follow_symlinks=False).st_mode):
            os.unlink(parts[-1], dir_fd=fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def apply_plan(root, out, plan):
    """Write the export folder: copy included files, delete files exported earlier that left the tier. No git.

    Every file is re-read and checked against the hash it had when it was scanned; if any differs, nothing is written."""
    os.makedirs(out, exist_ok=True)
    loaded = []
    for entry in plan["entries"]:
        _checked_target(out, entry["dest"])   # validate every destination before anything is read or written
        data, digest, _size, reason = read_source(os.path.join(root, entry["src"]), keep=True)
        if data is None or digest != entry["sha"]:
            raise ApplyError("%s changed after it was scanned (or cannot be read: %s); run the preview again" % (entry["src"], reason or "content differs"))
        loaded.append((entry["dest"], data))
    for dest, data in loaded:
        _checked_target(out, dest)
        _write_below(out, dest, data)
    for dest in plan["removed"]:
        _remove_below(out, dest)


def summarize(plan):
    counts = {"new": 0, "changed": 0, "same": 0}
    total = 0
    for entry in plan["entries"]:
        counts[entry["status"]] += 1
        total += entry["size"]
    return {"files": len(plan["entries"]), "bytes": total, "new": counts["new"], "changed": counts["changed"],
            "same": counts["same"], "removed": len(plan["removed"]), "findings": len(plan["findings"])}


def print_report(tier, plan, out, status_only, stream=None):
    stream = stream or sys.stdout
    info = summarize(plan)
    w = stream.write
    w("tier %s: %d files, %d bytes (%d new, %d changed, %d unchanged, %d removed since last export)\n"
      % (tier, info["files"], info["bytes"], info["new"], info["changed"], info["same"], info["removed"]))
    if status_only:
        for entry in plan["entries"]:
            if entry["status"] != "same":
                w("  %-8s %s\n" % (entry["status"], entry["dest"]))
        for dest in plan["removed"]:
            w("  %-8s %s\n" % ("removed", dest))
    else:
        tops = {}
        for entry in plan["entries"]:
            top = entry["dest"].split("/", 1)[0] if "/" in entry["dest"] else "(root files)"
            tops[top] = tops.get(top, 0) + 1
        for top in sorted(tops):
            w("  %-28s %d\n" % (top, tops[top]))
    if plan.get("ignored_state"):
        w("warning: ignored %d unsafe path(s) in the state file (never deleted): %s\n" % (len(plan["ignored_state"]), ", ".join(plan["ignored_state"][:3])))
    if plan["findings"]:
        w("scan: %d finding(s), values masked:\n" % len(plan["findings"]))
        for item in plan["findings"][:60]:
            w("  %s:%d  %s  %s\n" % (item["file"], item["line"], item["kind"], item["excerpt"]))
        if len(plan["findings"]) > 60:
            w("  ... %d more\n" % (len(plan["findings"]) - 60))
    else:
        w("scan: clean\n")
    w("export folder: %s\n" % out)


def _usage_error(message):
    sys.stderr.write("mach: %s\n" % message)
    return 2


def main(argv):
    """Entry point for `mach publish`. Exit codes: 0 ok, 1 findings (preview) or apply refused, 2 failure."""
    opts = {"tier": None, "apply": False, "status": False, "out": None, "manifest": None, "root": None,
            "features": None, "rules": None}
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg in ("--tier", "--out", "--manifest", "--root", "--features", "--rules"):
            if not args:
                return _usage_error("%s needs a value (try: mach publish --help)" % arg)
            opts[arg[2:]] = args.pop(0)
        elif arg == "--apply":
            opts["apply"] = True
        elif arg == "--status":
            opts["status"] = True
        elif arg in ("--dry-run", "--yes", "-y"):
            pass
        else:
            return _usage_error("unknown option: %s (try: mach publish --help)" % arg)
    tier = opts["tier"]
    if not tier or not re.match(r"^[a-z][a-z0-9-]*$", tier):
        return _usage_error("--tier NAME is required (a lower-case word such as org or public)")
    root = opts["root"] or os.environ.get("MACH_MF_ROOT") or os.path.expanduser("~/machine-flows")
    staging = os.environ.get("MACH_STAGING") or os.path.expanduser("~/machine-flows-staging")
    tiers_dir = os.environ.get("MACH_TIERS_DIR") or os.path.join(root, "service-settings", "tiers")
    manifest_path = opts["manifest"] or os.path.join(tiers_dir, tier + ".manifest")
    out = os.path.abspath(opts["out"] or os.path.join(staging, "publish", tier))
    state_path = os.path.join(staging, "publish", tier + ".state.json")
    root_abs = os.path.abspath(root)
    if out == root_abs or out.startswith(root_abs + os.sep):
        return _usage_error("the export folder must be outside %s (it is synced); got %s" % (root_abs, out))
    try:
        if opts["features"]:
            with open(opts["features"], "r", encoding="utf-8") as handle:
                features_text = handle.read()
            rules_path = opts["rules"] or os.path.join(os.path.dirname(os.path.abspath(opts["features"])), "publish.rules")
            rules_text = ""
            if os.path.isfile(rules_path):
                with open(rules_path, "r", encoding="utf-8") as handle:
                    rules_text = handle.read()
            man = manifest_from_features(features_text, tier, rules_text)
        else:
            with open(manifest_path, "r", encoding="utf-8") as handle:
                man = parse_manifest(handle.read())
    except OSError:
        return _usage_error("no manifest for tier %s: expected %s" % (tier, opts["features"] or manifest_path))
    except ManifestError as exc:
        return _usage_error("%s: %s" % (opts["features"] or manifest_path, exc))
    state = load_state(state_path)
    plan = build_plan(root_abs, man, state)
    if os.environ.get("MACH_JSON"):
        payload = {"tier": tier, "out": out, "summary": summarize(plan), "findings": plan["findings"],
                   "changed": [e["dest"] for e in plan["entries"] if e["status"] != "same"], "removed": plan["removed"]}
        sys.stdout.write(json.dumps(payload, indent=1) + "\n")
    else:
        print_report(tier, plan, out, opts["status"])
    if not opts["apply"]:
        if not os.environ.get("MACH_JSON"):
            sys.stdout.write("preview only: nothing written. Add --apply to write the export folder (no git is run).\n")
        return 1 if plan["findings"] else 0
    if plan["findings"]:
        sys.stderr.write("mach: refusing to apply: fix or allow the %d finding(s) above first\n" % len(plan["findings"]))
        return 1
    try:
        apply_plan(root_abs, out, plan)
    except ApplyError as exc:
        sys.stderr.write("mach: refusing to apply: %s\n" % exc)
        return 1
    save_state(state_path, dict((e["dest"], e["sha"]) for e in plan["entries"]))
    sys.stdout.write("wrote %d files to %s\nnext (by hand): git -C %s status, review, then commit and push yourself.\n"
                     % (len(plan["entries"]), out, out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
