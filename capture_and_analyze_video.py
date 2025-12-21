#!/usr/bin/env python3
"""
Single-file script to capture Chrome window content and analyze it with AI.
Designed to be dropped into any project with an .env file.

Requirements:
  pip install mss requests websocket-client python-dotenv

Usage:
  python capture_and_analyze_video.py local_file.html
  python capture_and_analyze_video.py https://example.com --duration 5
"""

import argparse
import asyncio
import base64
import json
import mimetypes
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

# --- Dependency Checks ---
try:
    from mss import mss, tools
except ImportError:
    print("ERROR: mss is required. Install with: pip install mss")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("ERROR: requests is required. Install with: pip install requests")
    sys.exit(1)

try:
    import websocket
except ImportError:
    print("ERROR: websocket-client is required. Install with: pip install websocket-client")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass # If dotenv is not installed, assume env vars are set manually

# --- Image Utils ---

def encode_image_to_data_url(data: Union[str, Path], mime: Optional[str] = None) -> str:
    """Encode an image file path to a base64 data URL."""
    path = Path(str(data))
    if not path.exists():
        raise ValueError(f"Image file does not exist: {path}")
    raw = path.read_bytes()
    if mime is None:
        mime, _ = mimetypes.guess_type(str(path))
        mime = mime or "image/png"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"

# --- OpenRouter Client ---

class OpenRouterError(RuntimeError):
    pass

class OpenRouterClient:
    def __init__(self, api_key: Optional[str] = None, endpoint: str = "https://openrouter.ai/api/v1/chat/completions", session: Optional[requests.Session] = None):
        key = api_key or os.getenv("OPENROUTER_API_KEY")
        if not key:
            raise OpenRouterError("OPENROUTER_API_KEY environment variable must be set.")
        self._api_key = key
        
        base_url = os.getenv("OPENROUTER_BASE_URL")
        if base_url:
            base_url = base_url.rstrip("/")
            if not base_url.endswith("/chat/completions"):
                base_url = f"{base_url}/chat/completions"
            endpoint = base_url
            
        self._endpoint = endpoint
        self._session = session or requests.Session()

    def chat(self, messages: Sequence[Mapping[str, Any]], model: str, max_retries: int = 3, backoff_initial: float = 2.0, timeout: float = 300.0) -> Mapping[str, Any]:
        payload: Dict[str, Any] = {"model": model, "messages": list(messages), "temperature": 0.7}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Title": "video-vibes-capture",
        }

        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._session.post(self._endpoint, json=payload, headers=headers, timeout=timeout)
                if response.status_code in {429, 500, 502, 503, 504}:
                    response.raise_for_status()
                response.raise_for_status()
                data = response.json()
                if "error" in data:
                    error_payload = data["error"]
                    msg = str(error_payload.get("message") if isinstance(error_payload, dict) else error_payload)
                    raise OpenRouterError(f"OpenRouter returned error payload: {msg}")
                return data
            except (requests.RequestException, ValueError) as exc:
                if attempt > max_retries:
                    raise OpenRouterError(f"OpenRouter request failed after {max_retries} attempts.") from exc
                time.sleep(backoff_initial * (2 ** (attempt - 1)))

    def chat_with_vision(self, text: str, images: List[Union[str, Path]], model: str) -> Mapping[str, Any]:
        content: List[Dict[str, Any]] = [{"type": "text", "text": text}]
        for img in images:
            data_url = encode_image_to_data_url(img)
            content.append({"type": "image_url", "image_url": {"url": data_url}})
        return self.chat(messages=[{"role": "user", "content": content}], model=model)

# --- Browser Automation ---

def _find_chrome_path() -> str:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    for candidate in candidates:
        if os.path.exists(candidate): return candidate
    return shutil.which("google-chrome") or shutil.which("chromium") or "google-chrome"

def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]

def _wait_for_debugger_target(debug_port: int, target_url: str, timeout: float = 10.0) -> tuple[str, str]:
    import urllib.request
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json", timeout=1.0) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                target_unquoted = urllib.parse.unquote(target_url)
                target_name = Path(urllib.parse.urlparse(target_unquoted).path).name
                for entry in payload:
                    if entry.get("type") != "page":
                        continue
                    ws_url = entry.get("webSocketDebuggerUrl")
                    if not ws_url:
                        continue
                    entry_url = entry.get("url", "")
                    entry_unquoted = urllib.parse.unquote(entry_url)
                    entry_name = Path(urllib.parse.urlparse(entry_unquoted).path).name
                    if entry_unquoted == target_unquoted or entry_url == target_url or (target_name and entry_name == target_name):
                        target_id = entry.get("id") or entry.get("targetId")
                        if not target_id:
                            raise RuntimeError("DevTools target id not found for page.")
                        return ws_url, target_id
        except: pass
        time.sleep(0.1)
    raise RuntimeError("Timed out waiting for DevTools endpoint")

async def _wait_for_load_event(debug_port: int, target_url: str, timeout: float = 10.0):
    import websocket
    ws_url, _ = _wait_for_debugger_target(debug_port, target_url, timeout=timeout)
    ws = websocket.create_connection(ws_url, timeout=timeout)
    try:
        ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
        ws.send(json.dumps({"id": 2, "method": "Page.reload", "params": {"ignoreCache": True}}))
        start = time.time()
        while time.time() - start < timeout:
            ws.settimeout(0.5)
            try:
                msg = json.loads(ws.recv())
                if msg.get("method") == "Page.loadEventFired": return
            except websocket.WebSocketTimeoutException: continue
    finally:
        ws.close()


def _set_window_bounds(
    debug_port: int,
    target_url: str,
    left: int,
    top: int,
    width: int,
    height: int,
    timeout: float = 5.0,
) -> None:
    import websocket
    ws_url, target_id = _wait_for_debugger_target(debug_port, target_url, timeout=timeout)
    ws = websocket.create_connection(ws_url, timeout=timeout)
    try:
        ws.send(json.dumps({"id": 1, "method": "Browser.getWindowForTarget", "params": {"targetId": target_id}}))
        msg = json.loads(ws.recv())
        window_id = msg.get("result", {}).get("windowId")
        if window_id is None:
            return
        ws.send(json.dumps({
            "id": 2,
            "method": "Browser.setWindowBounds",
            "params": {
                "windowId": window_id,
                "bounds": {"left": left, "top": top, "width": width, "height": height}
            }
        }))
        ws.recv()
    finally:
        ws.close()

def _get_window_bounds(debug_port: int, target_url: str, timeout: float = 5.0):
    import websocket
    ws_url, target_id = _wait_for_debugger_target(debug_port, target_url, timeout=timeout)
    ws = websocket.create_connection(ws_url, timeout=timeout)
    try:
        ws.send(json.dumps({"id": 3, "method": "Browser.getWindowForTarget", "params": {"targetId": target_id}}))
        msg = json.loads(ws.recv())
        window_id = msg.get("result", {}).get("windowId")
        if window_id is None:
            return None
        ws.send(json.dumps({"id": 4, "method": "Browser.getWindowBounds", "params": {"windowId": window_id}}))
        msg = json.loads(ws.recv())
        return msg.get("result", {}).get("bounds")
    finally:
        ws.close()

def _evaluate_script(debug_port: int, target_url: str, expression: str, timeout: float = 5.0) -> Any:
    import websocket
    ws_url, target_id = _wait_for_debugger_target(debug_port, target_url, timeout=timeout)
    ws = websocket.create_connection(ws_url, timeout=timeout)
    try:
        ws.send(json.dumps({
            "id": 50,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": True
            }
        }))
        resp = json.loads(ws.recv())
        return resp.get("result", {}).get("result", {}).get("value")
    finally:
        ws.close()

def _bring_target_to_front(debug_port: int, target_url: str, timeout: float = 5.0) -> None:
    import websocket
    ws_url, _ = _wait_for_debugger_target(debug_port, target_url, timeout=timeout)
    ws = websocket.create_connection(ws_url, timeout=timeout)
    try:
        ws.send(json.dumps({"id": 1, "method": "Page.bringToFront"}))
        ws.recv()
    except Exception:
        pass
    finally:
        ws.close()

def _focus_chrome(app_name: str):
    try:
        # Multifaceted approach to ensure focus
        scripts = [
            f'tell application "{app_name}" to activate',
            f'tell application "System Events" to set frontmost of process "{app_name}" to true',
        ]
        for script in scripts:
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=1)
    except Exception:
        pass

async def capture_frames(region: dict, duration: float, fps: float, output_dir: Path) -> List[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    frame_paths = []
    interval = 1.0 / fps
    with mss() as sct:
        start_time = time.perf_counter()
        frame_count = 0
        target_frames = int(duration * fps)
        while frame_count < target_frames:
            now = time.perf_counter()
            if now - start_time >= frame_count * interval:
                shot = sct.grab(region)
                path = output_dir / f"frame_{frame_count:04d}.png"
                tools.to_png(shot.rgb, shot.size, output=str(path))
                frame_paths.append(path)
                frame_count += 1
            else:
                await asyncio.sleep(0.001)
    return frame_paths

async def main():
    parser = argparse.ArgumentParser(description="Capture HTML animation and analyze with AI.")
    parser.add_argument("html_path", help="Path to local HTML file")
    parser.add_argument("--duration", type=float, default=5.0, help="Duration to capture in seconds")
    parser.add_argument("--fps", type=float, default=2.0, help="Frames per second to capture")
    parser.add_argument("--prompt", default="Describe what's happening in these frames.", help="Prompt for AI analysis")
    parser.add_argument("--model", default="qwen/qwen3-vl-8b-instruct", help="OpenRouter model")
    parser.add_argument("--output-dir", default="artifacts", help="Directory to save captured frames")
    args = parser.parse_args()

    if args.html_path.startswith("http://") or args.html_path.startswith("https://"):
        url = args.html_path
        name = "remote_url"
    else:
        html_path = Path(args.html_path).resolve()
        if not html_path.exists():
            print(f"File not found: {html_path}")
            return
        url = html_path.as_uri()
        name = html_path.name
    
    chrome_path = _find_chrome_path()
    debug_port = _pick_free_port()
    user_data_dir = Path(tempfile.mkdtemp(prefix="video_vibes_gen_"))
    
    chrome_args = [
        chrome_path,
        "--new-window",
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
        "--window-position=0,0",
        "--window-size=1280,720",
        f"--app={url}",
    ]

    print(f"Launching Chrome and loading {name}...")
    chrome_proc = subprocess.Popen(chrome_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    try:
        await _wait_for_load_event(debug_port, url)
        _bring_target_to_front(debug_port, url)
        _set_window_bounds(debug_port, url, left=0, top=30, width=1280, height=720)
        bounds = _get_window_bounds(debug_port, url)
        if not bounds:
            raise RuntimeError("Failed to read Chrome window bounds from DevTools.")
        print(f"Raw CDP bounds: {bounds}")
        
        # Calculate viewport offset (exclude title bar/borders)
        viewport = _evaluate_script(debug_port, url, "({x: window.screenX, y: window.screenY, ow: window.outerWidth, oh: window.outerHeight, iw: window.innerWidth, ih: window.innerHeight})")
        
        if viewport:
            # outerHeight = innerHeight + title_bar_height (approx)
            # We assume chrome is at the top.
            border_top = viewport['oh'] - viewport['ih']
            border_left = (viewport['ow'] - viewport['iw']) // 2
            
            region = {
                "left": int(bounds.get("left", 0)) + int(border_left),
                "top": int(bounds.get("top", 0)) + int(border_top),
                "width": int(viewport['iw']),
                "height": int(viewport['ih']),
            }
            print(f"Computed Viewport Metrics: {viewport}")
        else:
            region = {
                "left": int(bounds.get("left", 0)),
                "top": int(bounds.get("top", 0)),
                "width": int(bounds.get("width", 0)),
                "height": int(bounds.get("height", 0)),
            }

        if region["width"] <= 0 or region["height"] <= 0:
            raise RuntimeError("Invalid window bounds detected.")

        print(f"Capture region: {region}")

        # Determine app name for focus
        app_name = "Google Chrome"
        if "Canary" in chrome_path: app_name = "Google Chrome Canary"
        elif "Chromium" in chrome_path: app_name = "Chromium"
        _focus_chrome(app_name)
        
        stamp = time.strftime("%Y%m%d_%H%M%S")
        output_dir = Path(args.output_dir) / f"video_{stamp}"
        
        print(f"Capturing {args.duration}s @ {args.fps}fps...")
        frame_paths = await capture_frames(region, args.duration, args.fps, output_dir)
        print(f"Captured {len(frame_paths)} frames to {output_dir}")
        
        print(f"Analyzing with {args.model}...")
        
        # Run synchronous client in executor
        loop = asyncio.get_running_loop()
        client = OpenRouterClient()
        response = await loop.run_in_executor(
            None, 
            lambda: client.chat_with_vision(text=args.prompt, images=frame_paths, model=args.model)
        )
        content = response["choices"][0]["message"]["content"]
        
        print("\n--- ANALYSIS RESULT ---")
        print(content)
        print("------------------------")
        
    except Exception as e:
        print(f"\nERROR: {e}")
    finally:
        chrome_proc.terminate()
        try:
            chrome_proc.wait(timeout=2)
        except:
            chrome_proc.kill()
        shutil.rmtree(user_data_dir, ignore_errors=True)

if __name__ == "__main__":
    asyncio.run(main())
