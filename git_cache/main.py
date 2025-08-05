import os
import sys
import hashlib
import subprocess
import shutil
import configparser
import urllib.parse
import re
from pathlib import Path
from collections import namedtuple

_cache_dir = os.environ.get("GIT_CACHE_DIR")
if not _cache_dir:
    sys.exit("Error: GIT_CACHE_DIR environment variable is not set.")
cache_dir = Path(_cache_dir)

verbose = False

# ------------------------------------------------------------------------------

def run_git(args, cwd=None, check=True, capture_output=False) -> str:
    if verbose:
        print(f"[run-git] cwd = {cwd}, args = {args}")
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        check=check,
        capture_output=capture_output,
        text=True
    )
    return result.stdout.strip() if capture_output else ""

def hash_str(str):
    return hashlib.sha256(str.encode()).hexdigest()

def create_symlink(path: Path, target: Path, force: bool = False, quiet = False):
    if path.exists() or path.is_symlink():

        same_file = False
        try:
            same_file = path.samefile(target)
        except:
            print(f"[link] Error following existing path, attempt to replace...")
            pass

        if same_file:
            return
        else:
            if path.is_symlink():
                # print(f"[link] Removing existing symlink from {path} -> {path.resolve()}")
                path.unlink()
            else:
                if force:
                    if path.is_file():
                        # print(f"[link] Removing existing file")
                        path.unlink()
                    else:
                        # print(f"[link] Removing existing directory")
                        shutil.rmtree(path)
                else:
                    raise RuntimeError(f"[link] Target path {path} already exists and is not a symlink.")

    if not quiet:
        print(f"[link] Creating symlink from {path} -> {target}")
    os.makedirs(path.parent, exist_ok=True)
    path.symlink_to(target.resolve())

# ------------------------------------------------------------------------------

def get_metadata_repo(url: str) -> Path:
    metadata_repo = cache_dir / "metadata" / hash_str(url)

    if not metadata_repo.exists():
        print(f"[metadata] Cloning metadata repo for {url}")
        run_git(["clone", url, "--no-checkout", metadata_repo])
        # TODO: Mirror is excessively expensive (11 minutes for an llvm-project checkout) and we don't seem to need
        #       *everything* that a mirror provides
        # run_git(["clone", url, "--mirror", metadata_repo])

    return metadata_repo

def is_known_branch(metadata_repo: Path, ref: str) -> bool:
    try:
        branches = run_git(["branch", "-a"], cwd=metadata_repo, capture_output=True).splitlines()
        return any(line.strip()[len("remotes/origin/"):] == ref for line in branches)
    except subprocess.CalledProcessError:
        pass
    return False

def get_commit(metadata_repo: Path, ref: str, fetch: bool) -> str:
    def fetch_if_update_and_branch():
        if fetch and is_known_branch(metadata_repo, ref):
            print(f"[checkout] Fetching updated branch content: {ref}")
            run_git(["fetch", "origin", ref], cwd=metadata_repo)

    try:
        if len(ref) == 40:
            # Probably a SHA-256 hash
            try:
                run_git(["cat-file", "-e", f"{ref}^{{commit}}"], cwd=metadata_repo)
                commit = ref
            except:
                # Slow path for potential 40 character long branch or tag
                fetch_if_update_and_branch()
                commit = run_git(["rev-parse", ref], cwd=metadata_repo, capture_output=True)
                run_git(["cat-file", "-e", f"{commit}^{{commit}}"], cwd=metadata_repo)
        else:
            fetch_if_update_and_branch()
            commit = run_git(["rev-parse", ref], cwd=metadata_repo, capture_output=True)

    except subprocess.CalledProcessError:
        print(f"[metadata] Ref '{ref}' not found, attempting direct fetch")
        try:
            run_git(["fetch", "origin", ref], cwd=metadata_repo)
            commit = ref
        except subprocess.CalledProcessError:
            raise RuntimeError(f"Ref '{ref}' could not be resolved or fetched directly.")

    return commit

# ------------------------------------------------------------------------------

def load_gitmodules(path: Path) -> configparser.ConfigParser:
    normalized_gitmodules = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if len(line) > 0 and line[0].isspace():
                normalized_gitmodules.append("\t" + line.lstrip())
            else:
                normalized_gitmodules.append(line)

    config = configparser.ConfigParser()
    config.read_string("".join(normalized_gitmodules))
    return config

def resolve_relative_submodule_url(parent_url: str, relative: str) -> str:
    # Note: Normalize Git+SSH URLs to URL-like syntax
    # E.g.  git@github.com:org/repo.git -> ssh://git@github.com/org/repo.git
    ssh_match = re.match(r"(?P<user>[^@]+)@(?P<host>[^:]+):(?P<path>.+)", parent_url)
    if ssh_match:
        parent_url = f"ssh://{ssh_match.group("user")}@{ssh_match.group("host")}/{ssh_match.group("path")}"

    parsed = urllib.parse.urlparse(parent_url)

    base_path = Path(parsed.path).parent
    resolved_path = os.path.normpath(base_path / relative)
    resolved_url = f"{parsed.scheme}://{parsed.netloc}{resolved_path}"
    return resolved_url

Submodule = namedtuple('Submodule', ['name', 'path', 'url', 'hash'])

def get_submodules(parent_metadata_repo: Path, parent_repo: Path, commit: str) -> list[Submodule]:
    gitmodules_path = parent_repo / ".gitmodules"
    if not gitmodules_path.exists():
        return []

    gitmodules = load_gitmodules(gitmodules_path)
    submodules = []
    for section in gitmodules.sections():
        if section.startswith("submodule"):
            name = section.split('"')[1]
            path = gitmodules[section]["path"]
            url = gitmodules[section]["url"]
            submodules.append((name, path, url))

    tree_output = run_git(["ls-tree", "-r", commit], cwd=parent_repo, capture_output=True)
    path_to_sha = {}
    for line in tree_output.splitlines():
        parts = line.split()
        if parts[1] == "commit":
            path_to_sha[parts[3]] = parts[2]

    parent_url = None

    output: list[Submodule] = []

    for name, path, url in submodules:
        if path not in path_to_sha:
            raise RuntimeError(f"[submodule] No SHA found for submodule path {path}")
        sha = path_to_sha[path]

        if url.startswith("./") or url.startswith("../"):
            if url.startswith("../"):
                url = url[1:]
            if not parent_url:
                parent_url = run_git(["remote", "get-url", "origin"], cwd=parent_metadata_repo, capture_output=True)
                print(f"[submodules] Fetching parent repo URL: {parent_url}")
            url = resolve_relative_submodule_url(parent_url, url)

        output.append(Submodule(name, path, url, sha))

    return output

# ------------------------------------------------------------------------------

checkout_root_dir = cache_dir / "checkout"

def checkout(url: str, ref: str, fetch: bool) -> Path:

    maybe_checkout_repo = checkout_root_dir / hash_str(f"{url}@{ref}")
    if maybe_checkout_repo.exists():
        # ref was full hash and repo exists
        return maybe_checkout_repo

    metadata_repo = get_metadata_repo(url)
    commit = get_commit(metadata_repo, ref, fetch)

    checkout_repo = checkout_root_dir / hash_str(f"{url}@{commit}")
    if checkout_repo.exists():
        return checkout_repo

    print(f"[checkout] Checking out {ref} - {commit}")
    run_git(["clone", metadata_repo, f"--revision={commit}", checkout_repo])

    # TODO: Only need this for tooling that cares about origin repo
    # run_git(["remote", "set-url", "origin", url], cwd=checkout_repo)

    for sm in get_submodules(metadata_repo, checkout_repo, commit):
        submodule_repo = checkout(sm.url, sm.hash, fetch=False)
        create_symlink(checkout_repo / sm.path, submodule_repo, force=True)

    return checkout_repo

# ------------------------------------------------------------------------------

def make_view(src_root: Path, dst_root: Path):
    print(f"[view] {os.path.abspath(dst_root)}")

    src_root = src_root.resolve()
    dst_root = dst_root.resolve()
    os.makedirs(dst_root, exist_ok=True)

    create_symlink(dst_root / ".git", src_root / ".git", quiet=True)

    def is_checkout(p: Path) -> bool:
        return p.parent == checkout_root_dir

    def replicate(src: Path, dst: Path):
        if src.is_symlink():
            target = os.readlink(src)
            if os.path.isabs(target):
                real_target = Path(target)
                if real_target.is_dir() and is_checkout(real_target):
                    print(f"[view] {dst}")
                    # Explode the symlinked directory
                    dst.mkdir(exist_ok=True)
                    for child in real_target.iterdir():
                        replicate(child, dst / child.name)
                else:
                    # Copy the symlink as-is
                    create_symlink(dst, target, quiet=True)
            else:
                # Relative symlink - preserve
                create_symlink(dst, target, quiet=True)

        elif src.is_dir():
            dst.mkdir(exist_ok=True)
            for child in src.iterdir():
                replicate(child, dst / child.name)

        elif src.is_file():
            create_symlink(dst, src, quiet=True)

    for item in src_root.iterdir():
        if item.name == ".git":
            continue

        replicate(item, dst_root / item.name)

# ------------------------------------------------------------------------------

def list_urls():
    for metadata_repo in (cache_dir / "metadata").iterdir():
        url = run_git(["remote", "get-url", "origin"], cwd=metadata_repo, capture_output=True)
        print(url)

# ------------------------------------------------------------------------------

# TODO: metadata deduplication (for URL formatting variations)
# TODO: Accept descriptive short name to prefix SHA-256 folders with for debugging?
# TODO: Process for deleting stale checkouts
# TODO: The system is brittle to interruptions during checkout - it early-outs
#         on subsequent runs at the mere presence of a checkout folder
#         Add a flag that forces all checkouts to be re-checked completely?
# TODO: Use OverlayFS for copy-on-write views on Linux? (mount -t overlay overlay -o lowerdir=lower,upperdir=upper,workdir=work merged)

def run(url: str, ref: str, out_dir: Path, fetch: bool):
    checkout_dir = checkout(url, ref, fetch)
    create_symlink(out_dir, checkout_dir)
    # make_view(checkout_dir, out_dir)

def cli():
    global verbose
    import argparse

    # list_urls()

    parser = argparse.ArgumentParser(description="Upstream git caching layer")
    parser.add_argument("--url",     type=str,  required=True, help="Git repository URL")
    parser.add_argument("--ref",     type=str,  required=True, help="Commit hash, tag, or branch")
    parser.add_argument("--dir",     type=str,  required=True, help="Target directory")
    parser.add_argument("--fetch",   action="store_true",      help="Fetch branch content")
    parser.add_argument("--verbose", action="store_true",      help="Verbose output")
    args = parser.parse_args()

    if args.verbose:
        verbose = True

    run(args.url, args.ref, Path(args.dir), args.fetch)
