#!/usr/bin/env python3
"""
aiml-sync: an MCP server that mirrors a folder from your course repo
into its own standalone GitHub repo, and keeps it updated.

Tools exposed to Claude:
  register_project   - link a folder to a GitHub repo (creates the repo, or links an
                       existing one and updates it) and do the first push
  sync_project       - push the latest changes of one project (or all of them)
  preview_project    - show what a sync would change on GitHub, without pushing
  list_projects      - show what is being mirrored
  unregister_project - stop mirroring (does NOT delete the GitHub repo)

CLI (for git hooks / cron):
  python server.py --sync-all
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path(os.environ.get("AIML_SYNC_HOME", Path.home() / "aiml-sync"))
CONFIG = HOME / "projects.json"
MIRRORS = HOME / "mirrors"
MAX_FILE_MB = 95  # GitHub rejects files over 100 MB

SECRET_NAMES = {".env", "id_rsa", "id_ed25519", "credentials.json"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),                       # AWS access key
    re.compile(r"ghp_[A-Za-z0-9]{36}"),                    # GitHub token
    re.compile(r"github_pat_[A-Za-z0-9_]{50,}"),           # GitHub fine-grained token
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),                 # OpenAI/Anthropic style keys
    re.compile(r"hf_[A-Za-z0-9]{30,}"),                    # Hugging Face token
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),                 # Google API key
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]
FALLBACK_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".ipynb_checkpoints"}


class SyncError(Exception):
    pass


# --------------------------------------------------------------------------- helpers
def run(cmd, cwd=None):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        raise SyncError(f"'{cmd[0]}' is not installed or not on PATH.")
    if p.returncode != 0:
        raise SyncError(f"`{' '.join(cmd)}` failed:\n{(p.stderr or p.stdout).strip()}")
    return p.stdout


def load_config():
    if CONFIG.exists():
        return json.loads(CONFIG.read_text())
    return {}


def save_config(cfg):
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))


def sanitize(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "project"


def repo_key(text):
    """Registry name for a repo given as a name, owner/name or URL."""
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    return sanitize(re.split(r"[/:]", text)[-1])


def source_files(source: Path):
    """Files that belong in the mirror: tracked + untracked, minus anything .gitignored."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=source, capture_output=True, text=True, check=True,
        ).stdout
        rels = [r for r in out.split("\0") if r]
    except (subprocess.CalledProcessError, FileNotFoundError):
        rels = []
        for root, dirs, files in os.walk(source):
            dirs[:] = [d for d in dirs if d not in FALLBACK_SKIP_DIRS]
            for f in files:
                rels.append(str((Path(root) / f).relative_to(source)))
    return sorted({r for r in rels if (source / r).is_file()})


def safety_scan(source: Path, rels):
    """Return (ok_files, skipped_large, findings)."""
    ok, large, findings = [], [], []
    for rel in rels:
        p = source / rel
        if p.stat().st_size > MAX_FILE_MB * 1024 * 1024:
            large.append(rel)
            continue
        if p.name in SECRET_NAMES or p.suffix in SECRET_SUFFIXES:
            findings.append(f"{rel}: looks like a secrets file")
            continue
        if p.stat().st_size < 2 * 1024 * 1024:
            try:
                text = p.read_text(errors="ignore")
            except OSError:
                text = ""
            for pat in SECRET_PATTERNS:
                if pat.search(text):
                    findings.append(f"{rel}: matches a secret pattern ({pat.pattern[:18]}...)")
                    break
        ok.append(rel)
    return ok, large, findings


# --------------------------------------------------------------------------- git / GitHub plumbing
def remote_has_commits(remote):
    return bool(run(["git", "ls-remote", "--heads", remote]).strip())


def current_branch(mirror):
    return run(["git", "symbolic-ref", "--short", "HEAD"], cwd=mirror).strip()


def ensure_mirror(entry) -> Path:
    """Local working copy of the GitHub repo, always starting from what is on GitHub right now
    (so existing history and branch are kept, and edits made directly on GitHub don't block a push)."""
    mirror = Path(entry["mirror"])
    remote = entry["remote"]
    if not (mirror / ".git").exists():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", remote, str(mirror)])
        if not remote_has_commits(remote):
            run(["git", "checkout", "-B", "main"], cwd=mirror)  # brand-new empty repo
        return mirror

    run(["git", "fetch", "origin"], cwd=mirror)
    branch = current_branch(mirror)
    probe = subprocess.run(["git", "rev-parse", "--verify", "-q", f"origin/{branch}"],
                           cwd=mirror, capture_output=True, text=True)
    if probe.returncode == 0:
        run(["git", "reset", "-q", "--hard", f"origin/{branch}"], cwd=mirror)
    return mirror


def gh_login():
    return run(["gh", "api", "user", "--jq", ".login"]).strip()


def resolve_remote(repo, private):
    """Find the GitHub repo for `repo` (name, owner/name or URL); create it if it doesn't exist.
    Returns (clone_url, already_existed)."""
    if repo.startswith(("http://", "https://", "git@", "ssh://")):
        return repo, True
    full = repo if "/" in repo else f"{gh_login()}/{repo}"
    try:
        url = run(["gh", "repo", "view", full, "--json", "url", "--jq", ".url"]).strip()
        return (url if url.endswith(".git") else url + ".git"), True
    except SyncError as e:
        msg = str(e).lower()
        if "could not resolve" not in msg and "not found" not in msg and "404" not in msg:
            raise  # auth problem, network problem, etc. - don't guess
    out = run(["gh", "repo", "create", full, "--private" if private else "--public"])
    url = out.strip().splitlines()[-1].strip()
    return (url if url.endswith(".git") else url + ".git"), False


# --------------------------------------------------------------------------- sync
def sync_entry(name, entry, dry_run=False):
    source = Path(entry["source"])
    if not source.is_dir():
        raise SyncError(f"Source folder no longer exists: {source}")

    rels = source_files(source)
    ok, large, findings = safety_scan(source, rels)
    if findings:
        raise SyncError(
            "Sync blocked - possible secrets found (nothing was pushed):\n  - "
            + "\n  - ".join(findings)
            + "\nAdd these to the project's .gitignore or remove the secrets, then sync again."
        )

    mirror = ensure_mirror(entry)

    # make the mirror an exact copy of the source (minus .git)
    for child in mirror.iterdir():
        if child.name == ".git":
            continue
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    for rel in ok:
        dest = mirror / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, dest)

    run(["git", "add", "-A"], cwd=mirror)
    status = run(["git", "status", "--porcelain"], cwd=mirror).splitlines()
    notes = ""
    if large:
        notes = f"\nSkipped {len(large)} file(s) over {MAX_FILE_MB} MB: " + ", ".join(large[:5])
    if not status:
        return f"{name}: already up to date.{notes}"

    if dry_run:
        groups = {"A": [], "M": [], "D": [], "R": []}
        for s in status:
            groups.setdefault(s[0], []).append(s[3:])
        lines = [
            f"{name}: a sync would change {len(status)} file(s) on GitHub: "
            f"{len(groups['A'])} added, {len(groups['M'])} modified, "
            f"{len(groups['D'])} deleted, {len(groups['R'])} renamed."
        ]
        for label, code in (("Deleted from GitHub", "D"), ("Added", "A"), ("Modified", "M"), ("Renamed", "R")):
            files = groups.get(code, [])
            if files:
                more = f" ...and {len(files) - 8} more" if len(files) > 8 else ""
                lines.append(f"  {label}: " + ", ".join(files[:8]) + more)
        try:  # leave the mirror clean; the next real sync recopies everything anyway
            run(["git", "reset", "-q", "--hard"], cwd=mirror)
            run(["git", "clean", "-fdq"], cwd=mirror)
        except SyncError:
            pass
        return "\n".join(lines) + notes

    added = sum(1 for s in status if s.startswith("A"))
    modified = sum(1 for s in status if s.startswith("M"))
    deleted = sum(1 for s in status if s.startswith("D"))
    renamed = sum(1 for s in status if s.startswith("R"))
    changed = [s[3:].split(" -> ")[-1] for s in status[:5]]
    msg = f"Sync from AIML repo: +{added} ~{modified} -{deleted}" + (f" r{renamed}" if renamed else "")
    msg += "\n\n" + "\n".join(f"- {c}" for c in changed)
    if len(status) > 5:
        msg += f"\n- ...and {len(status) - 5} more"
    run(["git", "commit", "-m", msg], cwd=mirror)
    run(["git", "push", "-u", "origin", current_branch(mirror)], cwd=mirror)
    return f"{name}: pushed {len(status)} change(s) -> {entry['remote']}{notes}"


def find_entry(cfg, key):
    if key in cfg:
        return key
    try:
        resolved = str(Path(key).expanduser().resolve())
    except OSError:
        resolved = key
    for n, e in cfg.items():
        if e["source"] == resolved:
            return n
    raise SyncError(f"No registered project matches '{key}'. Use list_projects to see what exists.")


# --------------------------------------------------------------------------- operations
def do_register(project_path, repo_name=None, private=True, remote_url=None, confirm_overwrite=False):
    source = Path(project_path).expanduser().resolve()
    if not source.is_dir():
        raise SyncError(f"Not a folder: {source}")
    target = repo_name or source.name
    key = repo_key(target)
    cfg = load_config()
    if key in cfg:
        raise SyncError(f"'{key}' is already registered. Use sync_project instead.")

    if remote_url:
        url, existed = remote_url, True
    else:
        url, existed = resolve_remote(target, private)

    entry = {"source": str(source), "remote": url, "mirror": str(MIRRORS / key)}

    # An existing repo with content will be made an exact copy of the folder, which can delete
    # files that only exist on GitHub. Show that first and wait for a go-ahead.
    if existed and remote_has_commits(url) and not confirm_overwrite:
        preview = sync_entry(key, entry, dry_run=True)
        return (
            f"The GitHub repo already exists and has content: {url}\n"
            f"Nothing was pushed and nothing was saved yet. If you continue, the repo becomes an exact copy "
            f"of '{source.name}' (its existing history is kept; the update is added as a new commit).\n\n"
            f"{preview}\n\n"
            "To go ahead, call register_project again with confirm_overwrite=true."
        )

    cfg[key] = entry
    save_config(cfg)
    try:
        result = sync_entry(key, entry)
    except SyncError as e:
        return f"Registered '{key}' ({url}) but the first sync did not complete.\n{e}"
    if existed:
        return f"Linked the existing repo and updated it from '{source.name}'.\n{result}"
    vis = "private" if private else "public"
    return f"Created {vis} repo and mirrored '{source.name}'.\n{result}"


def _selected(cfg, name_or_path):
    return list(cfg) if not name_or_path else [find_entry(cfg, name_or_path)]


def do_sync(name_or_path=None):
    cfg = load_config()
    if not cfg:
        return "No projects registered yet."
    results = []
    for n in _selected(cfg, name_or_path):
        try:
            results.append(sync_entry(n, cfg[n]))
        except SyncError as e:
            results.append(f"{n}: FAILED - {e}")
    return "\n".join(results)


def do_preview(name_or_path=None):
    cfg = load_config()
    if not cfg:
        return "No projects registered yet."
    results = []
    for n in _selected(cfg, name_or_path):
        try:
            results.append(sync_entry(n, cfg[n], dry_run=True))
        except SyncError as e:
            results.append(f"{n}: FAILED - {e}")
    return "\n".join(results)


def do_list():
    cfg = load_config()
    if not cfg:
        return "No projects registered yet."
    return "\n".join(f"{n}\n  source: {e['source']}\n  remote: {e['remote']}" for n, e in cfg.items())


def do_unregister(name_or_path):
    cfg = load_config()
    n = find_entry(cfg, name_or_path)
    cfg.pop(n)
    save_config(cfg)
    return f"Stopped mirroring '{n}'. The GitHub repo and local mirror were left untouched."


# --------------------------------------------------------------------------- MCP wiring
def build_mcp():
    try:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x
    except ModuleNotFoundError:
        from mcp.server.mcpserver import MCPServer as FastMCP  # mcp 2.x (renamed)

    mcp = FastMCP("aiml-sync")

    def guarded(fn, *a, **k):
        try:
            return fn(*a, **k)
        except SyncError as e:
            return f"Error: {e}"

    @mcp.tool()
    def register_project(project_path: str, repo_name: str = "", private: bool = True,
                         confirm_overwrite: bool = False) -> str:
        """Mirror a project folder from the course repo to its own GitHub repo.
        If a GitHub repo with that name already exists it is linked and updated (its history is kept);
        otherwise a new repo is created. repo_name can be a name, owner/name or a GitHub URL and
        defaults to the folder name. New repos are private by default.
        For an existing repo that already has content, the first call only returns a preview of what
        would change (including files that would be deleted) and pushes nothing. Show that preview to
        the user and only call again with confirm_overwrite=true after they agree."""
        return guarded(do_register, project_path, repo_name or None, private, None, confirm_overwrite)

    @mcp.tool()
    def sync_project(name_or_path: str = "") -> str:
        """Push the latest changes of one registered project to its GitHub repo.
        Leave empty to sync every registered project."""
        return guarded(do_sync, name_or_path or None)

    @mcp.tool()
    def preview_project(name_or_path: str = "") -> str:
        """Show what a sync would change on GitHub (added, modified, deleted files) without pushing.
        Leave empty to preview every registered project."""
        return guarded(do_preview, name_or_path or None)

    @mcp.tool()
    def list_projects() -> str:
        """List all projects being mirrored, with their source folder and GitHub repo."""
        return guarded(do_list)

    @mcp.tool()
    def unregister_project(name_or_path: str) -> str:
        """Stop mirroring a project. Does not delete the GitHub repo."""
        return guarded(do_unregister, name_or_path)

    return mcp


if __name__ == "__main__":
    if "--sync-all" in sys.argv:
        print(do_sync())
    else:
        build_mcp().run()