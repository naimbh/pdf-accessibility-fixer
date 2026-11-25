"""Verify a fixed PDF against its original. Exit code 0 = PASS, 1 = FAIL.

usage: python3 verify.py original.pdf fixed.pdf [--order N] [--dpi 60]
Checks:
  1. same page count and every page renders pixel-identical (max channel difference 0)
  2. page content bytes: only marked-content tag labels may differ (reported, not required)
  3. tagged (StructTreeRoot + MarkInfo), language set, title set and shown in the title bar
  4. exactly one H1; no Figure without alt text
  5. no software/tool names left in the metadata
  6. space ratio of the tag text (warns when words may run together)
--order N prints the first N tag elements with their text, to read the screen-reader order."""
import sys, re, collections, pikepdf, pymupdf, pdfplumber, numpy as np

orig, fixed = sys.argv[1], sys.argv[2]
N = int(sys.argv[sys.argv.index("--order") + 1]) if "--order" in sys.argv else 0
DPI = int(sys.argv[sys.argv.index("--dpi") + 1]) if "--dpi" in sys.argv else 60
fails, notes = [], []

a, b = pymupdf.open(orig), pymupdf.open(fixed)
if len(a) != len(b): fails.append(f"page count {len(a)} vs {len(b)}")
else:
    for i in range(len(a)):
        pa = a[i].get_pixmap(dpi=DPI); pb = b[i].get_pixmap(dpi=DPI)
        if (pa.width, pa.height) != (pb.width, pb.height): fails.append(f"page {i+1}: size differs"); continue
        d = int(np.abs(np.frombuffer(pa.samples, np.uint8).astype(int) - np.frombuffer(pb.samples, np.uint8).astype(int)).max())
        if d: fails.append(f"page {i+1}: pixels differ (max {d})")

po, pf = pikepdf.open(orig), pikepdf.open(fixed)
TAG = re.compile(rb"/[A-Za-z0-9_#.\-]+\s*<<\s*/MCID\s+\d+\s*>>\s*BDC|/[A-Za-z0-9_#.\-]+\s*BMC|EMC")
def body(pg):
    c = pg.obj.Contents; c = list(c) if isinstance(c, pikepdf.Array) else [c]
    return b"".join(s.read_bytes() for s in c)
if len(po.pages) == len(pf.pages):
    tag_only = other = 0
    for x, y in zip(po.pages, pf.pages):
        bx, by = body(x), body(y)
        if bx == by: continue
        if TAG.sub(b"", bx).split() == TAG.sub(b"", by).split(): tag_only += 1
        else: other += 1
    notes.append(f"content bytes: {len(po.pages) - tag_only - other} pages unchanged, {tag_only} with tag-label edits only, {other} re-serialized (pixel check above is authoritative)")

r = pf.Root
if "/StructTreeRoot" not in r or not bool(r.get("/MarkInfo", {}).get("/Marked", False)): fails.append("not tagged")
if not str(r.get("/Lang", "")): fails.append("no document language")
if not b.metadata.get("title"): fails.append("no title")
if not bool(r.get("/ViewerPreferences", {}).get("/DisplayDocTitle", False)): fails.append("DisplayDocTitle not set")

txt = collections.defaultdict(str)
with pdfplumber.open(fixed) as pl:
    for i, pg in enumerate(pl.pages):
        for ch in pg.chars:
            if ch.get("mcid") is not None: txt[(i, ch["mcid"])] += ch["text"]
pages = {pg.objgen: i for i, pg in enumerate(pf.pages)}
rm = r.StructTreeRoot.get("/RoleMap", {}) if "/StructTreeRoot" in r else {}
c = collections.Counter(); noalt = 0; seq = []
def text_of(n, pg=None):
    out = ""
    if "/K" not in n: return out
    if "/Pg" in n: pg = pages.get(n.Pg.objgen, pg)
    for k in (n.K if isinstance(n.K, pikepdf.Array) else [n.K]):
        if isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/MCR": out += txt.get((pages.get(k.Pg.objgen, pg) if "/Pg" in k else pg, int(k.MCID)), "")
        elif isinstance(k, int): out += txt.get((pg, int(k)), "")
        elif isinstance(k, pikepdf.Dictionary) and "/S" in k: out += text_of(k, pg)
    return out
def walk(n, d=0):
    global noalt
    if isinstance(n, pikepdf.Array):
        for k in n: walk(k, d)
        return
    if not isinstance(n, pikepdf.Dictionary) or "/S" not in n: return
    s = str(n.S); s2 = str(rm.get(s, s)); c[s2] += 1
    if s2 == "/Figure" and not str(n.get("/Alt", "")).strip(): noalt += 1
    if s2 not in ("/Document", "/L", "/LI", "/TR", "/Table", "/Link", "/Span", "/Lbl", "/Part", "/Sect", "/Art", "/Div", "/NonStruct"):
        seq.append((d, s2, ("[alt] " + str(n.get("/Alt", ""))) if s2 == "/Figure" else (str(n.ActualText) if "/ActualText" in n else text_of(n))))
    if "/K" in n: walk(n.K, d + 1)
if "/StructTreeRoot" in r: walk(r.StructTreeRoot.K)
if c["/H1"] != 1: fails.append(f"{c['/H1']} H1 headings (expected exactly 1)")
if noalt: fails.append(f"{noalt} figures without alt text")

meta = " ".join(str(v) for k, v in pf.docinfo.items() if k not in ("/Title", "/Author", "/Subject", "/Keywords"))
xmp = r.Metadata.read_bytes().decode("utf8", "ignore") if "/Metadata" in r else ""
xmp_tools = re.findall(r"(?is)<(?:\w+:)?(?:Producer|CreatorTool|softwareAgent)\b[^>]*>(.*?)<", xmp) + re.findall(r'(?i)(?:Producer|CreatorTool|softwareAgent|xmptk)="([^"]+)"', xmp)
tools = re.findall(r"(?i)(pikepdf|ocrmypdf|tesseract|itext|pdftex|distiller|ghostscript|quartz|print to pdf|indesign|acrobat|pdf library|libreoffice|openoffice|microsoft|pymupdf|chrome|skia|python|qpdf|1t3xt|powerpoint|latex)", meta)
if tools or xmp_tools: fails.append(f"tool traces in metadata: {sorted(set(t.lower() for t in tools)) + xmp_tools[:3]}")

alltext = " ".join(t for _, s, t in seq if not t.startswith("[alt]"))
letters = sum(ch.isalpha() for ch in alltext)
if letters > 500 and alltext.count(" ") / letters < 0.05:
    notes.append("WARNING: tag text has almost no spaces; run spaced_actualtext.py")

print(f"{'PASS' if not fails else 'FAIL'}  {fixed.split('/')[-1]}  pages {len(a)}/{len(b)}")
print("  tags:", dict(c.most_common(16)))
print("  title:", b.metadata.get("title"), "| lang:", r.get("/Lang"), "| bookmarks:", len(b.get_toc()))
for n_ in notes: print("  " + n_)
for f_ in fails: print("  FAIL: " + f_)
for d, s, t in seq[:N]: print("   " + "  " * min(d, 4), s, repr(t[:110]))
sys.exit(1 if fails else 0)
