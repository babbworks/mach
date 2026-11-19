"""Append-only run index at files/runs/index.csv (stdlib only).

One row per finished run (done / failed / cancelled). Concurrent writers use
fcntl.flock. Existing rows are never rewritten; same run_id+status is skipped.
"""
import csv
import datetime
import fcntl
import io
import os
import re

import frontmatter as fm
import mach_common as mc

INDEX_COLS = [
    "run_id", "date", "machine", "tool", "model", "repo",
    "linear_issue", "requester", "status", "duration", "link",
]
FINISH_STATUSES = frozenset(("done", "failed", "cancelled"))
LINEAR_RE = re.compile(r"\b((?:NA|NIA)-\d+)\b")
HEADER_LINE = ",".join(INDEX_COLS) + "\n"


def index_path(cfg=None):
    """Path to the append-only index: <runs_dir>/index.csv (runs_dir uses mf_root by default)."""
    return os.path.join(mc.runs_dir(cfg), "index.csv")


def _runs_root_for(run_dir):
    """Return the runs root (.../files/runs) for a .../YYYY/MM/<id> folder, else None."""
    d = os.path.realpath(run_dir)
    parts = d.split(os.sep)
    if len(parts) < 3:
        return None
    month, year = parts[-2], parts[-3]
    if not (year.isdigit() and len(year) == 4 and month.isdigit() and 1 <= int(month) <= 12):
        return None
    return os.sep.join(parts[:-3]) if parts[:-3] else os.sep


def _iso_local(dt=None):
    t = dt if dt is not None else mc.now()
    if getattr(t, "tzinfo", None) is not None:
        return t.isoformat(timespec="seconds")
    return t.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_dt(value):
    if not value:
        return None
    s = str(value).strip()
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def _is_git_repo(path):
    if not path:
        return False
    p = mc.expand(str(path))
    return os.path.isdir(os.path.join(p, ".git")) or os.path.isfile(os.path.join(p, ".git"))


def _repo_basename(path):
    if not _is_git_repo(path):
        return ""
    return os.path.basename(os.path.normpath(mc.expand(str(path))))


def _read_brief(run):
    """Return (meta dict, body text) from the run's input copy, or ({}, '')."""
    name = run.meta.get("input_file") or "input.md"
    path = run.path_of(name)
    if not os.path.isfile(path):
        for cand in ("input.md", "brief.md"):
            p = run.path_of(cand)
            if os.path.isfile(p):
                path = p
                break
        else:
            return {}, ""
    doc = fm.parse_file(path)
    return doc.meta or {}, doc.body or ""


def _first_linear(*parts):
    for part in parts:
        if part is None:
            continue
        if isinstance(part, (list, tuple)):
            text = " ".join(str(x) for x in part)
        else:
            text = str(part)
        m = LINEAR_RE.search(text)
        if m:
            return m.group(1)
    return ""


def _duration_seconds(run):
    raw = run.meta.get("elapsed_seconds")
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    ms = run.meta.get("agent_duration_ms")
    if ms is not None and str(ms).strip() != "":
        try:
            return str(round(float(ms) / 1000.0, 3))
        except (TypeError, ValueError):
            pass
    start = _parse_dt(run.meta.get("agent_start") or run.meta.get("created"))
    end = _parse_dt(run.meta.get("agent_end") or run.meta.get("finished") or run.meta.get("updated"))
    if start and end:
        return str(round((end - start).total_seconds(), 3))
    return ""


def _link_for(run):
    root = os.path.realpath(mc.mf_root())
    d = os.path.realpath(run.dir)
    try:
        rel = os.path.relpath(d, root)
    except ValueError:
        return mc.tilde(d)
    if rel.startswith(".."):
        return mc.tilde(d)
    return rel.replace(os.sep, "/")


def row_from_run(run):
    """Build an index row dict from a Run (finish status assumed)."""
    brief_meta, brief_body = _read_brief(run)
    model = str(run.meta.get("agent_model") or brief_meta.get("model") or "").strip()
    path = run.meta.get("path_abs") or run.meta.get("path") or ""
    title = run.meta.get("title") or brief_meta.get("title") or ""
    tags = brief_meta.get("tags") or run.meta.get("tags") or ""
    linear = _first_linear(title, tags, brief_body, run.meta.get("input", ""))
    requester = str(brief_meta.get("requester") or "").strip() or "owner"
    date = _iso_local(_parse_dt(run.meta.get("finished") or run.meta.get("agent_end") or run.meta.get("updated")) or mc.now())
    return {
        "run_id": run.id,
        "date": date,
        "machine": str(run.meta.get("machine") or ""),
        "tool": str(run.meta.get("actual_tool") or run.meta.get("tool") or ""),
        "model": model,
        "repo": _repo_basename(path),
        "linear_issue": linear,
        "requester": requester,
        "status": str(run.status),
        "duration": _duration_seconds(run),
        "link": _link_for(run),
    }


def _row_tuple(row):
    return tuple(str(row.get(c, "")) for c in INDEX_COLS)


def _existing_keys(fh):
    """Set of (run_id, status) already present. fh must be readable from start."""
    fh.seek(0)
    text = fh.read()
    keys = set()
    if not text.strip():
        return keys
    reader = csv.DictReader(io.StringIO(text))
    for r in reader:
        if not r:
            continue
        rid = (r.get("run_id") or "").strip()
        st = (r.get("status") or "").strip()
        if rid:
            keys.add((rid, st))
    return keys


def append_row(row, path=None):
    """Append one row under an exclusive lock. Returns True if written, False if skipped."""
    path = path or index_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = _csv_line(row)
    with open(path, "a+", encoding="utf-8", newline="") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            keys = _existing_keys(fh)
            key = (str(row.get("run_id", "")), str(row.get("status", "")))
            if key in keys and key[0]:
                return False
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(HEADER_LINE)
            elif not _ends_with_newline(fh):
                fh.write("\n")
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
            return True
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _ends_with_newline(fh):
    if fh.tell() == 0:
        return True
    fh.seek(fh.tell() - 1)
    ch = fh.read(1)
    fh.seek(0, os.SEEK_END)
    return ch == "\n"


def _csv_line(row):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([row.get(c, "") for c in INDEX_COLS])
    return buf.getvalue()


def record_finish(run):
    """If run is in a finish status, append an index row. Errors are swallowed.

    Only records that live under a normal .../YYYY/MM/<id> runs tree are indexed, so
    hermetic unit tests that use a bare temp folder never touch the real index.
    """
    try:
        if run.status not in FINISH_STATUSES:
            return False
        root = _runs_root_for(run.dir)
        if root is None:
            return False
        return append_row(row_from_run(run), os.path.join(root, "index.csv"))
    except Exception:
        return False


def _iter_record_dirs(base):
    """Yield directories under files/runs that contain a run.* record file."""
    if not os.path.isdir(base):
        return
    for year in sorted(os.listdir(base)):
        yp = os.path.join(base, year)
        if not (year.isdigit() and os.path.isdir(yp)):
            continue
        for month in sorted(os.listdir(yp)):
            mp = os.path.join(yp, month)
            if not (month.isdigit() and os.path.isdir(mp)):
                continue
            for rid in sorted(os.listdir(mp)):
                d = os.path.join(mp, rid)
                if not os.path.isdir(d):
                    continue
                if any(fn.startswith("run.") for fn in os.listdir(d)):
                    yield d


def _load_run_dir(directory):
    import mach_runs as mr
    for fn in sorted(os.listdir(directory)):
        if fn == "run.md" or (fn.startswith("run.") and fn.endswith(".md")):
            return mr.load_run(directory)
    return None


def _codex_runs_dir():
    return os.path.join(mc.home(), "na", "runs", "codex-runs")


def row_from_codex_file(path):
    """Build an index row from a legacy ~/na/runs/codex-runs/*.md or *.log file."""
    base = os.path.basename(path)
    stem, ext = os.path.splitext(base)
    text = mc.read_text(path)
    linear = _first_linear(text)
    model = ""
    m = re.search(r"(?im)^model:\s*(\S+)", text)
    if m:
        model = m.group(1).strip()
    date = ""
    try:
        date = _iso_local(datetime.datetime.fromtimestamp(os.path.getmtime(path)))
    except OSError:
        date = _iso_local()
    # Prefer .md as the canonical record when both exist; caller dedupes by run_id+status.
    link = path
    try:
        link = os.path.relpath(path, mc.mf_root()).replace(os.sep, "/")
        if link.startswith(".."):
            link = mc.tilde(path)
    except ValueError:
        link = mc.tilde(path)
    return {
        "run_id": stem,
        "date": date,
        "machine": "",
        "tool": "codex",
        "model": model,
        "repo": "",
        "linear_issue": linear,
        "requester": "owner",
        "status": "unknown",
        "duration": "",
        "link": link,
    }


def collect_reindex_rows(cfg=None):
    """Return list of row dicts that would be appended (missing from the index)."""
    path = index_path(cfg)
    existing = set()
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8", newline="") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
            try:
                existing = _existing_keys(fh)
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    rows = []
    seen = set(existing)

    for d in _iter_record_dirs(mc.runs_dir(cfg)):
        run = _load_run_dir(d)
        if run is None or run.status not in FINISH_STATUSES:
            continue
        row = row_from_run(run)
        key = (row["run_id"], row["status"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)

    codex = _codex_runs_dir()
    if os.path.isdir(codex):
        # Prefer .md over .log for the same stem.
        by_stem = {}
        for fn in sorted(os.listdir(codex)):
            if not (fn.endswith(".md") or fn.endswith(".log")):
                continue
            stem, ext = os.path.splitext(fn)
            p = os.path.join(codex, fn)
            if not os.path.isfile(p):
                continue
            prev = by_stem.get(stem)
            if prev is None or (ext == ".md" and prev[1] != ".md"):
                by_stem[stem] = (p, ext)
        for stem, (p, _ext) in sorted(by_stem.items()):
            row = row_from_codex_file(p)
            # If a .log had the model and we picked .md, merge model from .log when empty.
            log_p = os.path.join(codex, stem + ".log")
            if not row["model"] and os.path.isfile(log_p) and p != log_p:
                extra = row_from_codex_file(log_p)
                row["model"] = extra["model"]
            key = (row["run_id"], row["status"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)

    return rows


def reindex(apply=False, cfg=None):
    """Preview or append missing index rows. Never moves or deletes sources."""
    rows = collect_reindex_rows(cfg)
    result = {
        "would_append": len(rows),
        "appended": 0,
        "skipped": 0,
        "rows": rows,
        "index": mc.tilde(index_path(cfg)),
    }
    if not apply:
        return result
    for row in rows:
        if append_row(row, index_path(cfg)):
            result["appended"] += 1
        else:
            result["skipped"] += 1
    return result
