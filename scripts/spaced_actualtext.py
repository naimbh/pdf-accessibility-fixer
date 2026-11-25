"""Give text elements correctly spaced ActualText when the PDF's text layer has no space characters
(common in LaTeX and some publisher PDFs: screen readers may otherwise hear "Thefieldofmachinelearning").
Only the tag tree changes, so the file stays pixel-identical.

usage: python3 spaced_actualtext.py in.pdf out.pdf [P,LBody,Note,BlockQuote,Caption,TD,TH,H1,H2,H3,H4,H5]
Prints the line-end hyphen joins it made so they can be reviewed."""
import sys, re, collections, pikepdf, pymupdf, pdfplumber

src, dst = sys.argv[1], sys.argv[2]
TAGS = {"/" + t for t in (sys.argv[3] if len(sys.argv) > 3 else "P,LBody,Note,BlockQuote,Caption,TD,TH,H1,H2,H3,H4,H5").split(",")}
try: DICT = {w.strip().lower() for w in open("/usr/share/dict/words")}
except OSError: DICT = set()
EXTRA = {"database", "databases", "timestamp", "timestamps", "chatbot", "chatbots", "wheelchair", "wheelchairs", "countdown",
         "explainability", "interpretability", "undertaken", "familiarity", "grandmaster", "online", "website", "websites", "email"}
SUFF = ["", "ability", "ibility", "ity", "ize", "ized", "izing", "ization", "en", "n", "s", "es", "ed", "d", "ing", "ers", "er",
        "ly", "ies", "ied", "ness", "ment", "ments", "al", "ally", "ation", "ations"]
LIG = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"}

def inflected(word):
    for suf in SUFF:
        if suf and not word.endswith(suf): continue
        stem = word[:len(word) - len(suf)] if suf else word
        for cand in (stem, stem + "e", stem[:-1] if len(stem) > 2 and stem[-1] == stem[-2] else stem, stem + "y"):
            if len(cand) >= 3 and cand in DICT: return True
    return False
def isword(t): return t in EXTRA or inflected(t)
def keep_hyphen(prev, w, x, y):
    if re.search(r"https?:|www\.", prev + w): return True                       # URLs: hyphens are real
    if re.fullmatch(r"[A-Z]{2,}\w*", re.sub(r"\W", "", prev.split("-")[-1])): return True   # AI-powered, LLM-driven
    rest = w.strip("-‘’“”'\"(").split("-")
    if len(rest) > 1: return not isword(x + re.sub(r"\W", "", rest[0]).lower())   # end-to-end vs expect-ancy-value
    if x in {"self", "non", "well", "anti", "quasi", "pseudo", "cross", "semi"} and isword(y): return True
    if isword(x + y): return False                                               # a dictionary word: line-break hyphen
    if y in {"in", "on", "up", "of", "to", "by", "hoc"} or x in {"in", "on", "post"}: return True
    return isword(x) and isword(y)                                               # two real words: compound

p = pikepdf.open(src); pages = {pg.objgen: i for i, pg in enumerate(p.pages)}; rm = p.Root.StructTreeRoot.get("/RoleMap", {})
box = collections.defaultdict(dict)
with pdfplumber.open(src) as pl:
    for i, pg in enumerate(pl.pages):
        for c in pg.chars:
            m = c.get("mcid")
            if m is None: continue
            b = box[i].setdefault(m, [1e9, 1e9, -1e9, -1e9])
            b[0] = min(b[0], c["x0"]); b[1] = min(b[1], c["top"]); b[2] = max(b[2], c["x1"]); b[3] = max(b[3], c["bottom"])
doc = pymupdf.open(src)
words = {i: doc[i].get_text("words") for i in range(len(doc))}
offs = {i: (doc[i].cropbox.x0 - doc[i].mediabox.x0, doc[i].mediabox.y1 - doc[i].cropbox.y1) for i in range(len(doc))}
used = collections.defaultdict(set); JOINS = []

def own_mcrs(n, pg):
    out = []
    if "/Pg" in n: pg = pages.get(n.Pg.objgen, pg)
    if "/K" not in n: return out
    for k in (n.K if isinstance(n.K, pikepdf.Array) else [n.K]):
        if isinstance(k, int): out.append((pg, int(k)))
        elif isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/MCR": out.append((pages.get(k.Pg.objgen, pg) if "/Pg" in k else pg, int(k.MCID)))
        elif isinstance(k, pikepdf.Dictionary) and "/S" in k and str(k.S) in ("/Span", "/Link", "/Lbl", "/LBody"): out += own_mcrs(k, pg)
    return out

def spaced(n, pg):
    toks = []
    for pi, m in own_mcrs(n, pg):
        b = box.get(pi, {}).get(m)
        if b is None: continue
        ox, oy = offs[pi]
        ws = [(j, w) for j, w in enumerate(words[pi]) if j not in used[pi]
              and b[0] - 1 <= (w[0] + w[2]) / 2 + ox <= b[2] + 1 and b[1] - 1 <= (w[1] + w[3]) / 2 + oy <= b[3] + 1]
        ws.sort(key=lambda t: (t[1][1] + t[1][3]) / 2); rows = []
        for t in ws:                                     # visual lines (superscripts stay in their line)
            cy = (t[1][1] + t[1][3]) / 2
            if rows and abs(cy - rows[-1][0]) < 0.6 * max(3, t[1][3] - t[1][1]): rows[-1][1].append(t)
            else: rows.append([cy, [t]])
        for j, w in [t for r in rows for t in sorted(r[1], key=lambda t: t[1][0])]:
            used[pi].add(j); toks.append(("".join(LIG.get(ch, ch) for ch in w[4]), (pi, w[5], w[6])))
    out = ""
    for k, (w, ln) in enumerate(toks):
        if out.endswith("-") and k and toks[k - 1][1] != ln and re.match(r"[a-z]", w) and re.search(r"[A-Za-z]-$", out):
            prev = re.search(r"(\S+)-$", out).group(1)
            x = re.sub(r"\W", "", prev.split("-")[-1]).lower(); y = re.sub(r"\W", "", w).lower()
            keep = keep_hyphen(prev, w, x, y)
            out = (out if keep else out[:-1]) + w; JOINS.append(prev + ("-" if keep else "") + w)
        else: out += (" " if out else "") + w
    return out

count = collections.Counter()
def walk(n, pg=None):
    if isinstance(n, pikepdf.Array):
        for k in n: walk(k, pg)
        return
    if not isinstance(n, pikepdf.Dictionary) or "/S" not in n: return
    if "/Pg" in n: pg = pages.get(n.Pg.objgen, pg)
    s = str(n.S); s2 = str(rm.get(s, s))
    if (s in TAGS or s2 in TAGS) and "/ActualText" not in n:
        t = spaced(n, pg)
        if t: n.ActualText = pikepdf.String(t); count[s2] += 1
    if "/K" in n: walk(n.K, pg)
walk(p.Root.StructTreeRoot.K)
p.save(dst)
print("ActualText set on", sum(count.values()), dict(count))
print("line-end hyphen joins (review these):", JOINS)
