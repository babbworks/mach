#!/usr/bin/env python3
"""Local, unsynced registry for independent machine-flows workspaces."""
import json
import os


def registry_path():
    override = os.environ.get("MACH_WORKSPACES_FILE")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(config, "mach", "workspaces.json")


def load(path=None):
    path = path or registry_path()
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return {"version": 1, "active": None, "workspaces": []}
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("workspaces"), list):
        raise ValueError("unsupported or invalid workspace registry: %s" % path)
    names = set()
    for item in data["workspaces"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("path"), str) or not os.path.isabs(item["path"]):
            raise ValueError("workspace entries need a name and absolute path: %s" % path)
        if item["name"] in names:
            raise ValueError("duplicate workspace name in registry: %s" % item["name"])
        names.add(item["name"])
    if data.get("active") is not None and data.get("active") not in names:
        raise ValueError("active workspace is missing from registry: %s" % path)
    return data


def selected_root():
    try:
        data = load()
        cwd = os.path.realpath(os.getcwd())
        for item in data["workspaces"]:
            root = item.get("path")
            if isinstance(root, str):
                root = os.path.realpath(root)
                if cwd == root or cwd.startswith(root + os.sep):
                    return root
        active = data.get("active")
        for item in data["workspaces"]:
            if item.get("name") == active and isinstance(item.get("path"), str):
                return os.path.realpath(item["path"])
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return ""


if __name__ == "__main__" and len(os.sys.argv) == 2 and os.sys.argv[1] == "root":
    print(selected_root())
