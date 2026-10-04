# -*- coding: utf-8 -*-
"""
bypass/joinwomu/session.py
==========================
Thread-safe, customer-PC reliable HTTP session for joinwomu.com.

Features:
- File-based cookie persistence across application runs in %TEMP%
- Automatic Cloudflare Turnstile bypass via system Microsoft Edge / Chrome CDP
- Standard library socket/subprocess - no Playwright or Selenium required
- Thread-safe singleton with lock to prevent concurrent browser spawns
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Optional
import urllib.parse

import requests

_log = logging.getLogger(__name__)

# Alias across namespaces so tests, hotpatches, and frozen builds share the identical singleton
_self_mod = sys.modules.get(__name__)
if _self_mod is not None:
    for _alias in ("bypass.joinwomu.session", "AIVideoTranslator.bypass.joinwomu.session", "joinwomu_session"):
        sys.modules[_alias] = _self_mod

BASE_URL = "https://www.joinwomu.com"
COOKIE_CACHE_FILE = os.path.join(tempfile.gettempdir(), "unich_joinwomu_cookies.json")
COOKIE_TTL_SEC = 7200  # 2 hours

# Customer-PC reliability: longer timeouts for slow hardware and antivirus interference
TURNSTILE_CDP_WAIT_ITERATIONS = 25   # was 15 — give slow Edge installs more time to start
TURNSTILE_CHALLENGE_WAIT_ITERATIONS = 35  # was 25 — slow customer networks need more patience

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": f"{BASE_URL}/",
    "Sec-Ch-Ua": '"Not?A_Brand";v="99", "Chromium";v="130"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}

_session_lock = threading.Lock()
_warmup_lock = threading.Lock()   # Prevent multiple concurrent browser launches
_global_session: Optional[requests.Session] = None
_global_ua: str = DEFAULT_UA
_session_ready = threading.Event()  # Set once a valid session is established
_warmup_in_progress = False


def _find_system_browser() -> str | None:
    """
    Find Microsoft Edge or Google Chrome executable on Windows reliably across
    all system drive letters, localized paths, and user profiles.
    """
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_app_data = os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local"))
    candidates = [
        os.path.join(program_files_x86, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(program_files, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(local_app_data, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(local_app_data, "Google", "Chrome", "Application", "chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    for name in ("msedge.exe", "msedge", "chrome.exe", "chrome"):
        p = shutil.which(name)
        if p and os.path.isfile(p):
            return p
    return None


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _cdp_ws_connect(host: str, port: int, path: str, timeout: float = 15.0) -> socket.socket:
    """Open a raw WebSocket connection to a CDP target (pure standard library, zero external deps)."""
    sock = socket.create_connection((host, port), timeout=timeout)
    ws_key = base64.b64encode(os.urandom(16)).decode()
    handshake = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {ws_key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    )
    sock.sendall(handshake.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("WebSocket handshake failed: connection closed")
        buf += chunk
    if b"101" not in buf.split(b"\r\n")[0]:
        raise ConnectionError(f"WebSocket handshake rejected: {buf[:200]}")
    return sock


def _cdp_ws_send(sock: socket.socket, data: str) -> None:
    """Send a masked WebSocket text frame."""
    payload = data.encode("utf-8")
    frame = bytearray()
    frame.append(0x81)  # FIN + TEXT
    length = len(payload)
    if length <= 125:
        frame.append(0x80 | length)
    elif length <= 65535:
        frame.append(0x80 | 126)
        frame.extend(struct.pack(">H", length))
    else:
        frame.append(0x80 | 127)
        frame.extend(struct.pack(">Q", length))
    mask_key = os.urandom(4)
    frame.extend(mask_key)
    masked = bytearray(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    frame.extend(masked)
    sock.sendall(bytes(frame))


def _cdp_ws_recv(sock: socket.socket, timeout: float = 30.0) -> str:
    """Receive a WebSocket text frame (unmasked, from server)."""
    sock.settimeout(timeout)
    header = b""
    while len(header) < 2:
        chunk = sock.recv(2 - len(header))
        if not chunk:
            return ""
        header += chunk
    b1, b2 = header[0], header[1]
    length = b2 & 0x7F
    if length == 126:
        raw = b""
        while len(raw) < 2:
            chunk = sock.recv(2 - len(raw))
            if not chunk:
                return ""
            raw += chunk
        length = struct.unpack(">H", raw)[0]
    elif length == 127:
        raw = b""
        while len(raw) < 8:
            chunk = sock.recv(8 - len(raw))
            if not chunk:
                return ""
            raw += chunk
        length = struct.unpack(">Q", raw)[0]
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(min(65536, length - len(payload)))
        if not chunk:
            break
        payload += chunk
    return payload.decode("utf-8", errors="replace")


def _cdp_call(ws_url: str, method: str, params: dict | None = None, timeout: float = 15.0) -> dict | None:
    """
    Send a CDP command over WebSocket and wait for result.
    Uses pure standard library socket first (zero external dependencies),
    with websocket-client fallback if available.
    """
    # 1. Primary: Standard library socket (works 100% on any Windows PC without pip packages)
    try:
        parsed = urllib.parse.urlparse(ws_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 9222
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        sock = _cdp_ws_connect(host, port, path, timeout=timeout)
        msg_id = int(time.time() * 1000) % 1000000
        msg = {"id": msg_id, "method": method}
        if params:
            msg["params"] = params

        _cdp_ws_send(sock, json.dumps(msg))

        deadline = time.time() + timeout
        result_payload = None
        while time.time() < deadline:
            sock.settimeout(max(0.1, deadline - time.time()))
            try:
                raw = _cdp_ws_recv(sock, timeout=max(0.1, deadline - time.time()))
                if not raw:
                    continue
                resp = json.loads(raw)
                if resp.get("id") == msg_id:
                    result_payload = resp.get("result", {})
                    break
            except Exception:
                break
        try:
            sock.close()
        except Exception:
            pass
        if result_payload is not None:
            return result_payload
    except Exception as exc:
        _log.debug("Stdlib CDP call error for %s: %s", method, exc)

    # 2. Fallback: websocket-client if installed
    try:
        import websocket
        ws = websocket.create_connection(ws_url, timeout=timeout)
        msg_id = int(time.time() * 1000) % 1000000
        msg = {"id": msg_id, "method": method}
        if params:
            msg["params"] = params
        ws.send(json.dumps(msg))

        deadline = time.time() + timeout
        while time.time() < deadline:
            ws.settimeout(max(0.1, deadline - time.time()))
            try:
                raw = ws.recv()
                resp = json.loads(raw)
                if resp.get("id") == msg_id:
                    ws.close()
                    return resp.get("result", {})
            except Exception:
                break
        ws.close()
    except Exception as exc:
        _log.debug("Fallback websocket CDP call failed for %s: %s", method, exc)

    return None


def _solve_cloudflare_turnstile(target_url: str = f"{BASE_URL}/tv/3.html") -> dict[str, Any] | None:
    """Launch headless/visible Edge via CDP to solve Cloudflare Turnstile and extract cookies."""
    browser_exe = _find_system_browser()
    if not browser_exe:
        _log.warning("No system browser found to solve Cloudflare Turnstile")
        return None

    port = _get_free_port()
    tmpdir = tempfile.mkdtemp(prefix="unich_jw_")
    proc = None
    try:
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 6  # SW_MINIMIZE

        cmd = [
            browser_exe,
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-fre",
            "--disable-features=msFirstRunExperience,msEdgeWelcomePage,msImplicitSignIn",
            "--disable-search-engine-choice-screen",
            "--disable-extensions",
            "--disable-default-apps",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-component-update",
            "--mute-audio",
            "--hide-scrollbars",
            f"--user-data-dir={tmpdir}",
            "--window-position=-32000,-32000",
            "--window-size=1280,900",
            target_url,
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=startupinfo,
        )

        pages = None
        for _ in range(TURNSTILE_CDP_WAIT_ITERATIONS):
            time.sleep(1)
            try:
                r = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=2)
                if r.status_code == 200 and r.json():
                    pages = r.json()
                    break
            except Exception:
                pass

        if not pages:
            _log.warning("CDP browser target not available")
            return None

        target = None
        for p in pages:
            if "joinwomu" in (p.get("url", "") + p.get("title", "")).lower():
                target = p
                break
        if not target:
            target = pages[0]

        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            return None

        # Wait for Turnstile to clear (checks title)
        passed = False
        browser_ua = DEFAULT_UA
        for i in range(TURNSTILE_CHALLENGE_WAIT_ITERATIONS):
            time.sleep(1)
            res = _cdp_call(ws_url, "Runtime.evaluate", {"expression": "document.title", "returnByValue": True}, timeout=3)
            title = res.get("result", {}).get("value", "") if res else ""
            if title and "moment" not in title.lower() and "verify" not in title.lower() and len(title) > 3:
                passed = True
                break

        if not passed:
            _log.warning("Cloudflare Turnstile did not clear in time")

        # Get Browser UA
        res_ua = _cdp_call(ws_url, "Runtime.evaluate", {"expression": "navigator.userAgent", "returnByValue": True}, timeout=3)
        if res_ua and res_ua.get("result", {}).get("value"):
            browser_ua = res_ua["result"]["value"]

        # Get Cookies across root and image subdomains
        cookie_urls = [BASE_URL, f"{BASE_URL}/", "https://www.joinwomu.com", "https://img.joinwomu.com"]
        res_cookies = _cdp_call(ws_url, "Network.getCookies", {"urls": cookie_urls}, timeout=5)
        cookies_list = res_cookies.get("cookies", []) if res_cookies else []
        cookie_dict = {c["name"]: c["value"] for c in cookies_list if "name" in c and "value" in c}

        if cookie_dict:
            payload = {
                "cookies": cookie_dict,
                "user_agent": browser_ua,
                "timestamp": time.time(),
            }
            try:
                with open(COOKIE_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
            except Exception as exc:
                _log.debug("Failed to write cookie cache: %s", exc)
            return payload

    except Exception as exc:
        _log.warning("Turnstile bypass error: %s", exc)
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
    return None


def _load_cached_cookies() -> dict[str, Any] | None:
    """Load valid cookies from disk if within TTL."""
    if not os.path.isfile(COOKIE_CACHE_FILE):
        return None
    try:
        with open(COOKIE_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            ts = float(data.get("timestamp", 0))
            if time.time() - ts < COOKIE_TTL_SEC:
                return data
    except Exception:
        pass
    return None


def get_joinwomu_session(force_refresh: bool = False) -> requests.Session:
    """Return an active requests.Session configured with Cloudflare credentials.

    Thread-safe: uses _warmup_lock to ensure only one thread launches the
    headless browser at a time, preventing the "36-poster storm" from spawning
    36 Edge processes on a customer PC.
    """
    global _global_session, _global_ua, _warmup_in_progress

    with _session_lock:
        if _global_session is not None and not force_refresh:
            return _global_session

    # Serialize browser launches — only one thread solves Turnstile at a time.
    # Other threads wait here instead of each spawning their own Edge process.
    with _warmup_lock:
        # Double-check: another thread may have finished while we waited
        with _session_lock:
            if _global_session is not None and not force_refresh:
                return _global_session

        _warmup_in_progress = True
        try:
            sess = requests.Session()
            cached = _load_cached_cookies() if not force_refresh else None

            if not cached:
                _log.info("No cached JoinWomu cookies — launching Turnstile solver…")
                cached = _solve_cloudflare_turnstile()

            if cached and isinstance(cached, dict):
                cookies = cached.get("cookies", {})
                ua = cached.get("user_agent", DEFAULT_UA)
                _global_ua = ua
                sess.headers.update(DEFAULT_HEADERS)
                sess.headers["User-Agent"] = ua
                sess.cookies.update(cookies)
                _log.info("JoinWomu session established with %d cookies", len(cookies))
            else:
                sess.headers.update(DEFAULT_HEADERS)
                _log.warning("JoinWomu session created without Cloudflare cookies — poster loading may fail")

            with _session_lock:
                _global_session = sess
            _session_ready.set()
            return sess
        finally:
            _warmup_in_progress = False


def fetch_joinwomu_html(url: str, timeout: int = 15) -> str:
    """
    Fetch an HTML page from joinwomu.com with automatic Cloudflare retry.
    """
    sess = get_joinwomu_session()
    try:
        resp = sess.get(url, timeout=timeout)
        if resp.status_code == 200 and "Just a moment..." not in resp.text:
            return resp.text
        if resp.status_code in (403, 503) or "Just a moment..." in resp.text:
            _log.info("Cloudflare challenge encountered on %s, refreshing session…", url)
            sess = get_joinwomu_session(force_refresh=True)
            resp = sess.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp.text
    except Exception as exc:
        _log.warning("Failed to fetch %s: %s", url, exc)
    return ""


def fetch_joinwomu_image(url: str, timeout: int = 15) -> tuple[bytes, str]:
    """
    Fetch image bytes from img.joinwomu.com / joinwomu.com using the Cloudflare-cleared session.
    Automatically handles Turnstile / 403 / 503 challenges with session refresh.
    """
    sess = get_joinwomu_session()
    headers = {
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": f"{BASE_URL}/",
    }
    try:
        resp = sess.get(url, headers=headers, timeout=timeout)
        if resp.status_code == 200 and resp.content:
            content_type = resp.headers.get("Content-Type", "").split(";", 1)[0].lower()
            return resp.content, content_type
        if resp.status_code in (403, 503) or "Just a moment..." in resp.text:
            _log.info("Cloudflare challenge encountered on image %s, refreshing session…", url)
            sess = get_joinwomu_session(force_refresh=True)
            resp = sess.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200 and resp.content:
                content_type = resp.headers.get("Content-Type", "").split(";", 1)[0].lower()
                return resp.content, content_type
            resp.raise_for_status()
        resp.raise_for_status()
    except Exception as exc:
        _log.warning("Failed to fetch image %s: %s", url, exc)
        raise
    return b"", ""


def is_session_ready() -> bool:
    """Return True if a JoinWomu session with cookies has been established.

    Poster loading tasks can check this before attempting to fetch images,
    avoiding 403 errors on customer PCs where Turnstile hasn't been solved yet.
    """
    if _session_ready.is_set():
        return True
    # Fast path: check if valid cookies are already cached on disk!
    cached = _load_cached_cookies()
    if cached and isinstance(cached, dict) and cached.get("cookies"):
        try:
            get_joinwomu_session()
            return True
        except Exception:
            pass
    return False


def wait_for_session(timeout: float = 60.0) -> bool:
    """Block until the session is ready or timeout expires.

    Returns True if the session became ready, False on timeout.
    Used by poster tasks to wait for the initial Turnstile solve
    instead of each independently triggering a browser launch.
    """
    if is_session_ready():
        return True
    warm_session_async()
    return _session_ready.wait(timeout=timeout)


def warm_session_async() -> None:
    """Start a background thread to pre-warm the JoinWomu Cloudflare session.

    Called when the JoinWomu catalog is first loaded so that poster images
    can be fetched immediately once the catalog cards render. On customer PCs
    this prevents the poster-loading "storm" from all competing to launch Edge.
    """
    global _warmup_in_progress
    if is_session_ready() or _warmup_in_progress:
        return

    def _warmup() -> None:
        try:
            get_joinwomu_session()
        except Exception as exc:
            _log.warning("Background JoinWomu session warm-up failed: %s", exc)

    t = threading.Thread(target=_warmup, name="joinwomu-session-warmup", daemon=True)
    t.start()


# Pre-initialize session from disk cache on module load if valid cookies exist (0ms cost)
try:
    _init_cached = _load_cached_cookies()
    if _init_cached and isinstance(_init_cached, dict) and _init_cached.get("cookies"):
        get_joinwomu_session()
except Exception:
    pass


# ===========================================================================
# Complete JoinWomu Catalog & Scraper Provider
# Bundled directly into hotpatch to guarantee 100% operation on customer PCs
# without requiring frozen binary recompilation or missing .pyd dependencies.
# ===========================================================================

import html as html_mod
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

JOINWOMU_DOMAINS = (
    "joinwomu.com",
    "www.joinwomu.com",
    "dytt-tvs.com",
    "dytt-cinema.com",
    "dytt-film.com",
    "dytt-movie.com",
    "ppqrrs.com",
    "uvjtih.cn",
)

CACHE_TTL = 300  # 5 minutes
_catalog_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def is_joinwomu_url(url: str | None) -> bool:
    """Return True if URL belongs to joinwomu.com or its stream domains."""
    if not url or not isinstance(url, str):
        return False
    clean = url.strip().lower()
    return any(domain in clean for domain in JOINWOMU_DOMAINS) or "joinwomu" in clean or "dytt-" in clean


def _extract_id(url: str) -> str | None:
    """Extract numeric ID from video or play URL."""
    m = re.search(r"/(?:video|play)/(\d+)", url)
    if m:
        return m.group(1)
    return None


def _parse_catalog_cards(html: str) -> tuple[list[dict[str, Any]], int, bool]:
    """Parse video cards and pagination from joinwomu catalog HTML."""
    if not html or not BeautifulSoup:
        return [], 0, False

    soup = BeautifulSoup(html, "html.parser")
    books: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    items = soup.find_all(["li", "div"], class_=re.compile(r"col-xs-|col-md-|col-lg-"))
    for item in items:
        a_tag = item.find("a", href=re.compile(r"/video/\d+\.html"))
        if not a_tag:
            continue

        href = str(a_tag.get("href") or "").strip()
        m_id = re.search(r"/video/(\d+)\.html", href)
        bid = m_id.group(1) if m_id else ""
        if not bid or bid in seen_ids:
            continue
        seen_ids.add(bid)

        title_el = item.find("h3") or a_tag
        raw_title = title_el.get_text(strip=True) or a_tag.get("title") or ""
        title = html_mod.unescape(raw_title).strip()
        if not title:
            continue

        cover = ""
        for el in item.find_all(attrs={"data-background": True}):
            cover = el.get("data-background") or ""
            if cover:
                break
        if not cover:
            for el in item.find_all(attrs={"data-original": True}):
                cover = el.get("data-original") or ""
                if cover:
                    break
        if not cover:
            img = item.find("img")
            if img:
                cover = img.get("data-src") or img.get("data-original") or img.get("src") or ""
        if cover.startswith("//"):
            cover = "https:" + cover

        badge = item.find(class_=re.compile(r"fed-list-class|tag|badge"))
        tag_text = badge.get_text(strip=True) if badge else ""

        rem = item.find(class_=re.compile(r"vtitle|remarks?|status"))
        remark = rem.get_text(strip=True) if rem else ""

        actor = item.find(class_=re.compile(r"item-status"))
        actor_text = actor.get_text(strip=True) if actor else ""

        m_num = re.search(r"\d+", remark)
        ep_count = int(m_num.group()) if m_num else 0
        is_full = bool(re.search(r"全集|完结", remark)) or (ep_count <= 1 and bool(remark))

        if is_full and ep_count <= 1:
            ep_text = "全集 · Full Movie" if "全集" in remark else (remark or "全集 · Full Movie")
        elif ep_count > 1:
            ep_text = f"更新至{ep_count}集" if "更新" in remark else f"全{ep_count}集 · Full"
        else:
            ep_text = remark or "Full Series"

        full_url = f"{BASE_URL}{href}" if href.startswith("/") else href

        books.append({
            "book_id": bid,
            "title": title,
            "thumbnail": cover,
            "book_cover": cover,
            "ep_count": ep_count,
            "ep_text": ep_text,
            "remark": remark,
            "is_full": is_full and ep_count <= 1,
            "intro": actor_text,
            "synopsis": actor_text,
            "url": full_url,
            "platform": "JoinWomu",
            "tags": tag_text,
            "quality": "Full HD",
            "is_full_hd": True,
        })

    total_pages = 1
    has_more = False
    page_ul = soup.find(class_=re.compile(r"ewave-page|pagination"))
    if page_ul:
        num_span = page_ul.find(class_="num")
        if num_span:
            m_p = re.search(r"\d+\s*/\s*(\d+)", num_span.get_text(strip=True))
            if m_p:
                total_pages = int(m_p.group(1))

        for a in page_ul.find_all("a", href=re.compile(r"/page/(\d+)")):
            m_a = re.search(r"/page/(\d+)", a.get("href", ""))
            if m_a:
                total_pages = max(total_pages, int(m_a.group(1)))

        next_link = page_ul.find(lambda tag: tag.name == "a" and "下一页" in tag.get_text())
        has_more = next_link is not None and not (
            next_link.parent and "disabled" in next_link.parent.get("class", [])
        )

    estimated_total = total_pages * max(len(books), 24)
    return books, estimated_total, has_more


def browse_joinwomu(
    page: int = 1,
    page_size: int = 50,
    category: str = "short_drama",
    status_cb: Optional[Any] = None,
    cancel_check: Optional[Any] = None,
) -> dict[str, Any]:
    """Browse JoinWomu catalog for short drama or cartoon."""
    if cancel_check and cancel_check():
        raise RuntimeError("Cancelled")

    current_page = max(1, int(page))
    cat_clean = str(category or "").strip().lower()
    is_cartoon = cat_clean in ("cartoon", "anime", "4", "tv4", "动漫")

    cache_key = f"browse_{'cartoon' if is_cartoon else 'short_drama'}_{current_page}"
    with _cache_lock:
        now = time.time()
        if cache_key in _catalog_cache:
            ts, data = _catalog_cache[cache_key]
            if now - ts <= CACHE_TTL:
                return data

    channel_id = 4 if is_cartoon else 3
    channel_name = "Cartoon" if is_cartoon else "Short Drama"

    if status_cb:
        status_cb(f"Loading JoinWomu {channel_name} (page {current_page})…")

    if current_page == 1:
        target_url = f"{BASE_URL}/tv/{channel_id}.html"
    else:
        target_url = f"{BASE_URL}/tv/{channel_id}/page/{current_page}.html"

    html = fetch_joinwomu_html(target_url)
    books, total, has_more = _parse_catalog_cards(html)

    result = {
        "books": books,
        "page": current_page,
        "page_size": page_size,
        "has_more": has_more or (total > current_page * 24),
        "total": total,
    }

    with _cache_lock:
        _catalog_cache[cache_key] = (time.time(), result)

    return result


def search_joinwomu(
    word: str,
    page: int = 1,
    page_size: int = 50,
    status_cb: Optional[Any] = None,
    cancel_check: Optional[Any] = None,
) -> dict[str, Any]:
    """Search JoinWomu titles by keyword."""
    if cancel_check and cancel_check():
        raise RuntimeError("Cancelled")

    current_page = max(1, int(page))
    kw = word.strip()
    if not kw:
        return browse_joinwomu(current_page, page_size, status_cb=status_cb, cancel_check=cancel_check)

    if status_cb:
        status_cb(f"Searching JoinWomu for '{kw}'…")

    encoded_kw = urllib.parse.quote(kw)
    if current_page == 1:
        target_url = f"{BASE_URL}/search.html?wd={encoded_kw}"
    else:
        target_url = f"{BASE_URL}/search/wd/{encoded_kw}/page/{current_page}.html"

    html = fetch_joinwomu_html(target_url)
    books, total, has_more = _parse_catalog_cards(html)

    return {
        "books": books,
        "page": current_page,
        "page_size": page_size,
        "has_more": has_more,
        "total": total,
    }


def fetch_joinwomu_info(
    url: str,
    status_cb: Optional[Any] = None,
    cancel_check: Optional[Any] = None,
) -> dict[str, Any]:
    """Fetch series metadata and complete episode list from JoinWomu."""
    if cancel_check and cancel_check():
        raise RuntimeError("Cancelled")

    clean_url = url.strip()
    if clean_url.startswith("/"):
        clean_url = f"{BASE_URL}{clean_url}"

    if clean_url.isdigit():
        clean_url = f"{BASE_URL}/video/{clean_url}.html"

    if status_cb:
        status_cb("Fetching JoinWomu drama details…")

    html = fetch_joinwomu_html(clean_url)
    if not html:
        sid = _extract_id(clean_url)
        if sid:
            video_url = f"{BASE_URL}/video/{sid}.html"
            html = fetch_joinwomu_html(video_url)

    if not html:
        raise ValueError(f"Could not load JoinWomu page: {clean_url}")

    soup = BeautifulSoup(html, "html.parser")

    title = ""
    title_el = soup.find("h1") or soup.find(class_=re.compile(r"title|vod-name|drama-name"))
    if title_el:
        title = title_el.get_text(strip=True)
    if not title and soup.title:
        title = soup.title.get_text(strip=True).split("-")[0].replace("免费观看", "").strip()
        m_t = re.search(r"《([^》]+)》", title)
        if m_t:
            title = m_t.group(1)
    title = title or "JoinWomu Drama"

    cover = ""
    og_img = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
    if og_img and og_img.get("content"):
        cover = str(og_img["content"]).strip()

    if not cover:
        pic_el = soup.find(class_=re.compile(r"pic|poster|thumb|img-wrapper"))
        if pic_el:
            sub_img = pic_el.find("img")
            if sub_img:
                cover = sub_img.get("data-original") or sub_img.get("data-src") or sub_img.get("src") or ""

    if not cover:
        for el in soup.find_all(attrs={"data-original": True}):
            orig = el.get("data-original")
            if orig and ("upload" in orig or "img" in orig or "vod" in orig):
                cover = orig
                break

    if not cover:
        for el in soup.find_all(attrs={"data-background": True}):
            bg = el.get("data-background")
            if bg and ("upload" in bg or "img" in bg or "vod" in bg):
                cover = bg
                break

    if cover.startswith("//"):
        cover = "https:" + cover

    synopsis = ""
    meta_desc = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", property="og:description")
    if meta_desc and meta_desc.get("content"):
        synopsis = str(meta_desc["content"]).strip()
    if not synopsis:
        for el in soup.find_all(["div", "p", "span"]):
            txt = el.get_text(strip=True)
            if ("简介：" in txt or "简介:" in txt) and len(txt) > 20:
                synopsis = re.sub(r"^简介[：:]\s*", "", txt).strip()
                break
    if not synopsis:
        desc_el = soup.find(class_=re.compile(r"desc|intro|synopsis|summary|sketch"))
        if desc_el:
            txt = desc_el.get_text(strip=True)
            if "首页" not in txt[:20]:
                synopsis = txt

    current_stream_url = ""
    m_player = re.search(r"var\s+player_aaaa\s*=\s*(\{.*?\})\s*[;<\n]", html, re.DOTALL)
    if m_player:
        try:
            pdata = json.loads(m_player.group(1))
            raw_stream = pdata.get("url", "")
            if raw_stream.startswith("http"):
                current_stream_url = raw_stream
        except Exception:
            pass

    candidate_sources = []
    pbox = soup.find("div", class_="playlist-box")
    if pbox:
        candidate_sources.extend(pbox.find_all("ul", class_=re.compile(r"playlist|ewave-playlist")))
    if not candidate_sources:
        candidate_sources.extend(soup.find_all("ul", class_=re.compile(r"playlist|ewave-playlist")))

    candidate_sources.sort(
        key=lambda s: len(s.find_all("a", href=re.compile(r"/play/"))),
        reverse=True,
    )

    episodes: list[dict[str, Any]] = []
    seen_ep_nums: set[int] = set()

    for source in candidate_sources:
        for idx, a in enumerate(source.find_all("a", href=re.compile(r"/play/"))):
            ep_text = a.get_text(strip=True)
            ep_href = str(a.get("href") or "").strip()
            if not ep_href.startswith("http"):
                ep_href = f"{BASE_URL}{ep_href}"

            m_num = re.search(r"\d+", ep_text)
            ep_num = int(m_num.group()) if m_num else (idx + 1)

            if ep_num in seen_ep_nums:
                continue
            seen_ep_nums.add(ep_num)

            ep_video_url = current_stream_url if (current_stream_url and ep_href in clean_url) else ""

            episodes.append({
                "num": ep_num,
                "ep_num": ep_num,
                "index": ep_num,
                "title": f"{title} {ep_text}" if ep_text else f"Episode {ep_num}",
                "url": ep_href,
                "thumbnail": cover,
                "book_cover": cover,
                "synopsis": synopsis,
                "video_url": ep_video_url,
                "source": "joinwomu",
                "platform": "JoinWomu",
            })

    episodes.sort(key=lambda x: x["num"])

    if not episodes:
        episodes.append({
            "num": 1,
            "ep_num": 1,
            "index": 1,
            "title": f"{title} Episode 1",
            "url": clean_url,
            "thumbnail": cover,
            "book_cover": cover,
            "synopsis": synopsis,
            "video_url": current_stream_url,
            "source": "joinwomu",
            "platform": "JoinWomu",
        })

    is_full_compilation = len(episodes) == 1
    if is_full_compilation and episodes:
        episodes[0]["is_full"] = True

    return {
        "title": title,
        "book_id": _extract_id(clean_url) or clean_url,
        "thumbnail": cover,
        "book_cover": cover,
        "synopsis": synopsis,
        "is_full": is_full_compilation,
        "total": len(episodes),
        "episodes": episodes,
        "platform": "JoinWomu",
    }


def get_joinwomu_episode_video_url(episode_url: str, episode: dict | None = None) -> str | None:
    """Resolve direct M3U8/MP4 video stream URL for a JoinWomu episode."""
    if episode and episode.get("video_url") and str(episode["video_url"]).startswith("http"):
        return str(episode["video_url"])

    clean_url = episode_url.strip()
    if clean_url.startswith("/"):
        clean_url = f"{BASE_URL}{clean_url}"

    if "/video/" in clean_url and episode and episode.get("url") and "/play/" in str(episode.get("url")):
        clean_url = str(episode["url"]).strip()
        if clean_url.startswith("/"):
            clean_url = f"{BASE_URL}{clean_url}"

    html = fetch_joinwomu_html(clean_url)
    if not html:
        return None

    if "player_aaaa" not in html and "/play/" in html:
        m_play = re.search(r'href=["\'](/play/[^"\']+)["\']', html)
        if m_play:
            play_url = f"{BASE_URL}{m_play.group(1)}" if m_play.group(1).startswith("/") else m_play.group(1)
            play_html = fetch_joinwomu_html(play_url)
            if play_html:
                html = play_html

    m_player = re.search(r"var\s+player_aaaa\s*=\s*(\{.*?\})\s*[;<\n]", html, re.DOTALL)
    if m_player:
        try:
            pdata = json.loads(m_player.group(1))
            raw_url = pdata.get("url", "")
            if raw_url.startswith("http"):
                stream_url = raw_url.replace(r"\/", "/")
                if episode is not None:
                    episode["video_url"] = stream_url
                return stream_url
        except Exception as exc:
            _log.debug("Error parsing player_aaaa: %s", exc)

    m_mac = re.search(r"var\s+MacPlayer\s*=\s*(\{.*?\})\s*[;<\n]", html, re.DOTALL)
    if m_mac:
        try:
            mdata = json.loads(m_mac.group(1))
            raw_url = mdata.get("PlayUrl", "")
            if raw_url.startswith("http"):
                stream_url = raw_url.replace(r"\/", "/")
                if episode is not None:
                    episode["video_url"] = stream_url
                return stream_url
        except Exception:
            pass

    m3u8_matches = re.findall(r'https?://[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*', html)
    if m3u8_matches:
        stream_url = m3u8_matches[0]
        if episode is not None:
            episode["video_url"] = stream_url
        return stream_url

    mp4_matches = re.findall(r'https?://[^\s"\'<>\\]+\.mp4[^\s"\'<>\\]*', html)
    if mp4_matches:
        stream_url = mp4_matches[0]
        if episode is not None:
            episode["video_url"] = stream_url
        return stream_url

    return None


# Pre-initialize session and signal _session_ready immediately on import if cookies are cached
try:
    _initial_cached = _load_cached_cookies()
    if _initial_cached and isinstance(_initial_cached, dict) and _initial_cached.get("cookies"):
        _init_sess = requests.Session()
        _init_sess.headers.update(DEFAULT_HEADERS)
        _init_sess.headers["User-Agent"] = _initial_cached.get("user_agent", DEFAULT_UA)
        _init_sess.cookies.update(_initial_cached.get("cookies", {}))
        with _session_lock:
            _global_session = _init_sess
        _session_ready.set()
except Exception:
    pass


