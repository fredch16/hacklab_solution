"""Command-line client for the control service (control_server.py). Standard library only.

    python ctl.py status
    python ctl.py views
    python ctl.py go top             # returns at once with a motion_id
    python ctl.py go top --wait      # follows the events until the move ends
    python ctl.py tour 2             # every view, twice, waiting for each
    python ctl.py stop
    python ctl.py watch              # live events (same as: curl -N localhost:8100/events)
    python ctl.py still -o still.jpg
    python ctl.py health

Exit code 0 = ok, 1 = the service refused or the move did not reach its view.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8100"
FINAL = ("reached", "stalled", "timeout", "stopped", "error")


class CtlError(Exception):
    pass


def request(url: str, method: str = "GET", data: dict | None = None, timeout: float = 10):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Content-Type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, r.headers, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            err = json.loads(raw)
            raise CtlError(f"{e.code} {err.get('error')}: {err.get('detail')}")
        except (json.JSONDecodeError, AttributeError):
            raise CtlError(f"{e.code}: {raw[:200]!r}")
    except urllib.error.URLError as e:
        raise CtlError(f"control service not reachable at {url} ({e.reason}); start control_server.py")


def get_json(base: str, path: str):
    return json.loads(request(base + path)[2])


def post_json(base: str, path: str, data: dict | None = None):
    return json.loads(request(base + path, "POST", data if data is not None else {})[2])


def events(base: str, timeout: float | None = None):
    """Yield events from GET /events (SSE)."""
    try:
        r = urllib.request.urlopen(base + "/events", timeout=timeout or 60)
    except urllib.error.URLError as e:
        raise CtlError(f"control service not reachable at {base} ({getattr(e, 'reason', e)})")
    with r:
        for raw in r:
            line = raw.decode().rstrip("\n")
            if line.startswith("data: "):
                yield json.loads(line[6:])


def fmt_event(ev: dict) -> str:
    t = time.strftime("%H:%M:%S", time.localtime(ev.get("t", time.time())))
    if ev.get("kind") == "status":
        m = ev.get("motion") or {}
        near = ev.get("nearest_view") or {}
        return (f"{t}  status   state={ev.get('state')}  last={m.get('motion_id', '-')}:{m.get('state', '-')}"
                f"  nearest={near.get('view', '?')} ({near.get('distance', '?')} steps)")
    bar = ""
    if ev.get("state") == "moving":
        n = int(ev.get("progress", 0) * 20)
        bar = "  [" + "#" * n + "." * (20 - n) + f"] {ev.get('progress', 0) * 100:3.0f}%"
    err = f"  {ev['error']}" if ev.get("error") else ""
    return f"{t}  {ev.get('motion_id', ''):6} {ev.get('view', ''):10} {ev.get('state', '')}{bar}{err}"


def wait_for(base: str, motion_id: str, timeout: float = 60) -> dict:
    """Follow the events until motion_id ends; returns the final motion."""
    deadline = time.monotonic() + timeout
    for ev in events(base, timeout=timeout):
        if ev.get("kind") == "status":
            m = ev.get("motion") or {}
            if m.get("motion_id") == motion_id and m.get("state") in FINAL:
                return m        # it ended before we subscribed
            continue
        if ev.get("motion_id") != motion_id:
            continue
        print(fmt_event(ev))
        if ev.get("state") in FINAL:
            return get_json(base, f"/motions/{motion_id}")
        if time.monotonic() > deadline:
            break
    raise CtlError(f"no end of {motion_id} within {timeout:.0f}s")


def go(base: str, view: str, wait: bool) -> bool:
    m = post_json(base, "/motions", {"view": view})
    print(f"accepted {m['motion_id']} -> {view} (path {' -> '.join(m['path'])})")
    if not wait:
        return True
    final = wait_for(base, m["motion_id"])
    print(f"{final['state']}  {final['view']}  {final['duration_s']}s" + (f"  {final['error']}" if final.get("error") else ""))
    return final["state"] == "reached"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=DEFAULT_URL)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("health")
    sub.add_parser("views")
    g = sub.add_parser("go"); g.add_argument("view"); g.add_argument("--wait", action="store_true")
    t = sub.add_parser("tour"); t.add_argument("rounds", type=int, nargs="?", default=1)
    t.add_argument("--pause", type=float, default=1.0)
    sub.add_parser("stop")
    sub.add_parser("watch")
    s = sub.add_parser("still"); s.add_argument("-o", "--out", default="still.jpg")
    a = p.parse_args(argv)
    base = a.url.rstrip("/")
    try:
        if a.cmd in ("status", "health"):
            print(json.dumps(get_json(base, "/" + a.cmd), indent=2))
        elif a.cmd == "views":
            for v in get_json(base, "/views")["views"]:
                via = f"  (via {', '.join(v['via'])})" if v["via"] else ""
                print(f"{v['name']:10} {v['description']}{via}")
        elif a.cmd == "go":
            return 0 if go(base, a.view, a.wait) else 1
        elif a.cmd == "tour":
            names = [v["name"] for v in get_json(base, "/views")["views"]]
            stops = [n for n in names if n != "home"]
            plan = (["home"] if "home" in names else []) + (stops + (["home"] if "home" in names else [])) * a.rounds
            for name in plan:
                if not go(base, name, wait=True):
                    return 1
                time.sleep(a.pause)
        elif a.cmd == "stop":
            st = post_json(base, "/stop")
            m = st.get("motion") or {}
            print(f"stopped: state={st['state']}  last={m.get('motion_id', '-')}:{m.get('state', '-')}")
        elif a.cmd == "watch":
            for ev in events(base, timeout=3600):
                print(fmt_event(ev), flush=True)
        elif a.cmd == "still":
            _, headers, jpeg = request(base + "/still.jpg")
            with open(a.out, "wb") as f:
                f.write(jpeg)
            print(f"saved {a.out} ({len(jpeg)} bytes)  nearest view: {headers.get('X-Nearest-View') or '?'}")
            print(f"joints: {headers.get('X-Joints')}")
    except CtlError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
