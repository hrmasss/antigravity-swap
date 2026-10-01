"""A stand-in for the agy binary, driven by a JSON plan file.

FAKE_AGY_PLAN points at {"<email>": "ok" | "quota" | "dead" | "verify"}. Every call is
appended to FAKE_AGY_LOG as one JSON line.
"""

import base64
import json
import os
import sys
import time
import uuid
from pathlib import Path


def main(argv):
    gdir = Path.home() / ".gemini"
    rest = []
    for a in argv:
        if a.startswith("--gemini_dir="):
            gdir = Path(a.split("=", 1)[1])
        else:
            rest.append(a)
    if os.environ.get("ASWAP_GEMINI_DIR") and not any(a.startswith("--gemini_dir=") for a in argv):
        gdir = Path(os.environ["ASWAP_GEMINI_DIR"])
    tok_path = gdir / "antigravity-cli" / "antigravity-oauth-token"
    plan = json.loads(Path(os.environ["FAKE_AGY_PLAN"]).read_text())
    try:
        cred = json.loads(tok_path.read_text())
        payload = cred["id_token"].split(".")[1]
        payload += "=" * (-len(payload) % 4)
        email = json.loads(base64.urlsafe_b64decode(payload))["email"]
    except Exception:
        cred, email = None, None
    behaviour = plan.get(email or "", "dead")
    with open(os.environ["FAKE_AGY_LOG"], "a") as fh:
        fh.write(json.dumps({"email": email, "args": rest, "gdir": str(gdir)}) + "\n")

    if rest and rest[0] == "models":
        if behaviour == "dead" or cred is None:
            print("Fetching available models...")
            print("Error: Please sign in to view available models.", file=sys.stderr)
            return 1
        if behaviour == "verify":
            print("Error: Verify your account to continue.", file=sys.stderr)
            return 1
        cred["token"]["expiry"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        cred["token"]["access_token"] = "renewed-" + email
        tok_path.write_text(json.dumps(cred))
        print("gemini-3.8-flash-high\tGemini 3.8 Flash (High)")
        return 0

    if behaviour == "dead":
        print("Authentication required. Please visit the URL to log in:", file=sys.stderr)
        time.sleep(30)  # agy waits for a pasted code; aswap must kill it
        return 1
    conv = None
    if "--conversation" in rest:
        conv = rest[rest.index("--conversation") + 1]
        if not (gdir / "antigravity-cli" / "conversations" / f"{conv}.db").is_file():
            print(json.dumps({"status": "ERROR", "response": "conversation not found"}))
            return 1
    conv = conv or str(uuid.uuid4())
    cdir = gdir / "antigravity-cli" / "conversations"
    cdir.mkdir(parents=True, exist_ok=True)
    db = cdir / f"{conv}.db"
    with open(db, "a") as fh:
        fh.write(f"turn by {email}\n")
    if behaviour == "quota":
        print("Error: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your "
              "subscription to increase your limits. Resets in 1h2m3s.", file=sys.stderr)
        print(json.dumps({"conversation_id": conv, "status": "ERROR", "response": ""}))
        return 1
    turns = db.read_text().count("turn by")
    print(json.dumps({"conversation_id": conv, "status": "SUCCESS",
                      "response": f"done by {email} after {turns} turn(s)\n"}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
