#!/usr/bin/env bash
# shellcheck disable=SC2034,SC1091,SC2012,SC2015,SC2016,SC2086,SC2088,SC2221,SC2222  # test scripts: intentional shortcuts
# mach publish: tier selection, content scan, preview vs apply, never-list, remaps, stale removal, refusals.
TEST_NAME=publish
. "$(dirname "$0")/lib.sh"
new_sandbox

MF="$MACH_MF_ROOT"
TIERS="$MF/service-settings/tiers"
mkdir -p "$TIERS" "$MF/ai-authored/claude/machines" "$MF/plans" "$MF/files" "$MF/templates"
printf '# readme\n' > "$MF/README.md"
printf 'shared rules\n' > "$MF/ai-authored/AGENTS.md"
printf 'host facts under /home/someone/work\n' > "$MF/ai-authored/claude/machines/box.md"
printf 'see DEMO-123 and Example Corp planning\n' > "$MF/plans/01-a.md"
printf 'TOKEN=abcdefghijklmnopqrstuvwxyz0123\n' > "$MF/files/.env"
FAKE_TOKEN="ghp_$(printf 'abcdefghijklmnopqrstuvwx')"
printf 'token is %s here\n' "$FAKE_TOKEN" > "$MF/files/note.txt"
printf '# org readme\n' > "$MF/templates/org-readme.md"
ln -s "$MF/README.md" "$MF/link.md"

# --- clean public tier: preview writes nothing
cat > "$TIERS/public.manifest" <<'EOF'
# the generic layer
include README.md
include ai-authored/
exclude ai-authored/claude/machines/
scan paths tickets names
deny acme
EOF
run publish --tier public
assert_rc 0 "clean preview"
assert_contains "2 files" "two files selected (machines folder excluded)"
assert_contains "scan: clean" "scan clean"
assert_contains "preview only" "says nothing was written"
assert_no_file "$MACH_STAGING/publish/public" "preview does not create the export folder"

# --- apply writes the folder and state, no git
run publish --tier public --apply
assert_rc 0 "apply"
assert_file "$MACH_STAGING/publish/public/README.md" "readme exported"
assert_file "$MACH_STAGING/publish/public/ai-authored/AGENTS.md" "agents exported"
assert_no_file "$MACH_STAGING/publish/public/.git" "no git repository created"
assert_file "$MACH_STAGING/publish/public.state.json" "state recorded outside the export"
run publish --tier public --status
assert_contains "0 new, 0 changed, 2 unchanged" "second run sees no change"

# --- a changed source shows up; a source that leaves the tier is removed on the next apply
printf '# readme v2\n' > "$MF/README.md"
run publish --tier public --status
assert_contains "changed  README.md" "status lists the changed file"
rm "$MF/ai-authored/AGENTS.md"
run publish --tier public --apply
assert_rc 0 "apply after change"
assert_no_file "$MACH_STAGING/publish/public/ai-authored/AGENTS.md" "stale export removed"
assert_file_has "$MACH_STAGING/publish/public/README.md" "v2" "changed file updated"
printf 'shared rules\n' > "$MF/ai-authored/AGENTS.md"

# --- findings: machine path, ticket key, denied name; apply refuses and writes nothing
cat > "$TIERS/org.manifest" <<'EOF'
include ai-authored/
include plans/
scan paths tickets names
deny Acme
EOF
run publish --tier org
assert_rc 1 "preview with findings exits 1"
assert_contains "machine-path" "machine path found"
assert_contains "ticket-key" "ticket key found"
assert_contains "denied-name" "denied name found"
run publish --tier org --apply
assert_rc 1 "apply refused while findings are open"
assert_contains "refusing to apply" "refusal message"
assert_no_file "$MACH_STAGING/publish/org" "refused apply wrote nothing"

# --- allow suppresses one kind for a glob
cat >> "$TIERS/org.manifest" <<'EOF'
allow plans/* tickets
allow plans/* names
allow ai-authored/claude/machines/* paths
EOF
run publish --tier org --apply
assert_rc 0 "apply after allowances"
assert_file "$MACH_STAGING/publish/org/plans/01-a.md" "allowed file exported"

# --- secrets always scanned, values masked, never-list and symlinks excluded
cat > "$TIERS/leak.manifest" <<'EOF'
include files/
include link.md
EOF
run publish --tier leak
assert_rc 1 "secret found"
assert_contains "github-token" "token kind reported"
assert_contains "ghp***" "token masked"
assert_not_contains "$FAKE_TOKEN" "full token never printed"
run publish --tier leak --status
assert_contains "files/note.txt" "status lists the real file"
assert_not_contains "files/.env" "never-list file is not exported"
assert_not_contains "link.md" "symlink is skipped"

# --- placeholder files named like secrets are allowed (content is still scanned); the real thing is not
mkdir -p "$MF/examples"
printf 'api_key = "REPLACE_ME"\n' > "$MF/examples/secrets.toml.example"
printf 'api_key = "REPLACE_ME"\n' > "$MF/examples/secrets.toml"
printf 'include examples/\n' > "$TIERS/ex.manifest"
run publish --tier ex --status
assert_contains "examples/secrets.toml.example" "placeholder file exported"
if printf '%s\n' "$OUT" | grep -qE 'examples/secrets\.toml$'; then flunk "real secrets file was exported"; else pass; fi

# --- the scan fails closed: a secret hidden in a big or NUL-containing file is still found; what cannot be scanned is a finding
mkdir -p "$MF/blobs"
printf 'a\0b %s c\n' "$FAKE_TOKEN" > "$MF/blobs/nul.dat"
python3 - "$MF/blobs/big.txt" "$FAKE_TOKEN" <<'PYEOF'
import sys
with open(sys.argv[1], "w") as h:
    h.write("x" * 9000000 + "\n" + sys.argv[2] + "\n")
PYEOF
python3 - "$MF/blobs/huge.bin" <<'PYEOF'
import sys
with open(sys.argv[1], "wb") as h:
    h.write(b"\0" * 30000000)
PYEOF
printf 'include blobs/\n' > "$TIERS/blobs.manifest"
run publish --tier blobs
assert_rc 1 "hidden secrets and unscannable files are findings"
assert_contains "blobs/nul.dat" "secret next to a NUL byte is found"
assert_contains "unscanned" "a file too large to scan is reported, not skipped"
run publish --tier blobs --apply
assert_rc 1 "apply refused while a file is unscanned"
assert_no_file "$MACH_STAGING/publish/blobs" "nothing written"
printf 'allow blobs/huge.bin unscanned\nallow blobs/nul.dat secrets\nallow blobs/big.txt secrets\nallow blobs/big.txt unscanned\n' >> "$TIERS/blobs.manifest"
run publish --tier blobs
assert_rc 0 "explicit allowances make it clean"
rm -rf "$MF/blobs"

# --- remap exports a file under another path
cat > "$TIERS/mapped.manifest" <<'EOF'
include README.md
map templates/org-readme.md README.md
EOF
run publish --tier mapped --apply
assert_rc 0 "mapped apply"
assert_file_has "$MACH_STAGING/publish/mapped/README.md" "org readme" "mapped source wins at its destination"

# --- a features index drives the export (--root, --features, publish.rules beside it)
mkdir -p "$MF/proj/lib" "$MF/proj/na"
printf 'ok\n' > "$MF/proj/lib/a.py"
printf 'org only\n' > "$MF/proj/na/x.json"
printf 'held\n' > "$MF/proj/GUIDE.md"
printf 'feature,paths,tiers,status,note\ncore,lib/,org;public,ready,\nna,na/,org,org-only,\nguide,GUIDE.md,org;public,needs-edit,held\n' > "$MF/proj/FEATURES.csv"
printf 'scan names\ndeny orgword\n' > "$MF/proj/publish.rules"
run publish --tier public --root "$MF/proj" --features "$MF/proj/FEATURES.csv" --status
assert_rc 0 "features export"
assert_contains "lib/a.py" "ready row exported"
assert_not_contains "na/x.json" "org-only row held back"
assert_not_contains "GUIDE.md" "needs-edit row held back"
printf 'mentions orgword here\n' > "$MF/proj/lib/b.py"
run publish --tier public --root "$MF/proj" --features "$MF/proj/FEATURES.csv"
assert_rc 1 "rules file is applied"
assert_contains "denied-name" "denied word found via publish.rules"
printf 'feature,paths,tiers,status,note\nx,a,org,maybe,\n' > "$MF/proj/FEATURES.csv"
run publish --tier public --root "$MF/proj" --features "$MF/proj/FEATURES.csv"
assert_rc 2 "bad status in the index"

# --- JSON output
run --json publish --tier public
assert_json "json output"
assert_eq "2" "$(json_get 'd["summary"]["files"]')" "json file count"

# --- refusals
run publish --tier nosuch
assert_rc 2 "missing manifest"
assert_contains "no manifest for tier nosuch" "missing manifest message"
printf 'frobnicate x\n' > "$TIERS/bad.manifest"
run publish --tier bad
assert_rc 2 "bad manifest line"
assert_contains "line 1" "line number reported"
run publish --tier public --out "$MF/export"
assert_rc 2 "export inside the synced tree refused"
run publish
assert_rc 2 "tier required"
run publish --tier public --bogus
assert_rc 2 "unknown option"
finish
