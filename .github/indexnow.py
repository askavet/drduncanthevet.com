#!/usr/bin/env python3
"""Tell the IndexNow search engines (Bing, Yandex, Seznam, Naver, Yep) which pages of the website changed.

site_build.py copies this file into the site repository as .github/indexnow.py and writes the workflow that runs it
(.github/workflows/indexnow.yml), so every push to main reports its new, changed and deleted pages without anyone
resubmitting them by hand. It also runs by hand:

  python3 indexnow_submit.py --site DIR --before SHA --after SHA   the pages that changed between two commits
  python3 indexnow_submit.py --site DIR --all                      every URL in sitemap.xml
  python3 indexnow_submit.py --site DIR --url URL [--url URL ...]  named pages
  --dry-run lists the URLs and submits nothing; --no-wait skips waiting for the live site

A page is an index.html: the home page and articles/<slug>/index.html. A deleted page is reported too, so the engine
drops it. The key is the <key>.txt file at the site root whose content is its own name (site_build.py creates it
once and keeps it); the domain comes from CNAME. In commit mode the script first waits until the live site serves
the new version of the changed pages and the key file (GitHub Pages deploys a minute or two after the push), so the
engine never fetches a stale page. Exit status: 0 when the engine accepted the list (200 or 202) or nothing
changed, 1 when it refused the list, 2 on a missing key, domain or commit.
"""
import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ENDPOINT = "https://api.indexnow.org/indexnow"  # shared by every IndexNow engine
KEY_RE = re.compile(r"^[A-Za-z0-9-]{8,128}$")
ZERO_SHA = "0" * 40
BATCH = 10000  # the protocol's limit per request
UA = "drduncanthevet-indexnow/1.0"
MEANING = {200: "OK, the URLs were submitted",
           202: "Accepted, the URLs were received and the key is being validated",
           400: "Bad request, the submission is malformed",
           403: "Forbidden, the key is not valid (the key file is missing or does not match)",
           422: "Unprocessable, a URL is not on this host or the key does not match",
           429: "Too many requests, the engine is throttling this host"}


def find_key(site):
    """The IndexNow key: a <key>.txt at the site root whose content is its own name, or None."""
    for name in sorted(os.listdir(site)):
        stem, ext = os.path.splitext(name)
        if ext == ".txt" and KEY_RE.match(stem):
            with open(os.path.join(site, name), encoding="utf-8") as f:
                if f.read().strip() == stem:
                    return stem
    return None


def page_url(path, base):
    """index.html is the home page, articles/<slug>/index.html an article; any other file is not a page."""
    if path == "index.html":
        return base + "/"
    if path.endswith("/index.html") and not path.startswith("."):
        return base + "/" + path[:-len("index.html")]
    return None


def git(site, *args, text=True):
    return subprocess.run(["git", "-C", site] + list(args), capture_output=True, text=text)


def resolvable(site, rev):
    return bool(rev) and rev != ZERO_SHA and git(site, "cat-file", "-e", rev + "^{commit}").returncode == 0


def changed_pages(site, before, after, base):
    """The pages added, modified or deleted between two commits, as {url, path, deleted}."""
    out = git(site, "diff", "--name-status", "--no-renames", before, after)
    if out.returncode != 0:
        raise SystemExit("git diff failed: " + out.stderr.strip())
    pages = []
    for line in out.stdout.splitlines():
        status, _, path = line.partition("\t")
        url = page_url(path, base)
        if url:
            pages.append({"url": url, "path": path, "deleted": status.startswith("D")})
    return pages


def sitemap_urls(site):
    with open(os.path.join(site, "sitemap.xml"), encoding="utf-8") as f:
        return [html.unescape(u.strip()) for u in re.findall(r"<loc>([^<]+)</loc>", f.read())]


def sha256_at(site, rev, path):
    out = git(site, "show", "%s:%s" % (rev, path), text=False)
    return hashlib.sha256(out.stdout).hexdigest() if out.returncode == 0 else None


def sentinels(pages, site, rev):
    """Up to three pages whose live state proves the deployment is out: first, middle and last."""
    picks = pages if len(pages) <= 3 else [pages[0], pages[len(pages) // 2], pages[-1]]
    checks = []
    for p in picks:
        check = {"url": p["url"], "deleted": p["deleted"]}
        if not p["deleted"]:
            check["sha256"] = sha256_at(site, rev, p["path"])
        checks.append(check)
    return checks


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (urllib.error.URLError, OSError):
        return None, b""


def is_live(check, bust):
    status, body = fetch(check["url"] + ("&" if "?" in check["url"] else "?") + "indexnow=" + bust)
    if check["deleted"]:
        return status == 404
    return status == 200 and hashlib.sha256(body).hexdigest() == check["sha256"]


def wait_live(checks, timeout, interval, bust):
    deadline = time.time() + timeout
    pending = list(checks)
    while True:
        pending = [c for c in pending if not is_live(c, bust)]
        if not pending:
            return True
        if time.time() >= deadline:
            print("not live after %d s: %s" % (timeout, ", ".join(c["url"] for c in pending)))
            return False
        time.sleep(interval)


def post(body):
    req = urllib.request.Request(ENDPOINT, data=body, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError) as e:
        print("network error: %s" % getattr(e, "reason", e))
        return None


def submit(host, key, urls):
    ok = True
    for i in range(0, len(urls), BATCH):
        body = json.dumps({"host": host, "key": key, "keyLocation": "https://%s/%s.txt" % (host, key),
                           "urlList": urls[i:i + BATCH]}).encode("utf-8")
        code = post(body)
        if code == 429:
            time.sleep(60)
            code = post(body)
        print("IndexNow %s: %s" % (code, MEANING.get(code, "unexpected response")))
        ok = ok and code in (200, 202)
    return ok


def main():
    ap = argparse.ArgumentParser(description="Submit the site's new, changed and deleted pages to IndexNow.")
    ap.add_argument("--site", default=".", help="the site folder (a clone of the site repository)")
    ap.add_argument("--before", help="the commit before the push")
    ap.add_argument("--after", default="HEAD", help="the commit after the push (default HEAD)")
    ap.add_argument("--all", action="store_true", help="submit every URL in sitemap.xml")
    ap.add_argument("--url", action="append", default=[], help="submit this URL (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="list the URLs, submit nothing")
    ap.add_argument("--no-wait", action="store_true", help="do not wait for the live site")
    ap.add_argument("--timeout", type=int, default=600, help="seconds to wait for the live site (default 600)")
    ap.add_argument("--interval", type=int, default=15, help="seconds between live checks (default 15)")
    a = ap.parse_args()

    key = find_key(a.site)
    if not key:
        print("no IndexNow key file (<key>.txt holding its own name) at the site root; site_build.py writes it")
        return 2
    try:
        with open(os.path.join(a.site, "CNAME"), encoding="utf-8") as f:
            host = f.read().strip()
    except OSError:
        print("no CNAME file at the site root")
        return 2
    base = "https://" + host

    checks, rev = [], None
    if a.url:
        urls, mode = a.url, "named"
    elif a.all:
        urls, mode = sitemap_urls(a.site), "sitemap"
    else:
        if not resolvable(a.site, a.after):
            print("unknown commit %s" % a.after)
            return 2
        before = a.before
        if not resolvable(a.site, before):
            parent = a.after + "~1"
            if resolvable(a.site, parent):
                print("commit %s is not available here; comparing with %s" % (before or "(none)", parent))
                before = parent
            else:
                print("no earlier commit to compare with; submitting every URL in sitemap.xml")
                before = None
        if before:
            pages = changed_pages(a.site, before, a.after, base)
            urls, mode, rev = [p["url"] for p in pages], "changed", a.after
            checks = sentinels(pages, a.site, a.after)
        else:
            urls, mode = sitemap_urls(a.site), "sitemap"

    foreign = [u for u in urls if not u.startswith(base + "/")]
    urls = list(dict.fromkeys(u for u in urls if u.startswith(base + "/")))  # IndexNow refuses a list with another host's URL
    for u in foreign:
        print("skipped (not on %s): %s" % (host, u))
    if not urls:
        print("no pages to submit (%s)" % mode)
        return 0
    print("%d URL(s) to submit (%s):" % (len(urls), mode))
    for u in urls[:50]:
        print("  " + u)
    if len(urls) > 50:
        print("  ... and %d more" % (len(urls) - 50))
    if a.dry_run:
        print("dry run: nothing submitted")
        return 0

    if not a.no_wait:
        key_rev = rev or "HEAD"
        key_sha = sha256_at(a.site, key_rev, key + ".txt")
        if key_sha is None:  # not committed (a hand run in a fresh build): compare with the file on disk
            with open(os.path.join(a.site, key + ".txt"), "rb") as f:
                key_sha = hashlib.sha256(f.read()).hexdigest()
        checks = [{"url": "%s/%s.txt" % (base, key), "deleted": False, "sha256": key_sha}] + checks
        bust = (rev or str(int(time.time())))[:12]
        print("waiting for the live site to serve this version ...")
        if wait_live(checks, a.timeout, a.interval, bust):
            print("live")
        else:
            print("submitting anyway; the engines fetch the pages later")
    return 0 if submit(host, key, urls) else 1


if __name__ == "__main__":
    sys.exit(main())
