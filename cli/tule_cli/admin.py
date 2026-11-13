"""tule admin: support access for admins (ADMIN_EMAILS) and the support account (an agent's key on a dev machine).

  tule admin ls                          every account's projects
  tule admin copy OWNER PROJECT          copy a project's newest autosave into your account, to debug it there
  tule admin pull PROJECT                a project of yours (or a copy) into a local dev server, to try changes on it
  tule admin browser CODE                (support) sign in the browser showing CODE at /auth/agent, for 12 hours
  tule admin support-key                 (admin) a new key for the support account; old keys stop working
  tule admin log                         (admin) what admins and support did
  tule admin stats                       (admin) the /admin dashboard's usage numbers (--json: all of them)
"""

import sys


def cmd_ls(args):
    from . import api

    ps = api("GET", "/api/admin/projects")["projects"]
    for p in ps:
        print(f"{p['owner'] or '?':<36} {p['name']:<32} {p['id']:<16} {p['created'][:10]}")
    if not ps:
        print("tule: no projects", file=sys.stderr)


def cmd_copy(args):
    from . import api

    if args.owner_or_id:
        body = {"owner": args.owner_or_id, "project": args.project}
    elif args.project.startswith("p_"):
        body = {"project": args.project}
    else:
        raise SystemExit("tule: give the owner's email and the project name (or the project's id from tule admin ls)")
    if args.machine:
        body["machine"] = args.machine
    if args.as_name:
        body["as"] = args.as_name
    r = api("POST", "/api/admin/copy", body)
    missing = r.get("missing", 0)
    while not r.get("done"):
        print(f"\rtule: copying {r['copy']}: {r['cursor']}/{r['total']} pieces", end="", file=sys.stderr, flush=True)
        r = api("POST", "/api/admin/copy", {"copy": r["copy"], "cursor": r["cursor"]})
        missing += r.get("missing", 0)
    size = f"{r['bytes'] / 1e6:.1f} MB" if r["bytes"] >= 1e5 else f"{r['bytes'] / 1e3:.0f} kB"
    print(f"\r\033[Ktule: copied into your project {r['copy']}: {r['files']} files, {size}" + (f", kept until {r['expires'][:10]}" if r.get("expires") else ""), file=sys.stderr)
    if missing:
        print(f"tule: {missing} pieces were missing from the original's storage; those files will be incomplete", file=sys.stderr)
    print(f"tule: next: {r['next']}, then tule build / tule screenshot as usual; viewer {r['viewer']}", file=sys.stderr)
    print(r["copy"])


def cmd_pull(args):
    """A project of yours (or your tule admin copy of someone's), its newest autosave, into a local dev server."""
    import json
    import urllib.error
    import urllib.parse
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor
    from . import CliError, api, load, need

    prod, token = need(load())
    local = args.to.rstrip("/")
    if local == prod:
        raise SystemExit(f"tule: --to is the server you are signed in to ({prod}); pull into a local dev server")
    name = urllib.parse.quote(args.project)

    def call(method, url, data=None, headers=None):
        req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": "tule-cli/0.1", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise CliError(f"{method} {url}: {e.read().decode(errors='replace')[:300]}") from None
        except urllib.error.URLError as e:
            raise CliError(f"cannot reach {url}: {e.reason} (is the dev server running? npm --prefix control run dev)") from None

    snaps = api("GET", f"/api/projects/{name}/snapshots?limit=1").get("snapshots") or []
    if not snaps:
        raise CliError(f"{args.project} has no autosave yet")
    sid = snaps[0]["id"]
    manifest = api("GET", f"/api/projects/{name}/snapshots/{sid}")
    files = list(manifest.get("files", {}).values())
    hashes = sorted({h for f in files for h in f.get("h", [])})
    made = json.loads(call("POST", f"{local}/__dev/import", json.dumps({"name": args.as_name or args.project, "manifest": manifest}).encode(),
                           {"Content-Type": "application/json"}))

    def piece(h):
        data = call("GET", f"{prod}/api/projects/{name}/blobs/{h}", headers={"Authorization": f"Bearer {token}"})
        call("PUT", f"{local}/__dev/import/{made['id']}/{h}", data)

    with ThreadPoolExecutor(8) as pool:
        for n, _ in enumerate(pool.map(piece, hashes), 1):
            print(f"\rtule: pulling {args.project}: {n}/{len(hashes)} pieces", end="", file=sys.stderr, flush=True)
    size = sum(int(f.get("s", 0)) for f in files)
    call("POST", f"{local}/__dev/import/{made['id']}/done", json.dumps({"snapshot": sid, "at": snaps[0].get("at"), "files": len(files), "bytes": size}).encode(),
         {"Content-Type": "application/json"})
    print(f"\r\033[Ktule: pulled {args.project} ({len(files)} files, {size / 1e6:.1f} MB) into {made['name']} at {local}", file=sys.stderr)
    print(f"tule: open {made['viewer']} (as the dev user: {local}/auth/dev)", file=sys.stderr)
    print(made["name"])


def cmd_browser(args):
    from . import api, load

    r = api("POST", "/api/admin/browser", {"code": args.code})
    print(f"tule: the browser showing {r['approved']} is signed in as {r['signedIn']} for 12 hours; it opens your copies' viewers ({load().get('url', '')}/p/<copy>)", file=sys.stderr)


def cmd_support_key(args):
    from . import api

    r = api("POST", "/api/admin/support-key")
    print(r["key"])
    print(f"tule: the support account's key until {r['expires'][:10]} (the old one stops working). {r['use']}", file=sys.stderr)


def cmd_log(args):
    from . import api

    for e in api("GET", f"/api/admin/log?limit={args.limit}")["log"]:
        what = f"{e['owner']}/{e['project']}" if e["project"] else e["owner"]
        print(f"{e['at'][:19]}  {e['actor']:<28} {e['action']:<15} {what}  {e['detail']}")


def cmd_stats(args):
    import json

    from . import api

    m = api("GET", "/api/admin/stats")
    if args.json:
        print(json.dumps(m, indent=2))
        return
    p, a, g, pr, run, mo = m["people"], m["active"], m["guests"], m["projects"], m["running"], m["month"]
    pct = round(100 * p["agents"] / p["total"]) if p["total"] else 0
    num = lambda v: f"{v:g}"
    print(f"accounts        {p['total']} (+{p['week']} this week)")
    print(f"agent connected {p['agents']} ({pct} %, +{p['agentsWeek']} this week)")
    print(f"active          {a['day']} today, {a['week']} this week, {a['month']} in 30 days")
    print(f"guests          {g['week']} this week, {g['converted']} signed in, {g['now']} now")
    print(f"projects        {pr['total']} (+{pr['week']} this week)")
    print(f"running now     {run['accounts']} accounts, {run['guests']} guests")
    print(f"compute {mo['month']}  CPU {num(mo['cpuHours'])} h, GPU {num(mo['gpuHours'])} h (${mo['gpuCost']:.2f})")
    print(f"connected       {m['machines']['computers']} computers, {m['machines']['runpod']} RunPod, {m['machines']['shares']} shares")
    if f := m.get("funnel"):
        print(f"funnel 7 days   {f['shareViews']} link views > {f['shareClicks']} 'Make your own' > {f['guests']} guests"
              f" > {f['guestProjects']} guest projects; {f['agents']} agents connected, {f['signups']} sign-ups")
    since = f" (daily counts since {m['since']})" if m.get("since") else ""
    print(f"\nlast 7 days{since}")
    print(f"  {'day':<10} {'views':>5} {'clicks':>6} {'guests':>6} {'signups':>7} {'agents':>6} {'active':>6} {'cpu h':>6}")
    for d in m["days"][-7:]:
        print(f"  {d['day']:<10} {d.get('shareViews', 0):>5} {d.get('shareClicks', 0):>6} {d['guests']:>6} {d['signups']:>7} {d['agents']:>6} {d['active']:>6} {num(d['cpuHours']):>6}")
    if m["top"]:
        print("\nmost compute this month")
        for r in m["top"]:
            print(f"  {r['email']:<32} CPU {num(r['cpuHours']):>5} h  GPU {num(r['gpuHours']):>5} h  {r['projects']} projects  last {r['last'] or '-'}")
    print("\nlatest sign-ups")
    for r in m["recent"]:
        print(f"  {r['email']:<32} {r['created']}  agent {'yes' if r['agent'] else 'no ':<3}  {r['projects']} projects  last {r['last'] or '-'}")


def add_commands(sub):
    ap = sub.add_parser("admin", help="support access: every project, copies to debug (admins and the support account)")
    s = ap.add_subparsers(dest="admin_cmd", required=True)
    s.add_parser("ls", help="every account's projects: owner, name, id").set_defaults(fn=cmd_ls)
    sp = s.add_parser("copy", help="copy a project's newest autosave into your account (the original is not touched)")
    sp.add_argument("owner_or_id", nargs="?", help="the owner's email")
    sp.add_argument("project", help="the project's name (or its id, p_…, alone)")
    sp.add_argument("--machine", help="copy this machine's autosave instead of the newest")
    sp.add_argument("--as", dest="as_name", metavar="NAME", help="the copy's name (default <owner>-<project>)")
    sp.set_defaults(fn=cmd_copy)
    sp = s.add_parser("pull", help="a project of yours (or your copy of someone's) into a local dev server, to try changes on it")
    sp.add_argument("project")
    sp.add_argument("--as", dest="as_name", metavar="NAME", help="the local project's name (default the same)")
    sp.add_argument("--to", default="http://127.0.0.1:8787", help="the dev server (default http://127.0.0.1:8787)")
    sp.set_defaults(fn=cmd_pull)
    sp = s.add_parser("browser", help="(support) sign in a browser: open /auth/agent there, then pass the code it shows")
    sp.add_argument("code")
    sp.set_defaults(fn=cmd_browser)
    s.add_parser("support-key", help="(admin) a new key for the support account, for a dev machine").set_defaults(fn=cmd_support_key)
    sp = s.add_parser("log", help="(admin) what admins and the support account did")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(fn=cmd_log)
    sp = s.add_parser("stats", help="(admin) usage: accounts, agents, activity, guests, compute (the /admin dashboard)")
    sp.add_argument("--json", action="store_true", help="every number, as JSON")
    sp.set_defaults(fn=cmd_stats)
