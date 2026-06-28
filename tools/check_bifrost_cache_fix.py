#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Watch for a published Bifrost image that contains the MCP tool-ordering cache fix.

Background: PR #4588 ("deterministic MCP tool ordering for prompt cache stability")
merged to `dev` at commit 1cdd311 on 2026-06-21 14:49 UTC, AFTER the v1.5.16 image
(and therefore `:latest`) was cut at 13:10. Until a newer image is published, pulling
`maximhq/bifrost:latest` does NOT get the fix.

This script answers one question: "Is there now a published image I can pull that
contains commit 1cdd311?" It does so without trusting timestamps:
  1. Find the newest `transports/v*` release tag on GitHub (that's what `:latest` tracks).
  2. Ask GitHub's compare API whether the fix commit is contained in that tag.
  3. Report the current Docker Hub `:latest` digest and our running digest.

Exit code 0 = fix is available to pull; 10 = not yet; 1 = error.

Usage:
  ./check_bifrost_cache_fix.py             # one-shot check
  ./check_bifrost_cache_fix.py --watch     # loop until the fix is published, then
                                           # print a loud banner and exit (leave in a tab)
  ./check_bifrost_cache_fix.py --watch 1800  # custom poll interval in seconds (default 3600)
"""
import json
import sys
import time
import urllib.request
import urllib.error

FIX_COMMIT = "1cdd311fdfb3017e3802b3f477c69261bc5dc971"
REPO = "maximhq/bifrost"
PIN_FILE_NOTE = "config in docker-compose.yml: image: maximhq/bifrost:latest"


def gh(path):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/{path}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "bifrost-watch"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def hub_latest():
    req = urllib.request.Request(
        f"https://hub.docker.com/v2/repositories/{REPO}/tags/latest",
        headers={"User-Agent": "bifrost-watch"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    return d.get("digest"), d.get("last_updated")


def newest_transports_tag():
    tags = gh("tags?per_page=100")
    trans = [t["name"] for t in tags if t["name"].startswith("transports/v")]

    def key(name):
        nums = name.split("/v", 1)[1].split(".")
        return tuple(int(x) for x in nums if x.isdigit())

    trans.sort(key=key, reverse=True)
    return trans[0] if trans else None


def main():
    try:
        tag = newest_transports_tag()
        if not tag:
            print("ERROR: no transports/v* tags found", file=sys.stderr)
            return 1
        cmp = gh(f"compare/{tag}...{FIX_COMMIT}")
        status = cmp.get("status")  # "behind"/"identical" => fix is in the tag
        contained = status in ("behind", "identical")
        digest, updated = hub_latest()

        print(f"newest release tag : {tag}")
        print(f"fix commit         : {FIX_COMMIT[:12]} (PR #4588)")
        print(f"compare status     : {tag}...fix = {status} "
              f"(ahead_by={cmp.get('ahead_by')}, behind_by={cmp.get('behind_by')})")
        print(f"docker :latest      : {digest}  (updated {updated})")
        print(f"running             : {PIN_FILE_NOTE}")
        print()
        if contained:
            print(f"✅ FIX AVAILABLE — {tag} contains the cache-ordering fix.")
            print("   Upgrade:  cd ~/hroot/devserver && docker pull maximhq/bifrost:latest "
                  "&& just up bifrost")
            print("   Then re-probe outbound tool order (should be stable + alphabetical).")
            return 0
        else:
            print(f"⏳ NOT YET — newest release {tag} predates the fix "
                  f"(fix is {cmp.get('ahead_by')} commits ahead). Keep waiting.")
            return 10
    except urllib.error.URLError as e:
        print(f"ERROR: network/API failure: {e}", file=sys.stderr)
        return 1


def watch(interval):
    """Poll until the fix is published; print a banner and return 0. Ctrl-C to stop."""
    print(f"watching maximhq/bifrost for the cache fix (PR #4588) — polling every "
          f"{interval}s. Ctrl-C to stop.\n")
    n = 0
    while True:
        n += 1
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"──── check #{n} @ {stamp} ────")
        code = main()
        if code == 0:
            print("\n" + "█" * 60)
            print("█  BIFROST CACHE FIX IS NOW PUBLISHED — TIME TO UPGRADE  █")
            print("█  cd ~/hroot/devserver && docker pull maximhq/bifrost:latest")
            print("█  && just up bifrost   (then re-probe MCP tool order)")
            print("█" * 60)
            sys.stdout.write("\a")  # terminal bell
            sys.stdout.flush()
            return 0
        print(f"(sleeping {interval}s)\n", flush=True)
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nstopped.")
            return 130


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--watch":
        ivl = int(args[1]) if len(args) > 1 and args[1].isdigit() else 3600
        sys.exit(watch(ivl))
    sys.exit(main())
