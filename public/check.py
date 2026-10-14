"""Vercel serverless function: POST /api/check  {src, bio, password}"""
import io, os, re, json, socket, hmac, ipaddress, zipfile
import urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from http.server import BaseHTTPRequestHandler

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
UA = {"User-Agent": "Mozilla/5.0 (doc-qa)"}

# ---------------------------------------------------------------- loading
def to_download_url(src):
    m = re.search(r"docs\.google\.com/document/d/([\w-]+)", src)
    if m:
        return f"https://docs.google.com/document/d/{m.group(1)}/export?format=docx"
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?id=)([\w-]+)", src)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    if "dropbox.com" in src:
        return re.sub(r"dl=0", "dl=1", src) if "dl=" in src else src + ("&" if "?" in src else "?") + "dl=1"
    return src



def check_url(u):
    p = urllib.parse.urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise SystemExit("Link sahi nahi hai.")
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except OSError:
        raise SystemExit("Link ka address nahi mila.")
    for ai in infos:
        ip = ipaddress.ip_address(ai[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise SystemExit("Yeh link allowed nahi hai.")


class Redir(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(Redir)


def http_get(url, limit=15_000_000):
    check_url(url)
    r = OPENER.open(urllib.request.Request(url, headers=UA), timeout=8)
    data = r.read(limit + 1)
    if len(data) > limit:
        raise SystemExit("File bohot badi hai.")
    return data, r.headers


def load_bytes(src):
    if not re.match(r"https?://", src):
        raise SystemExit("Doc ka link (https://...) paste karein.")
    url = to_download_url(src)
    data, headers = http_get(url)
    cd = headers.get("Content-Disposition", "") or ""
    name = ""
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd)
    if m:
        name = urllib.parse.unquote(m.group(1))
    else:
        m = re.search(r'filename="?([^";]+)', cd)
        if m:
            name = m.group(1)
    if not name:
        name = os.path.basename(urllib.parse.urlparse(url).path)
    if not data.startswith(b"PK"):
        raise SystemExit("Link se .docx nahi mili (shayad login chahiye ya sharing 'Anyone with the link' nahi hai).")
    return data, name


def read_docx(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    root = ET.fromstring(z.read("word/document.xml"))
    lines = []
    for p in root.iter(W + "p"):
        t = ""
        for el in p.iter():
            if el.tag == W + "t":
                t += el.text or ""
            elif el.tag in (W + "tab", W + "br"):
                t += " "
        t = t.strip()
        if t:
            lines.append(t)
    links = []
    try:
        rels = ET.fromstring(z.read("word/_rels/document.xml.rels"))
        for r in rels:
            if r.get("TargetMode") == "External" and r.get("Target", "").startswith("http"):
                links.append(r.get("Target"))
    except KeyError:
        pass
    return lines, links

# ---------------------------------------------------------------- analysis
FIRST = set("me my mine myself we us our ours ourselves".split())
THIRD = set("he him his himself she her hers herself they them their theirs themselves".split())
STOP = {"Click", "Episode", "Post"}


def body_text(body):
    return [l for l in body if not re.match(r"https?:", l, re.I) and not re.match(r"click\b", l, re.I)]


def person(body):
    toks = re.findall(r"[A-Za-z\u2019']+", " ".join(body_text(body)))
    f = t = 0
    for w in toks:
        l = w.lower()
        if w == "I" or re.match(r"^I['\u2019](m|ll|ve|d)$", w) or re.match(r"^we['\u2019](re|ll|ve|d)$", w, re.I) or (w != "US" and l in FIRST):
            f += 1
        elif l in THIRD:
            t += 1
    return f, t


def find_names(body, known):
    out = []
    for sen in re.split(r"(?<=[.!?])\s+", " ".join(body_text(body))):
        for m in re.finditer(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", sen):
            w = m.group(0).split()
            if w[0] in STOP:
                continue
            if m.start() == 0 and len(w) == 1 and w[0] not in known:
                continue
            out.append(m.group(0))
    return out


def analyse(lines, filename, links):
    posts, cur = [], None
    for l in lines:
        if re.match(r"^episode\s*\d+", l, re.I):
            cur = {"title": l, "body": []}
            posts.append(cur)
        elif cur:
            cur["body"].append(l)
    res = []  # (status, title, [details])
    if not posts:
        return posts, [("ERROR", "Titles", ['Koi "Episode X - Post Y:" title nahi mila.'])], []

    for p in posts:
        m = re.match(r"^Episode\s*(\d+)\s*[-\u2013\u2014]\s*Post\s*(\d+)\s*:?\s*$", p["title"], re.I)
        p["ep"], p["pn"] = (int(m.group(1)), int(m.group(2))) if m else (None, None)
        p["exact"] = bool(re.match(r"^Episode \d+ - Post \d+:$", p["title"]))
        p["pr"] = person(p["body"])
    known = set()
    for p in posts:
        for n in find_names(p["body"], set()):
            if " " in n:
                known.add(n.split()[0])
    for p in posts:
        p["nm"] = list(dict.fromkeys(find_names(p["body"], known)))

    # title format
    bad = [p for p in posts if not p["exact"]]
    res.append(("ERROR" if bad else "OK", "Title format",
                [f'Galat: "{p["title"]}" (sahi: Episode 1 - Post 1:)' for p in bad] or ['Sab titles "Episode X - Post Y:" format mein hain.']))

    # episode number
    eps = list(dict.fromkeys(p["ep"] for p in posts if p["ep"] is not None))
    d, st = [], "OK"
    if len(eps) > 1:
        st = "ERROR"
        d.append("Titles mein alag alag episode numbers: " + ", ".join(map(str, eps)))
        d += [f'Post {p["pn"]}: "{p["title"]}"' for p in posts if p["ep"] != eps[0]]
    elif eps:
        d.append(f"Sab titles mein Episode {eps[0]} hai.")
    fm = re.search(r"episode\D*(\d+)", filename or "", re.I)
    if fm and eps:
        if int(fm.group(1)) != eps[0]:
            st = "ERROR"
            d.append(f"File name mein Episode {fm.group(1)} hai lekin titles mein Episode {eps[0]}.")
        else:
            d.append("File name ka episode number bhi match karta hai.")
    res.append((st, "Episode number", d))

    # post numbering
    nums = [p["pn"] for p in posts if p["pn"] is not None]
    d = []
    dup = sorted({x for x in nums if nums.count(x) > 1})
    miss = [i for i in range(1, max(nums + [0]) + 1) if i not in nums]
    if dup:
        d.append("Duplicate post number: " + ", ".join(map(str, dup)))
    if miss:
        d.append("Missing post number: " + ", ".join(map(str, miss)))
    if not dup and not miss and nums != list(range(1, len(nums) + 1)):
        d.append("Posts ki tarteeb 1,2,3 ke mutabiq nahi hai.")
    res.append(("ERROR" if d else "OK", "Post numbering", d or [f"Post 1 se {len(nums)} tak sahi tarteeb mein hain."]))

    # person
    for p in posts:
        f, t = p["pr"]
        k = len(p["nm"])
        p["lab"] = "Mixed" if f and (t or k) else "First person" if f else "Third person" if (t or k) else "Unclear"
    cnt = Counter(p["lab"] for p in posts)
    main = cnt.most_common(1)[0][0]
    odd = [p for p in posts if p["lab"] != main]
    d = [f"Zyada-tar content: {main} ({cnt[main]} / {len(posts)} posts)."] + [f'Post {p["pn"]}: {p["lab"]}' for p in odd]
    res.append(("CHECK" if odd else "OK", "First person ya third person", d))

    # names
    tally = Counter(n for p in posts for n in p["nm"])
    d, st = [], "OK"
    if not tally:
        st = "ERROR"
        d.append("Kisi post mein koi name nahi mila.")
    else:
        d.append("Names: " + ", ".join(f"{n} ({c} / {len(posts)} posts)" for n, c in tally.most_common()))
        none = [p for p in posts if not p["nm"]]
        if none:
            st = "CHECK"
            d.append("In posts mein koi name nahi: " + ", ".join(f'Post {p["pn"]}' for p in none))
        if len(tally) > 1:
            st = "CHECK"
            d.append("Ek se zyada alag names mile, check kar lein.")
    res.append((st, "Names", d))

    # bio link presence
    urls = [next((l for l in p["body"] if re.match(r"https?:", l, re.I)), None) for p in posts]
    uq = list(dict.fromkeys(u for u in urls if u))
    d, st = [], "OK"
    nol = [p for p, u in zip(posts, urls) if not u]
    if nol:
        st = "ERROR"
        d.append("In posts mein bio link nahi: " + ", ".join(f'Post {p["pn"]}' for p in nol))
    if len(uq) > 1:
        st = "ERROR"
        d.append("Alag alag links mile, sab posts mein same hona chahiye.")
    d += uq
    res.append((st, "Bio link", d))
    return posts, res, uq

# ---------------------------------------------------------------- bio link (basic)
def bio_result(url, names):
    try:
        data, _ = http_get(url, 3_000_000)
        html = data.decode("utf-8", "replace")
    except SystemExit as e:
        return {"url": url, "st": "ERROR", "title": "", "lines": [], "msg": str(e)}
    except Exception as e:
        return {"url": url, "st": "ERROR", "title": "", "lines": [], "msg": "Bio link khul nahi saka: %s" % e}
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    title = m.group(1).strip() if m else ""
    text = re.sub(r"<(script|style).*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    lines = [l.strip()[:100] for l in text.splitlines() if l.strip()][:8]
    low = (text + " " + title).lower()
    hit = [n for n in names if n.lower() in low]
    if hit:
        st, msg = "OK", "Page par doc ka naam mila: " + ", ".join(hit)
    else:
        st = "CHECK"
        msg = "Basic check mein naam nahi mila. Bio.link JavaScript se load hota hai, isliye link khud khol kar naam dekh lein."
    return {"url": url, "st": st, "title": title, "lines": lines, "msg": msg}


def run_check(src, want_bio):
    data, filename = load_bytes(src)
    lines, rels = read_docx(data)
    posts, res, urls = analyse(lines, filename, rels)
    out = {"file": filename, "count": len(posts),
           "checks": [{"st": st, "title": t, "details": d} for st, t, d in res],
           "posts": [{"title": p["title"], "label": p["lab"], "names": p["nm"], "exact": p["exact"]} for p in posts],
           "bio": []}
    if want_bio and urls:
        names = [n for n, _ in Counter(n for p in posts for n in p["nm"]).most_common()]
        out["bio"] = [bio_result(u, names) for u in urls[:3]]
    return out


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send(200, {"ok": True})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            key = os.environ.get("ACCESS_KEY")
            if key and not hmac.compare_digest(str(req.get("password", "")), key):
                out = {"error": "Password galat hai."}
            else:
                out = run_check(str(req.get("src", "")).strip(), bool(req.get("bio")))
        except SystemExit as e:
            out = {"error": str(e)}
        except Exception as e:
            out = {"error": "%s: %s" % (type(e).__name__, e)}
        self._send(200, out)
