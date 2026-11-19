#!/usr/bin/env bash
# shellcheck disable=SC1091,SC2016,SC2034
# Bindings keyed by stable system IDs: lint, ID addressing, per-machine paths, early failure.
set -u
TEST_NAME=bindings
. "$(dirname "$0")/lib.sh"
new_sandbox
export MACH_SUBSYSTEMS_DIR="$MACH_MF_ROOT/subsystems" MACH_FORCE_MINITOML=1
mkdir -p "$MACH_SUBSYSTEMS_DIR/demo" "$MACH_SUBSYSTEMS_DIR/plain" "$MACH_SUBSYSTEMS_DIR/_template" "$HOME/checkout"
cp "$FIXTURES/subsystems/_template/subsystem.toml" "$MACH_SUBSYSTEMS_DIR/_template/subsystem.toml"
cp "$TEST_ROOT/config/subsystem.example.toml" "$MACH_SUBSYSTEMS_DIR/demo/subsystem.toml"
sed 's/id = "demo"/id = "plain"/' "$TEST_ROOT/config/subsystem.example.toml" > "$MACH_SUBSYSTEMS_DIR/plain/subsystem.toml"
write_bindings() {
  cat > "$MACH_SUBSYSTEMS_DIR/demo/bindings.toml" <<TOML
schema = 1
assembly_remote = "example-org/assembly-system"

[assembly_root]
linux-box = "~/assembly"

[repos.R-0001]
legacy = "example"
[repos.R-0001.paths]
linux-box = "$HOME/checkout"

[units.U-0001]
legacy = "alpha"
[units.U-0001.roles]
lead = "grokbot:Example"
since = "2026-10-08"
changed = []
assignments = []
drafting = "claude"
review = "user"
notes = "The user reviews before anything ships."

[units.U-0002]
legacy = "beta"
[units.U-0002.roles]
lead = "user"
since = "2026-10-08"
changed = []
assignments = []
research = "codex"

[units.U-0003]
legacy = "gamma"
todo = "choose a lead"
[units.U-0003.roles]
lead = "user"
since = "2026-10-08"
changed = []
assignments = []
TOML
}
write_bindings

# lint covers the new file
run subsystem lint demo; assert_rc 0 "good bindings lint clean"; assert_not_contains FAIL "no FAIL"; assert_not_contains WARN "no WARN"
cp "$MACH_SUBSYSTEMS_DIR/demo/bindings.toml" "$SANDBOX/bindings.good"
awk '{print} /^legacy = "alpha"$/ {print "folder = \"alpha\""}' "$SANDBOX/bindings.good" > "$MACH_SUBSYSTEMS_DIR/demo/bindings.toml"
run subsystem lint demo; assert_rc 2 "a company fact in bindings fails lint"; assert_contains 'company facts belong in the Assembly System profile' "says why"
cp "$SANDBOX/bindings.good" "$MACH_SUBSYSTEMS_DIR/demo/bindings.toml"
printf 'schema = = 1\n' > "$MACH_SUBSYSTEMS_DIR/demo/bindings.toml"
run subsystem lint demo; assert_rc 2 "unparseable bindings fail lint"; assert_contains bindings "names the file"
cp "$SANDBOX/bindings.good" "$MACH_SUBSYSTEMS_DIR/demo/bindings.toml"
run subsystem lint plain; assert_rc 0 "a subsystem without bindings still lints"

# listing shows the Assembly ID in JSON only
run demo --json; assert_rc 0 "tree json"; assert_json "tree json valid"; assert_contains '"assembly_id": "U-0001"' "alpha carries its Assembly ID"
run demo; assert_rc 0 "plain tree unchanged"; assert_not_contains 'U-0001' "plain output has no IDs"

# address a unit by Assembly ID; the working folder comes from the binding for THIS machine
run demo U-0001 --to codex --dry-run hello; assert_rc 0 "ID send dry-run"; assert_contains 'Working root: ~/checkout' "folder from the binding"; assert_not_contains '~/projects/example' "not the manifest target path"
run demo alpha --to codex --dry-run hello; assert_rc 0 "legacy key still works"; assert_contains 'Working root: ~/checkout' "legacy send also uses the bound folder"
run demo U-0001 --dry-run hello; assert_rc 0 "ID to a Grok Bot lead"; assert_contains '<redacted>' "router dry-run"

# early, plain failures for an ID-addressed send
run demo U-0099 --to codex --dry-run hello; assert_rc 2 "unknown ID"; assert_contains 'unknown unit' "says unknown unit"
MACH_HOST=mac-laptop run demo U-0001 --to codex --dry-run hello; assert_rc 2 "no checkout recorded for this machine"; assert_contains 'no checkout path' "says so"; assert_contains 'mac-laptop' "names the machine"
rmdir "$HOME/checkout"
run demo U-0001 --to codex --dry-run hello; assert_rc 2 "bound folder missing on this machine"; assert_contains 'does not exist' "says it does not exist"; assert_contains 'checkout' "names the folder"
run demo U-0001 --dry-run hello; assert_rc 0 "cloud destination does not need the folder"
run demo alpha --to codex --dry-run hello; assert_rc 0 "legacy key keeps today's behaviour"
run plain U-0001 --to codex --dry-run hello; assert_rc 2 "IDs need a bindings file"; assert_contains 'bindings.toml' "points at the file"
finish
