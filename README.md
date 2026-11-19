# mach

`mach` is a portable command-line toolkit for people who work across computers, AI tools and project workspaces. It helps keep machine inventories and shared instructions current, organizes independent workspace folders, and routes work through explicit review gates.

The command is designed to sit **outside** machine-flows folders. Install it once, then use it with one or more folders you choose. The companion workspace examples and shared-flow patterns live in [`babbworks/machine-flows`](https://github.com/babbworks/machine-flows).

## What it does

- **Setup and workspaces:** register existing personal or group folders, select the active workspace, choose optional scans, and initialize blank tracking templates. Setup presents the complete action plan—including scripts, reads, writes, temporary destinations and network use—before asking to apply it. Syncthing and Tailscale pairing remain under your control.
- **Machine upkeep:** inspect system tools, applications, services, AI tools and workspace metadata; preview inventory changes before saving them.
- **AI workflow wiring:** manage shared instruction links, MCP definitions, tool names, service records and lessons.
- **Work gateway:** capture notes, prepare prompts, run reusable flows and route tasks to supported agents. Runs record their state and pause at approval gates before actions that can write or send.
- **Tracking sheets:** maintain CSV-first sheet sets and rebuild their Excel workbook when needed.

`mach` is intentionally local-first. Scans and reports stay on the machine unless you explicitly review and share a summary. It does not configure Syncthing or Tailscale, commit or push repositories, or transmit scan results automatically.

## Install

Requirements: Bash 3.2 or newer, Python 3.9 or newer, and Git. The runtime uses the Python standard library.

```bash
git clone https://github.com/babbworks/mach ~/github/babbworks/mach
bash ~/github/babbworks/mach/install.sh       # preview installation
bash ~/github/babbworks/mach/install.sh --apply
mach --version
```

The installer links `mach` into `~/.local/bin` by default. Set `MACH_BIN_DIR` to choose another command directory. The checkout and installed command should remain outside any synced machine-flows workspace.

## First setup

Start with the setup wizard. It defaults to `~/machine-flows` and can also register a group workspace or an existing folder. Review the full plan, then explicitly apply it:

```bash
mach setup
```

For a non-interactive preview, print the exact action plan as JSON:

```bash
mach setup --mode existing --name personal --path ~/machine-flows --json-plan
```

Setup can optionally scan machine resources, initialize blank tracking templates, and show Syncthing or Tailscale connection guidance. It never pairs devices or shares scan results. Use `mach workspace list` and `mach workspace use NAME --apply` to manage multiple independent workspaces.

## Common commands

```bash
mach help                         # command list and options
mach doctor                       # read-only health checks
mach scan catalog                 # scan categories available on this machine
mach scan machine --save-local    # save a private report on this machine
mach workspace list               # registered workspace folders
mach flow list                    # reusable multi-step flows
mach run brief.md --dry-run       # inspect a prompt and its planned route
mach sheets new ~/making/project/tracking --apply
```

See the generated [command reference](docs/COMMANDS.md) and [command table](docs/COMMANDS-TABLE.md) for full options.

## Safety and privacy

Setup and administrative changes show a preview before applying. Setup additionally shows its complete action plan before its confirmation prompt. Sensitive local configuration stays outside synced folders. Shared workspace files and local machine state are treated separately; choose what to sync, scan or share.

## Contributing

Issues and pull requests are welcome. Keep examples generic: do not include real machine names, credentials, private project identifiers or personal inventory data. Use synthetic fixtures for tests. Before proposing changes, review the relevant command help and generated documentation.

## License

MIT. See [LICENSE](LICENSE).
