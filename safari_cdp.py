#!/usr/bin/env python3
"""USB-only delivery to the iPhone Files app via Safari + Web Inspector CDP.

No Wi-Fi, no SIM, no Apple ID.  A local CDP server attaches to Safari over
usbmuxd.  A fake http://usbdl.local/page request is intercepted and fulfilled
with a tiny HTML page; each file's bytes are injected into that page as a JS
Blob with an <a download> link.  The link is clicked with a CDP mouse event,
which iOS Safari turns into a real download prompt.

Files are delivered ONE AT A TIME.  After each click the caller (the Bookloader
GUI) waits for the user to confirm the prompt on the iPhone before releasing
the Blob and preparing the next file.  This avoids the two failure modes of
sending everything at once: the modal iOS prompt blocking later clicks, and
several large audiobooks exhausting the phone's memory.

Usage:
    python3 safari_cdp.py FILE [FILE2 ...]

Requires: pymobiledevice3, websocket-client, and on the iPhone:
Settings > Safari > Advanced > Web Inspector = on.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:
    import websocket
except ImportError:
    sys.exit("Missing dependency: pip install websocket-client")

try:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.webinspector import WebinspectorService
except ImportError:
    sys.exit("Missing dependency: pip install pymobiledevice3")


CDP_PORT = 9222
PAGE_URL = "http://usbdl.local/page"
CHUNK_SIZE = 512 * 1024  # base64 chars per Runtime.evaluate message

MIME_BY_EXT = {
    ".m4a": "audio/mp4", ".m4b": "audio/mp4", ".m4p": "audio/mp4",
    ".aac": "audio/mp4", ".mp3": "audio/mpeg", ".mp4": "video/mp4",
    ".alac": "audio/mp4", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".opus": "audio/ogg", ".wav": "audio/wav", ".aiff": "audio/aiff",
    ".txt": "text/plain", ".pdf": "application/pdf",
}


def log(message: str) -> None:
    print(message, flush=True)


def kill_stale_cdp() -> None:
    """Remove a leftover CDP server from a previous interrupted run."""
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1
        )
    except Exception:
        return
    log("Removing stale CDP server from a previous run...")
    subprocess.run(
        ["pkill", "-f", "[p]ymobiledevice3 webinspector cdp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1)


def start_cdp_server() -> subprocess.Popen:
    kill_stale_cdp()
    cmd = [sys.executable, "-m", "pymobiledevice3", "webinspector", "cdp",
           "--host", "127.0.0.1", "--port", str(CDP_PORT)]
    log("Starting CDP server over USB...")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1)
            return proc
        except Exception:
            if proc.poll() is not None:
                sys.exit("CDP server failed to start")
            time.sleep(0.5)
    proc.terminate()
    sys.exit("CDP server did not become ready")


def open_safari() -> None:
    async def _open() -> None:
        lockdown = await create_using_usbmux()
        async with WebinspectorService(lockdown=lockdown) as inspector:
            await inspector.connect()
            await inspector.open_app("com.apple.mobilesafari", 5)
    asyncio.run(_open())


def page_target() -> dict:
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            targets = json.load(urllib.request.urlopen(
                f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=1))
            for target in targets:
                if target.get("type") == "page":
                    return target
        except Exception:
            pass
        time.sleep(0.5)
    sys.exit("No inspectable Safari page found (is Safari open and Web Inspector on?)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path, help="Files to send")
    parser.add_argument("--names", default=None,
                        help="JSON list of display filenames, one per input file, in order")
    parser.add_argument("--auto", action="store_true",
                        help="Advance automatically after a delay instead of waiting for GUI confirmation")
    parser.add_argument("--delay", type=int, default=8,
                        help="Seconds to wait between files in auto mode (default: 8)")
    args = parser.parse_args()

    files = [p for p in args.files if p.is_file()]
    if not files:
        sys.exit("No existing files given")

    display_names: list[str] = []
    if args.names:
        try:
            parsed = json.loads(args.names)
            if isinstance(parsed, list):
                display_names = [str(n) for n in parsed]
        except (ValueError, TypeError):
            pass
    while len(display_names) < len(files):
        display_names.append(files[len(display_names)].name)

    server = start_cdp_server()
    try:
        open_safari()
        target = page_target()
        log(f"Using Safari page {target['id']}")
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=5)

        next_id = [0]

        def send(method: str, params: dict | None = None) -> int:
            next_id[0] += 1
            ws.send(json.dumps({"id": next_id[0], "method": method, "params": params or {}}))
            return next_id[0]

        def handle_event(message: dict) -> None:
            if message.get("method") != "Fetch.requestPaused":
                return
            params = message["params"]
            url = params["request"]["url"]
            request_id = params["requestId"]
            if url.startswith(PAGE_URL):
                send("Fetch.fulfillRequest", {
                    "requestId": request_id,
                    "responseCode": 200,
                    "responseHeaders": [{"name": "Content-Type", "value": "text/html"}],
                    "body": base64.b64encode(html.encode()).decode(),
                })
            elif url.endswith("/favicon.ico"):
                send("Fetch.fulfillRequest", {
                    "requestId": request_id, "responseCode": 204,
                    "responseHeaders": [], "body": "",
                })
            else:
                send("Fetch.continueRequest", {"requestId": request_id})

        def evaluate_value(expression: str) -> str | None:
            request_id = send("Runtime.evaluate", {
                "expression": expression, "returnByValue": True,
            })
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    message = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                except Exception as exc:
                    log(f"WebSocket error while evaluating: {exc}")
                    return None
                if message.get("id") == request_id:
                    result = message.get("result", {})
                    if "exceptionDetails" in result:
                        log(f"JS exception: {result['exceptionDetails'].get('text')}")
                        return None
                    return result.get("result", {}).get("value")
                handle_event(message)
            return None

        def drain(seconds: float) -> None:
            deadline = time.time() + seconds
            while time.time() < deadline:
                try:
                    message = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                except Exception:
                    return
                handle_event(message)

        # A branded status page shown in Safari while delivery runs.  It gives
        # the user a clear "connection is up / app is working" signal and a
        # live progress readout that mirrors the desktop app.
        html = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bookloader</title>
<style>
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { margin: 0; padding: 0; min-height: 100%; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: linear-gradient(145deg, #0f2027 0%, #203a43 55%, #2c5364 100%);
  color: #f5f7fa; padding: 28px 20px; min-height: 100vh;
  display: flex; align-items: flex-start; justify-content: center;
}
.card {
  width: 100%; max-width: 460px; margin-top: 24px;
  background: rgba(255,255,255,0.07);
  border: 1px solid rgba(255,255,255,0.14);
  border-radius: 22px; padding: 28px 24px;
  box-shadow: 0 18px 50px rgba(0,0,0,0.35);
  -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px);
}
.brand { display: flex; align-items: center; gap: 14px; }
.logo {
  width: 52px; height: 52px; border-radius: 14px; flex: none;
  background: linear-gradient(135deg, #ff9a44, #fc6076);
  display: flex; align-items: center; justify-content: center;
  font-size: 28px; box-shadow: 0 6px 16px rgba(252,96,118,0.35);
}
h1 { margin: 0; font-size: 26px; letter-spacing: -0.4px; }
.subtitle { margin: 2px 0 0; font-size: 13px; opacity: 0.65; }
.badge {
  display: inline-flex; align-items: center; gap: 8px;
  margin-top: 22px; padding: 8px 14px; border-radius: 999px;
  background: rgba(48,209,88,0.15); border: 1px solid rgba(48,209,88,0.4);
  color: #7ef0a2; font-size: 12px; font-weight: 600;
  letter-spacing: 0.4px;
}
.dot {
  width: 9px; height: 9px; border-radius: 50%; background: #30d158;
  box-shadow: 0 0 0 0 rgba(48,209,88,0.6);
  animation: pulse 1.8s infinite;
}
@keyframes pulse {
  0% { box-shadow: 0 0 0 0 rgba(48,209,88,0.55); }
  70% { box-shadow: 0 0 0 12px rgba(48,209,88,0); }
  100% { box-shadow: 0 0 0 0 rgba(48,209,88,0); }
}
.progress-head {
  display: flex; justify-content: space-between; align-items: baseline;
  margin-top: 26px; font-size: 13px; opacity: 0.85;
}
.progress-head strong { font-size: 15px; opacity: 1; }
.bar-bg {
  height: 10px; margin-top: 9px; border-radius: 999px;
  background: rgba(255,255,255,0.14); overflow: hidden;
}
.bar {
  height: 100%; width: 0%; border-radius: 999px;
  background: linear-gradient(90deg, #30d158, #7ee8a5);
  transition: width 0.35s ease;
}
.status {
  margin-top: 20px; font-size: 14px; line-height: 1.45;
  min-height: 40px; opacity: 0.92;
}
#books { margin-top: 16px; }
.file {
  display: flex; align-items: center; gap: 10px;
  padding: 9px 12px; margin-top: 7px; border-radius: 11px;
  background: rgba(255,255,255,0.06); font-size: 12.5px;
  border: 1px solid transparent;
}
.file .name {
  flex: 1; min-width: 0; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}
.file .mark { opacity: 0.35; font-size: 14px; }
.file.active { border-color: rgba(255,255,255,0.3); background: rgba(255,255,255,0.11); }
.file.active .mark { opacity: 1; color: #ffd60a; }
.file.done .mark { opacity: 1; color: #30d158; }
.foot {
  margin-top: 24px; font-size: 11px; opacity: 0.5;
  text-align: center; line-height: 1.5;
}
</style></head><body>
<div class="card">
  <div class="brand">
    <div class="logo">📚</div>
    <div><h1>Bookloader</h1><p class="subtitle">USB delivery to your iPhone</p></div>
  </div>
  <div class="badge"><span class="dot"></span> USB CONNECTED · WEB INSPECTOR ACTIVE</div>
  <div class="progress-head">
    <span id="label">Preparing…</span><strong id="count">0 / 0</strong>
  </div>
  <div class="bar-bg"><div class="bar" id="bar"></div></div>
  <div class="status" id="status">Preparing files…</div>
  <div id="dlbox"></div>
  <div id="books"></div>
  <div class="foot">Confirm each download on the iPhone.<br>Files appear in Files → On My iPhone → Downloads.</div>
</div>
</body></html>"""

        def page_status(text: str) -> None:
            evaluate_value(
                "document.getElementById('status').textContent=" + json.dumps(text)
            )

        def page_progress(done: int, total: int, label: str) -> None:
            pct = int(done * 100 / total) if total else 0
            evaluate_value(
                "(() => {"
                f"document.getElementById('count').textContent={json.dumps(f'{done} / {total}')};"
                f"document.getElementById('bar').style.width='{pct}%';"
                f"document.getElementById('label').textContent={json.dumps(label)};"
                "return 1})()"
            )

        def page_mark(index: int, state: str) -> None:
            # state: "active" (yellow) or "done" (green)
            evaluate_value(
                "(() => {const e=document.getElementById('f" + str(index) + "');"
                f"if(e) e.className='file {state}';return 1}})()"
            )

        send("Page.enable")
        send("Network.enable")

        # Safari can be left on an error page from a previous run; navigating
        # away first makes request interception work reliably.
        log("Resetting Safari page...")
        send("Page.navigate", {"url": "about:blank"})
        time.sleep(1)

        send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
        unique_url = f"{PAGE_URL}?t={int(time.time())}"
        log(f"Loading delivery page: {unique_url}")
        send("Page.navigate", {"url": unique_url})

        page_ready = False
        deadline = time.time() + 30
        while time.time() < deadline and not page_ready:
            try:
                message = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as exc:
                log(f"WebSocket error: {exc}")
                break
            handle_event(message)
            if (message.get("method") == "Page.frameNavigated"
                    and message["params"].get("frame", {}).get("url", "").startswith(PAGE_URL)):
                page_ready = True

        if not page_ready:
            log("Page did not load in Safari")
            return 1

        log("Page loaded. Sending files one at a time.")

        total = len(files)

        # Show the full file list on the status page and initialise progress.
        import html as html_lib
        items = "".join(
            f'<div class="file" id="f{i}"><span class="name">'
            f'{html_lib.escape(display_names[i])}</span><span class="mark">●</span></div>'
            for i in range(total)
        )
        evaluate_value(
            "(() => {document.getElementById('books').innerHTML=" +
            json.dumps(items) + ";return 1})()"
        )
        page_progress(0, total, "Preparing files…")

        for index, path in enumerate(files):
            name = display_names[index]
            log(f"PROGRESS {index}/{total} preparing")
            page_mark(index, "active")
            page_status(f"Preparing {index + 1}/{total}: {name}")
            page_progress(index, total, "Preparing")

            # Clear the previous link/Blob so only one file is held in memory.
            evaluate_value("(() => {const d=document.getElementById('dlbox');"
                           "d.textContent='';return 1})()")

            data = path.read_bytes()
            encoded = base64.b64encode(data).decode()
            evaluate_value("window.__p=[]")
            for start in range(0, len(encoded), CHUNK_SIZE):
                chunk = encoded[start:start + CHUNK_SIZE]
                evaluate_value(f"window.__p.push({json.dumps(chunk)})")
                time.sleep(0.005)

            mime = MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")
            safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
            build = f"""(() => {{
const b64 = window.__p.join('');
const bin = atob(b64);
const bytes = new Uint8Array(bin.length);
for (let i=0;i<bin.length;i++) bytes[i]=bin.charCodeAt(i);
window.__url = URL.createObjectURL(new Blob([bytes], {{type:'{mime}'}}));
const a = document.createElement('a');
a.id = 'dl';
a.href = window.__url;
a.download = '{safe_name}';
a.textContent = '⬇  Download ' + {json.dumps(name)};
a.style = 'display:block;padding:16px;margin:14px 0;border-radius:12px;'
        + 'background:linear-gradient(135deg,#ff9a44,#fc6076);color:white;'
        + 'font-size:17px;font-weight:600;text-align:center;text-decoration:none;'
        + 'box-shadow:0 8px 20px rgba(252,96,118,0.3)';
document.getElementById('dlbox').appendChild(a);
window.__p = null;
return 'ready'; }})()"""
            if evaluate_value(build) is None:
                log(f"Could not prepare {name}")
                return 1
            log(f"PROGRESS {index}/{total} ready")
            log(f"Prepared: {name} ({len(data)} bytes)")

            # Scroll the link into view and click it with a supported CDP
            # mouse event (pymobiledevice3 synthesizes this in-page).
            evaluate_value("document.getElementById('dl').scrollIntoView({block:'center'})")
            time.sleep(0.3)  # let the scroll settle before reading coordinates
            position = evaluate_value(
                "(() => {const r=document.getElementById('dl').getBoundingClientRect();"
                "return JSON.stringify({x:r.x+r.width/2,y:r.y+r.height/2});})()"
            )
            if position:
                coords = json.loads(position)
                x, y = coords["x"], coords["y"]
            else:
                x, y = 200, 200
            send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
            send("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": x, "y": y,
                "button": "left", "clickCount": 1,
            })
            send("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": x, "y": y,
                "button": "left", "clickCount": 1,
            })
            log(f"Download requested: {name}")
            log(f"PROGRESS {index + 1}/{total} confirming")
            page_status(f"Confirm download {index + 1}/{total} on your iPhone.")
            page_progress(index + 1, total, "Awaiting confirmation")
            drain(0.5)

            # Wait for the prompt to be confirmed before releasing this Blob
            # and moving on.  In auto mode we just pause for a fixed delay.
            if args.auto:
                log(f"AUTO_WAIT {args.delay}s")
                time.sleep(args.delay)
            else:
                log("AWAIT_CONFIRM")
                if not sys.stdin.readline():  # stdin closed: fall back to a pause
                    time.sleep(2)

            page_mark(index, "done")
            drain(1.0)
            time.sleep(0.5)
            evaluate_value("(() => {if(window.__url){URL.revokeObjectURL(window.__url);"
                           "window.__url=null}return 1})()")
            evaluate_value("(() => {const d=document.getElementById('dlbox');"
                           "d.textContent='';return 1})()")
            drain(0.2)

        page_status("All files sent. Check Files → On My iPhone → Downloads.")
        page_progress(total, total, "Complete")
        log("PROGRESS_DONE")
        ws.close()
        log("Done. Check Files > On My iPhone > Downloads.")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
