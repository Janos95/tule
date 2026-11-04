"""tule admin: support access for admins (ADMIN_EMAILS) and the support account (an agent's key on a dev machine).

  tule admin ls                          every account's projects
  tule admin copy OWNER PROJECT          copy a project's newest autosave into your account, to debug it there
  tule admin support-key                 (admin) a new key for the support account; old keys stop working
  tule admin log                         (admin) what admins and support did
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
    print(f"\r\033[Ktule: copied into your project {r['copy']}: {r['files']} files, {size}" + (f", deleted {r['expires'][:10]}" if r.get("expires") else ""), file=sys.stderr)
    if missing:
        print(f"tule: {missing} pieces were missing from the original's storage; those files will be incomplete", file=sys.stderr)
    print(f"tule: next: {r['next']}, then tule build / tule screenshot as usual; viewer {r['viewer']}", file=sys.stderr)
    print(r["copy"])


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
    s.add_parser("support-key", help="(admin) a new key for the support account, for a dev machine").set_defaults(fn=cmd_support_key)
    sp = s.add_parser("log", help="(admin) what admins and the support account did")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(fn=cmd_log)
