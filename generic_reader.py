"""通用閱讀模式：任何小說網站的章節網址丟進來，盡量找出內文、標題、上一章／下一章和目錄。

做法跟瀏覽器的「閱讀模式」類似：把網頁拆成節點，挑出文字最多、連結最少的那一塊當內文，
再從連結文字（下一章、下一頁、→……）找出上一章和下一章。
用 JavaScript 才會顯示內容、要登入、或把字加密的網站，這個方法讀不到。
"""

import html
import ipaddress
import re
import socket
import urllib.parse
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
SKIP = {"script", "style", "noscript", "iframe", "svg", "form", "select", "button", "textarea", "head", "template"}
BLOCK = {"p", "div", "br", "li", "dd", "dt", "tr", "section", "article", "blockquote", "pre", "table",
         "h1", "h2", "h3", "h4", "h5", "h6", "center", "ul", "ol", "dl"}
BAD_RE = re.compile(r"nav|menu|footer|header(?!template)|comment|sidebar|recommend|related|share|banner|breadcrumb"
                    r"|copyright|login|toolbar|rank|noprint|search|advert|\bads?\b", re.I)
GOOD_RE = re.compile(r"content|chapter|article|text|read|novel|txt|book|main|entry|parser-output", re.I)
JUNK_RE = re.compile(r"本章未完|點擊下一頁|点击下一页|請收藏|请收藏|最新網址|最新网址|手機閱讀|手机阅读|加入書籤|加入书签"
                     r"|一秒記住|一秒记住|記住本站|记住本站|章節錯誤|章节错误|免費閱讀|免费阅读")
NEXT_TEXT = re.compile(r"下一[章頁页回節节篇话話]|^下[章頁页]$|^next|→|»|下一頁?$", re.I)
PREV_TEXT = re.compile(r"上一[章頁页回節节篇话話]|^上[章頁页]$|^prev|←|«", re.I)
INDEX_TEXT = re.compile(r"目[錄录]|章[節节]列表|返回[書书]頁|[書书]頁|^index$", re.I)


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag, self.attrs, self.children, self.parent = tag, dict(attrs or {}), [], parent

    def cls(self):
        return f'{self.attrs.get("id", "")} {self.attrs.get("class", "")}'

    def iter(self):
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.iter()


class _Builder(HTMLParser):
    """容錯的 HTML 解析：沒關好的標籤也能大致還原結構。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, [(k, v or "") for k, v in attrs], self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, [(k, v or "") for k, v in attrs], self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse_html(text: str) -> Node:
    b = _Builder()
    b.feed(text)
    return b.root


def _text(node, skip_links=False):
    out = []
    for c in node.children:
        if isinstance(c, str):
            out.append(c)
        elif c.tag not in SKIP and not (skip_links and c.tag == "a"):
            out.append(_text(c, skip_links))
    return "".join(out)


def clean(s: str) -> str:
    return re.sub(r"[\s　\xa0]+", " ", s).strip()


def _measure(root):
    """每個節點的（文字長度、連結文字長度），由下往上算一次。"""
    stats = {}

    def walk(n):
        if n.tag in SKIP:
            stats[id(n)] = (0, 0)
            return 0, 0
        t = l = 0
        for c in n.children:
            if isinstance(c, str):
                t += len(c.strip())
            else:
                ct, cl = walk(c)
                t += ct
                l += ct if c.tag == "a" else cl
        stats[id(n)] = (t, l)
        return t, l

    walk(root)
    return stats


def find_content(root):
    stats = _measure(root)
    best, best_score = None, 0
    for n in root.iter():
        if n.tag not in ("div", "article", "section", "main", "td", "dd", "body") or id(n) not in stats:
            continue  # script／noscript 裡面的節點不算
        t, l = stats[id(n)]
        if t < 50:
            continue
        score = t - 3 * l
        c = n.cls()
        if GOOD_RE.search(c):
            score *= 1.4
        if BAD_RE.search(c):
            score *= 0.2
        if score > best_score:
            best, best_score = n, score
    if best is None:
        return None
    # 往下走到「真正裝著內文」的那一層：某個子節點幾乎包辦全部文字時就換成它
    while True:
        t, _ = stats[id(best)]
        kids = [c for c in best.children if isinstance(c, Node) and c.tag not in SKIP]
        top = max(kids, key=lambda c: stats[id(c)][0], default=None)
        if top is None or stats[id(top)][0] < 0.85 * t or top.tag in ("p", "a", "span", "font", "b", "strong"):
            return best
        best = top


def extract_blocks(node, base_url):
    """把內文拆成段落和插圖，順序跟網頁上一樣。"""
    paras, images, buf = [], [], []

    def flush():
        line = clean("".join(buf))
        buf.clear()
        if line and not JUNK_RE.search(line):
            paras.append(line)

    def walk(n):
        for c in n.children:
            if isinstance(c, str):
                buf.append(c)
                continue
            if c.tag in SKIP or (c.tag != "p" and BAD_RE.search(c.cls())):
                continue
            if c.tag == "img":
                src = c.attrs.get("data-src") or c.attrs.get("data-original") or c.attrs.get("src") or ""
                w = c.attrs.get("width", "")
                if src and not src.startswith("data:") and not re.search(r"\.(svg|gif)(\?|$)", src) and not (w.isdigit() and int(w) < 60):
                    flush()
                    images.append({"after": len(paras), "src": urllib.parse.urljoin(base_url, html.unescape(src))})
                continue
            block = c.tag in BLOCK
            if block:
                flush()
            if c.tag != "br":
                walk(c)
            if block:
                flush()

    walk(node)
    flush()
    return paras, images


def _links(root, base_url):
    for a in root.iter():
        if a.tag != "a":
            continue
        href = a.attrs.get("href", "").strip()
        if not href or href.startswith(("javascript:", "#", "mailto:")):
            continue
        yield a, clean(_text(a)), urllib.parse.urljoin(base_url, html.unescape(href))


def find_nav(root, base_url):
    nxt = prv = idx = None
    for n in root.iter():  # <link rel="next"> 最可靠
        if n.tag == "link" and n.attrs.get("href"):
            rel = n.attrs.get("rel", "").lower()
            if rel == "next" and not nxt:
                nxt = urllib.parse.urljoin(base_url, n.attrs["href"])
            elif rel in ("prev", "previous") and not prv:
                prv = urllib.parse.urljoin(base_url, n.attrs["href"])
    next_is_page = False
    for a, t, url in _links(root, base_url):
        around = a.cls() + " " + (a.parent.cls() if a.parent else "")
        if url.split("#")[0] == base_url.split("#")[0]:
            continue
        if not idx and INDEX_TEXT.search(t) and len(t) <= 8:
            idx = url
        is_next = (len(t) <= 12 and NEXT_TEXT.search(t)) or (t and re.search(r"next", around, re.I) and len(t) <= 40)
        is_prev = (len(t) <= 12 and PREV_TEXT.search(t)) or (t and re.search(r"prev", around, re.I) and len(t) <= 40)
        if is_next and not nxt:
            nxt, next_is_page = url, bool(re.search(r"[頁页]", t) and not re.search(r"[章回]", t))
        elif is_prev and not is_next and not prv:
            prv = url
    return nxt, prv, idx, next_is_page


def page_key(url: str) -> str:
    """同一章的分頁（123.html、123_2.html）算同一個章節。"""
    p = urllib.parse.urlparse(url)
    path = re.sub(r"_\d+(\.html?)$", r"\1", p.path)
    return f"{p.hostname}{urllib.parse.unquote(path)}"


def book_key(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return f"{p.hostname}{urllib.parse.unquote(p.path.rsplit('/', 1)[0])}"


def find_title(root):
    for n in root.iter():
        if n.tag == "h1":
            t = clean(_text(n))
            if t:
                return t
    for n in root.iter():
        if n.tag == "title":
            return clean(re.split(r"\s[-_|–—]\s|[_|]", _text(n))[0])
    return ""


def find_book(root, host):
    for n in root.iter():
        if n.tag == "meta" and n.attrs.get("property") in ("og:novel:book_name", "og:novel:bookname"):
            return clean(n.attrs.get("content", ""))
    for n in root.iter():
        if n.tag == "title":
            parts = [clean(x) for x in re.split(r"\s[-_|–—]\s|[_|]", _text(n)) if clean(x)]
            if len(parts) >= 2:
                return parts[1]
    return host


def catalog_links(root, base_url, prefix_url):
    """目錄頁上的章節連結：跟章節網址在同一層的連結，照順序排好。"""
    prefix = urllib.parse.urlparse(prefix_url)
    dir_path = prefix.path.rsplit("/", 1)[0] + "/"
    seen, order = {}, []
    for _, t, url in _links(root, base_url):
        p = urllib.parse.urlparse(url)
        if p.hostname != prefix.hostname or not p.path.startswith(dir_path) or not t or INDEX_TEXT.search(t):
            continue
        key = url.split("#")[0]
        if key in seen:
            order.remove(key)  # 很多網站上方先列「最新章節」，後面才是完整目錄，保留後面那份的順序
        seen[key] = t
        order.append(key)
    return [{"title": seen[u], "url": u, "id": page_key(u)} for u in order]


def is_public_url(url: str) -> bool:
    """只讀網路上的網站，不讓它去碰你電腦或區域網路裡的位址。"""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except OSError:
        return False
    return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


def decode(raw: bytes, ctype: str) -> str:
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I) or re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', raw[:4000], re.I)
    enc = m.group(1) if m else None
    if isinstance(enc, bytes):
        enc = enc.decode("ascii", "ignore")
    tries = ([enc] if enc else []) + ["utf-8", "gb18030", "big5"]
    for e in tries:
        e = {"gb2312": "gb18030", "gbk": "gb18030"}.get(e.lower(), e)
        try:
            return raw.decode(e)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")
