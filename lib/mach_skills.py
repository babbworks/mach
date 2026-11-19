"""Skill-tree upkeep proposals for mach memory review (stdlib only)."""
import os
import time

import mach_common as mc

STALE_DAYS = 90


def skills_root():
    return os.path.join(mc.mf_root(), "ai-authored", "skills")


def skill_upkeep_findings(root=None):
    """Proposal-only findings for ai-authored/skills (missing from INDEX/skills.csv, stale)."""
    root = root or skills_root()
    out = []
    if not os.path.isdir(root):
        return out
    names = []
    for ent in sorted(os.listdir(root)):
        if ent in ("synced", "INDEX.md", "skills.csv", "README.md"):
            continue
        if ent.startswith("."):
            continue
        d = os.path.join(root, ent)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, "SKILL.md")):
            names.append(ent)
    index_path = os.path.join(root, "INDEX.md")
    index_text = mc.read_text(index_path) if os.path.isfile(index_path) else None
    csv_path = os.path.join(root, "skills.csv")
    csv_text = mc.read_text(csv_path) if os.path.isfile(csv_path) else None
    now = time.time()
    stale_after = STALE_DAYS * 86400
    if names and not os.path.isfile(index_path):
        out.append(("INFO", "skill upkeep: INDEX.md missing under %s (proposal: create one listing %d skill(s))" % (
            mc.tilde(root), len(names))))
    for name in names:
        skill_md = os.path.join(root, name, "SKILL.md")
        if index_text is None or name not in index_text:
            out.append(("INFO", "skill upkeep: %s missing from INDEX.md (proposal: add a line)" % name))
        if csv_text is not None and name not in csv_text:
            out.append(("INFO", "skill upkeep: %s missing from skills.csv (proposal: add a row)" % name))
        try:
            age = now - os.path.getmtime(skill_md)
        except OSError:
            age = 0
        if age > stale_after:
            days = int(age // 86400)
            out.append(("INFO", "skill upkeep: %s not updated in %d days (>%d; proposal: review or refresh)" % (
                name, days, STALE_DAYS)))
    return out
