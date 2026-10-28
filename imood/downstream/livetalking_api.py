"""Small helpers for the LiveTalking HTTP API: GET / POST JSON and finding the active WebRTC session."""
import json
import sys
import urllib.request


def lt_get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return json.loads(r.read())


def lt_post_json(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


HOW_TO_GET_SESSION = """
  No active LiveTalking WebRTC session for this avatar.
  1. Make sure LiveTalking is running in Ubuntu-24.04 with --avatar_id {avatar} (port 8010).
  2. Open http://localhost:8010/index.html in the browser and click the connect/start button;
     the avatar video must be playing.
  3. Re-run this command. The session is discovered automatically from GET {url}/api/admin/sessions.
     (Or pass it explicitly: --session <id>. It is shown on the page after connecting and in the
      LiveTalking log line 'offer sessionid=...'.)
"""


def discover_session(base, avatar_id, explicit=None):
    sessions = lt_get(base, "/api/admin/sessions")["data"]["sessions"]
    if explicit:
        if not any(s["sessionid"] == explicit for s in sessions):
            sys.exit(f"--session {explicit} is not an active LiveTalking session. Active: {[s['sessionid'] for s in sessions]}")
        return explicit, sessions
    mine = [s for s in sessions if s.get("avatar_id") == avatar_id]
    if not mine:
        sys.exit(HOW_TO_GET_SESSION.format(avatar=avatar_id, url=base))
    if len(mine) > 1:
        print(f"[warn] {len(mine)} active sessions for {avatar_id}; using the newest. All: "
              f"{[s['sessionid'] for s in mine]}  (use --session to choose)", flush=True)
    return mine[-1]["sessionid"], sessions        # dict insertion order -> last = newest
