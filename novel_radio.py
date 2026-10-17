"""小說電台（本機版）

在自己電腦上開一個小伺服器，讀取 tw.linovelib.com 上你正在聽的那一頁，
交給瀏覽器播放。一次只讀一頁（外加預先讀下一頁），不做整本下載。

聲音有兩種來源：
- Edge 自然語音：裝了 edge-tts 套件時，由這裡把句子轉成 mp3 給網頁播放
- 瀏覽器語音：沒有 edge-tts 或連不上時的備援

執行：python novel_radio.py
"""

import asyncio
import html
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import generic_reader as gr

HOST = "127.0.0.1"
PORT = 8765
ALLOWED_HOSTS = {"tw.linovelib.com", "www.linovelib.com", "linovelib.com"}
BASE = "https://tw.linovelib.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
MIN_INTERVAL = 1.0  # 對網站的請求至少間隔 1 秒
# 打包成 exe 時，網頁、圖示和資料都放在 exe 旁邊，改網頁不用重新打包
HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent

_cache: "OrderedDict[str, str]" = OrderedDict()
_lock = threading.Lock()
_last_request = 0.0


class FetchError(Exception):
    pass


def icon_path(ext: str) -> Path:
    """想換成自己的圖示，就放一個 assets/icon-custom.png（和 .ico），會優先使用。"""
    custom = HERE / "assets" / f"icon-custom.{ext}"
    return custom if custom.exists() else HERE / "assets" / f"icon.{ext}"


# ---------- Edge 自然語音 ----------
try:
    import edge_tts
except ImportError:  # 沒裝也能用，只是只剩瀏覽器語音
    edge_tts = None

_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()
_tts_cache: "OrderedDict[tuple, bytes]" = OrderedDict()
_tts_lock = threading.Lock()
_voices = None

REGION = {"zh-TW": "台灣", "zh-CN": "中國", "zh-HK": "香港粵語", "zh-CN-liaoning": "遼寧話", "zh-CN-shaanxi": "陝西話"}
VOICE_RE = re.compile(r"^zh-[A-Za-z-]+Neural$")


def run_async(coro, timeout=30):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout)


def list_voices():
    global _voices
    if edge_tts is None:
        return []
    if _voices is None:
        try:
            vs = run_async(edge_tts.list_voices(), 20)
        except Exception as e:
            raise FetchError("連不到 Edge 語音服務，請檢查網路") from e
        order = {"zh-TW": 0, "zh-CN": 1, "zh-HK": 2}
        zh = [v for v in vs if v["Locale"].startswith("zh")]
        zh.sort(key=lambda v: (order.get(v["Locale"], 3), v["ShortName"]))
        _voices = [{
            "id": v["ShortName"],
            "name": v["ShortName"].split("-")[-1].replace("Neural", ""),
            "region": REGION.get(v["Locale"], v["Locale"]),
            "gender": "女" if v["Gender"] == "Female" else "男",
        } for v in zh]
    return _voices


async def _synth(text, voice, pitch):
    data = bytearray()
    async for chunk in edge_tts.Communicate(text, voice, pitch=pitch).stream():
        if chunk["type"] == "audio":
            data += chunk["data"]
    return bytes(data)


def tts(text: str, voice: str, pitch_hz: int) -> bytes:
    if edge_tts is None:
        raise FetchError("沒有安裝 edge-tts，請用啟動檔重新啟動，它會自動安裝")
    text = text.strip()[:400]
    if not text or not VOICE_RE.match(voice):
        raise FetchError("語音參數不正確")
    pitch_hz = max(-50, min(50, pitch_hz))
    key = (voice, pitch_hz, text)
    with _tts_lock:
        if key in _tts_cache:
            _tts_cache.move_to_end(key)
            return _tts_cache[key]
    last = None
    for _ in range(2):  # 偶爾會失敗，重試一次
        try:
            audio = run_async(_synth(text, voice, f"{pitch_hz:+d}Hz"))
            if audio:
                break
        except Exception as e:
            last = e
    else:
        raise FetchError("Edge 語音產生失敗，請檢查網路") from last
    with _tts_lock:
        _tts_cache[key] = audio
        while len(_tts_cache) > 300:
            _tts_cache.popitem(last=False)
    return audio


def full_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("/"):
        url = BASE + url
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    # 網址裡的中文（例如 /wiki/西遊記）先轉成 %E8%A5… 的格式，已經轉過的不會再轉
    return urllib.parse.quote(url.split("#")[0], safe=":/?&=%+@!$'()*,;~")


def is_linovelib(url: str) -> bool:
    return urllib.parse.urlparse(full_url(url)).hostname in ALLOWED_HOSTS


def normalize(url: str) -> str:
    p = urllib.parse.urlparse(full_url(url))
    if p.hostname not in ALLOWED_HOSTS:
        raise FetchError("這不是 linovelib 的網址")
    return urllib.parse.urlunparse(("https", "tw.linovelib.com", p.path, "", "", ""))


# 「原網頁」和推薦網站按鈕只會打開這些網站：推薦清單，加上讀過的網站
RECOMMENDED_HOSTS = {"zh.wikisource.org", "home.gamer.com.tw", "www.jjwxc.net", "www.mirrorfiction.com"}
_seen_hosts = set(ALLOWED_HOSTS) | RECOMMENDED_HOSTS


def fetch(url: str, referer: str = None) -> str:
    global _last_request
    with _lock:
        if url in _cache:
            _cache.move_to_end(url)
            return _cache[url]
        wait = MIN_INTERVAL - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        host = urllib.parse.urlparse(url).hostname or ""
        req = urllib.request.Request(url, headers={
            # 維基媒體要求程式用能辨識的名稱，其他網站用一般瀏覽器的名稱
            "User-Agent": "NovelRadio/1.0 (personal read-aloud tool)" if host.endswith("wikisource.org") else UA,
            "Accept-Language": "zh-TW,zh;q=0.9",
            "Referer": referer or BASE + "/",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw, ctype = r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            raise FetchError(f"網站回應錯誤 {e.code}，可能是網址不對或暫時被擋，等一下再試") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise FetchError("連不到網站，請檢查網路") from e
        finally:
            _last_request = time.time()
        text = gr.decode(raw, ctype)
        _seen_hosts.add(host)
        _cache[url] = text
        while len(_cache) > 40:
            _cache.popitem(last=False)
        return text


_img_cache: "OrderedDict[str, tuple]" = OrderedDict()
_img_lock = threading.Lock()
IMG_HOST_RE = re.compile(r"(^|\.)(readpai\.com|linovelib\.com)$")


def fetch_image(src: str, ref: str = ""):
    """插圖的網站會檢查來源，直接在網頁裡放網址會被擋，所以由這裡帶著小說網站當來源去拿。"""
    p = urllib.parse.urlparse(src)
    known = bool(p.hostname and IMG_HOST_RE.search(p.hostname))
    if not known and not gr.is_public_url(src):
        raise FetchError("不支援這個圖片網址")
    with _img_lock:
        if src in _img_cache:
            _img_cache.move_to_end(src)
            return _img_cache[src]
    referer = BASE + "/" if known or not ref else ref
    req = urllib.request.Request(src, headers={"User-Agent": UA, "Referer": referer})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data, ctype = r.read(), r.headers.get("Content-Type", "image/jpeg")
    except (urllib.error.URLError, TimeoutError) as e:
        raise FetchError("插圖讀取失敗") from e
    if not ctype.startswith("image/"):
        raise FetchError("插圖讀取失敗")
    with _img_lock:
        _img_cache[src] = (data, ctype)
        while sum(len(d) for d, _ in _img_cache.values()) > 60 * 1024 * 1024:  # 最多記 60MB
            _img_cache.popitem(last=False)
    return data, ctype


def strip_tags(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).replace(" ", " ").strip()


def read_param(page: str, key: str):
    m = re.search(key + r"\s*:\s*'([^']*)'", page)
    return m.group(1) if m else None


def chapter_key(url: str):
    """/novel/1/2_3.html -> ('1', '2')"""
    m = re.search(r"/novel/(\d+)/(\d+)(?:_\d+)?\.html", url or "")
    return (m.group(1), m.group(2)) if m else None


def parse_page(url: str) -> dict:
    return parse_page_lnb(url) if is_linovelib(url) else parse_page_generic(url)


def parse_catalog(url: str) -> dict:
    return parse_catalog_lnb(url) if is_linovelib(url) else parse_catalog_generic(url)


# ---------- 其他網站：通用閱讀模式 ----------
def fetch_url_for(url: str) -> str:
    """維基文庫改用繁體中文的網址來讀。"""
    p = urllib.parse.urlparse(url)
    if (p.hostname or "").endswith("wikisource.org") and p.path.startswith("/wiki/"):
        return urllib.parse.urlunparse(p._replace(path="/zh-hant/" + p.path[len("/wiki/"):]))
    return url


def generic_doc(url: str):
    url = full_url(url)
    if not gr.is_public_url(url):
        raise FetchError("讀不到這個網址，請確認是網路上的小說網站")
    return url, gr.parse_html(fetch(fetch_url_for(url), referer=url))


def parse_page_generic(url: str) -> dict:
    url, root = generic_doc(url)
    content = gr.find_content(root)
    if content is None:
        raise FetchError("在這一頁找不到小說內文。這個網站可能要用瀏覽器執行程式才看得到內容，或需要登入")
    paras, images = gr.extract_blocks(content, url)
    title = gr.find_title(root)
    if paras and title and paras[0] == title:
        paras = paras[1:]  # 內文開頭重複的章節標題拿掉，標題會另外念
    nxt, prv, idx, next_is_page = gr.find_nav(root, url)
    host = urllib.parse.urlparse(url).hostname
    if not nxt or not prv:
        # 頁面上找不到「上一章／下一章」時，改用目錄的順序推算（例如維基文庫）
        try:
            chs = parse_catalog_generic(url)["volumes"][0]["chapters"]
            i = next(k for k, c in enumerate(chs) if c["id"] == gr.page_key(url))
            nxt = nxt or (chs[i + 1]["url"] if i + 1 < len(chs) else None)
            prv = prv or (chs[i - 1]["url"] if i > 0 else None)
        except (FetchError, StopIteration):
            pass
    same = bool(nxt) and (next_is_page or gr.page_key(nxt) == gr.page_key(url))
    return {
        "site": "generic", "url": url, "book": gr.find_book(root, host), "bookId": gr.book_key(url),
        "chapterId": gr.page_key(url), "title": title,
        "page": 2 if re.search(r"_\d+\.html?$", url) else 1,
        "paras": paras, "images": images,
        "next": nxt, "nextSameChapter": same, "prev": prv, "index": idx,
    }


def parse_catalog_generic(url: str) -> dict:
    url, root = generic_doc(url)
    _, _, idx, _ = gr.find_nav(root, url)
    # 先找頁面上的「目錄」連結；沒有的話試試上一層（很多網站的書頁就是章節的上一層）
    candidates = [idx] if idx else []
    parent = url.rsplit("/", 1)[0]
    if parent.count("/") >= 3:
        candidates += [parent + "/", parent]
    for cand in candidates:
        try:
            _, croot = generic_doc(cand)
        except FetchError:
            continue
        chs = gr.catalog_links(croot, cand, url)
        if len(chs) >= 3:
            return {"bookId": gr.book_key(url), "book": gr.find_book(root, urllib.parse.urlparse(url).hostname),
                    "volumes": [{"title": "目錄", "chapters": chs}]}
    raise FetchError("這個網站找不到目錄")


def resolve(url: str) -> dict:
    """判斷貼進來的是章節還是整本書；整本書就回傳第一章。"""
    url = full_url(url)
    if is_linovelib(url):
        u = normalize(url)
        if chapter_key(u):
            return {"kind": "chapter", "url": u}
        cat = parse_catalog_lnb(u)
        return {"kind": "book", "bookId": cat["bookId"], "first": cat["volumes"][0]["chapters"][0]["url"]}
    u, root = generic_doc(url)
    # 這一頁「底下」有一排章節連結（例如 /book/123/ 底下的 /book/123/1.html），就是書的目錄頁
    chs = gr.catalog_links(root, u, u.rstrip("/") + "/x")
    if len(chs) >= 5:
        return {"kind": "book", "bookId": gr.book_key(chs[0]["url"]), "first": chs[0]["url"]}
    return {"kind": "chapter", "url": u}


# ---------- linovelib 專用 ----------
def parse_page_lnb(url: str) -> dict:
    url = normalize(url)
    if not chapter_key(url):
        raise FetchError("這不是章節頁的網址。章節網址長得像 https://tw.linovelib.com/novel/1/2.html")
    page = fetch(url)
    m = re.search(r'<div id="acontent"[^>]*>(.*?)(?:<div class="cgo"|<div id="footlink")', page, re.S)
    if not m:
        if "catalog-volume" in page:
            raise FetchError("網站沒有提供這一章的內文（打開只會看到目錄），可能已被移除，請換下一章")
        raise FetchError("在這一頁找不到小說內文，網站版面可能改了")
    body = m.group(1)
    paras, images = [], []
    # 照原本的順序走過段落和插圖；插圖記下「前面有幾段文字」，網頁才知道要插在哪裡
    for tok in re.finditer(r"<p[^>]*>(.*?)</p>|<img[^>]*>", body, re.S):
        inner = tok.group(1)
        for img in re.findall(r"<img[^>]*>", tok.group(0) if inner is None else inner):
            src = re.search(r'data-src="([^"]+)"', img) or re.search(r'\ssrc="([^"]+)"', img)
            if src and not src.group(1).endswith(".svg"):
                images.append({"after": len(paras), "src": urllib.parse.urljoin(BASE + "/", html.unescape(src.group(1)))})
        if inner is not None:
            t = strip_tags(inner)
            if t:
                paras.extend(x.strip() for x in t.split("\n") if x.strip())
    t = re.search(r'<h1 id="atitle"[^>]*>(.*?)</h1>', page, re.S)
    title = strip_tags(t.group(1)) if t else ""

    nxt = read_param(page, "url_next")
    prv = read_param(page, "url_previous")
    idx = read_param(page, "url_index")
    book = read_param(page, "articlename") or ""
    here = chapter_key(url)

    def clean(u):
        if not u or not chapter_key(u):
            return None  # 指向目錄頁之類，代表沒有下一章／上一章
        return normalize(u)

    nxt, prv = clean(nxt), clean(prv)
    m_page = re.search(r"_(\d+)\.html$", url)
    return {
        "site": "linovelib",
        "url": url,
        "book": html.unescape(book),
        "bookId": here[0],
        "chapterId": here[1],
        "title": title,
        "page": int(m_page.group(1)) if m_page else 1,
        "paras": paras,
        "images": images,
        "next": nxt,
        "nextSameChapter": bool(nxt and chapter_key(nxt) == here),
        "prev": prv,
        "index": normalize(idx) if idx else f"{BASE}/novel/{here[0]}/catalog",
    }


def parse_catalog_lnb(url: str) -> dict:
    url = normalize(url)
    m = re.search(r"/novel/(\d+)", url)
    if not m:
        raise FetchError("看不出這是哪一本書，請貼書的頁面或章節網址")
    book_id = m.group(1)
    page = fetch(f"{BASE}/novel/{book_id}/catalog")
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    book = strip_tags(h1.group(1)) if h1 else ""
    volumes = []
    for vol in re.findall(r'<div class="catalog-volume">(.*?)</ul>', page, re.S):
        vt = re.search(r'class="chapter-bar[^"]*"[^>]*>.*?<h3>(.*?)</h3>', vol, re.S)
        chs = [{"title": strip_tags(t), "url": normalize(h), "id": chapter_key(h)[1]}
               for h, t in re.findall(r'<li class="chapter-li jsChapter">\s*<a href="([^"]+)"[^>]*>(.*?)</a>', vol, re.S)
               if chapter_key(h)]
        if chs:
            volumes.append({"title": strip_tags(vt.group(1)) if vt else f"第 {len(volumes) + 1} 卷", "chapters": chs})
    if not volumes:
        raise FetchError("讀不到這本書的目錄")
    return {"bookId": book_id, "book": book, "volumes": volumes}


_gallery_cache: dict = {}
ILLUS_RE = re.compile(r"插[圖图画畫]|彩[頁页圖图]")


def volume_gallery(url: str) -> dict:
    """找出目前這一卷的插圖頁，把插圖整理成一份清單，不用切換正在聽的章節就能看。"""
    url = normalize(url)
    key = chapter_key(url)
    if not key:
        raise FetchError("請先開啟一個章節")
    if not is_linovelib(url):
        raise FetchError("這個網站沒有整理好的插圖集，插圖會直接顯示在內文裡")
    cat = parse_catalog_lnb(url)
    vol = next((v for v in cat["volumes"]
                if any(chapter_key(c["url"]) and chapter_key(c["url"])[1] == key[1] for c in v["chapters"])), None)
    if vol is None:
        raise FetchError("在目錄裡找不到這一章")
    cache_key = (key[0], vol["title"])
    if cache_key in _gallery_cache:
        return _gallery_cache[cache_key]
    images, fetched = [], 0
    for ch in [c for c in vol["chapters"] if ILLUS_RE.search(c["title"])]:
        page_url = ch["url"]
        while page_url and fetched < 10:  # 一個插圖章節可能分好幾頁，最多讀 10 頁
            p = parse_page_lnb(page_url)
            fetched += 1
            images += [{"src": im["src"], "chapter": ch["title"]} for im in p["images"]]
            page_url = p["next"] if p["nextSameChapter"] else None
    result = {"volume": vol["title"], "book": cat["book"], "images": images}
    _gallery_cache[cache_key] = result
    return result


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # 保持連線，瀏覽器不用每次重新連

    def log_message(self, fmt, *args):
        pass  # 不在視窗洗版

    def send(self, code, body: bytes, ctype, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def json(self, code, obj):
        self.send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                self.send(200, (HERE / "reader.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path in ("/icon.png", "/favicon.ico"):
                self.send(200, icon_path("png").read_bytes(), "image/png")
            elif u.path == "/api/page":
                self.json(200, parse_page(q.get("url", [""])[0]))
            elif u.path == "/api/catalog":
                self.json(200, parse_catalog(q.get("url", [""])[0]))
            elif u.path == "/api/resolve":
                self.json(200, resolve(q.get("url", [""])[0]))
            elif u.path == "/api/gallery":
                self.json(200, volume_gallery(q.get("url", [""])[0]))
            elif u.path == "/api/open":
                # 用電腦預設的瀏覽器打開小說網站；只接受支援的網站，避免被拿去開其他網址
                target = full_url(q.get("url", [""])[0] or BASE + "/")
                host = urllib.parse.urlparse(target).hostname
                if host not in _seen_hosts:
                    raise FetchError("只能打開支援的小說網站")
                webbrowser.open(target)
                self.json(200, {"ok": True})
            elif u.path == "/api/img":
                data, ctype = fetch_image(q.get("src", [""])[0], q.get("ref", [""])[0])
                self.send(200, data, ctype, cache="max-age=86400")
            elif u.path == "/api/voices":
                self.json(200, {"available": edge_tts is not None, "voices": list_voices()})
            elif u.path == "/api/tts":
                audio = tts(q.get("text", [""])[0], q.get("voice", [""])[0], int(q.get("pitch", ["0"])[0] or 0))
                self.send(200, audio, "audio/mpeg")
            else:
                self.send(404, b"not found", "text/plain")
        except FetchError as e:
            self.json(400, {"error": str(e)})
        except (ConnectionError, OSError) as e:
            if isinstance(e, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
                return  # 瀏覽器已經不等這個回應了
            self.json(500, {"error": f"發生錯誤：{e}"})
        except Exception as e:  # 不讓單次錯誤把伺服器弄掛
            self.json(500, {"error": f"發生錯誤：{e}"})


class Server(ThreadingHTTPServer):
    # Windows 上開著位址重用會讓第二個伺服器也綁到同一個連接埠，搶走連線
    allow_reuse_address = False
    daemon_threads = True


def main():
    port = PORT
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    try:
        server = Server((HOST, port), Handler)
    except OSError:
        print(f"連接埠 {port} 已經在使用，可能小說電台已經開著了。直接打開 http://{HOST}:{port}")
        webbrowser.open(f"http://{HOST}:{port}")
        return
    url = f"http://{HOST}:{port}"
    print(f"小說電台已啟動：{url}")
    print("要結束的話，關掉這個視窗或按 Ctrl+C。")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
