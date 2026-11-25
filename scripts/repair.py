"""Repair an ALREADY-TAGGED PDF in place (Word, PowerPoint, InDesign, LaTeX exports with tags).
Pixel-identical by construction: only the tag tree, marked-content labels and metadata change.
Page content is edited at the byte level, and only the marked-content tags of elements turned into artifacts.

usage: python3 repair.py cfg.json
cfg keys:
  src, dst, title, author (optional), lang (default "en-US")
  alt:            {"<figure number from fig_inventory.py>": "alt text" | "" (decorative -> artifact)}
  retag:          {"<element number from inspect_tags.py>": "H1" | "H2" | "P" | "Caption" | ...}
  rolemap:        {"Heading_1": "H2", "Chapter_Title": "H1", ...}   (fix custom style -> standard role mapping)
  artifact_elems: [<element number>, ...]   (e.g. running heads tagged as content)
  keep_auto_alt:  false (default) -> strips "Description automatically generated" tails from existing alt text
"""
import sys, os, json, re, zlib, pikepdf
from pikepdf import Name, String, Dictionary
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrub_metadata import scrub

cfg = json.load(open(sys.argv[1]))
p = pikepdf.open(cfg["src"]); root = p.Root; st = root.StructTreeRoot
rm = st.get("/RoleMap", pikepdf.Dictionary()); pages = {pg.objgen: i for i, pg in enumerate(p.pages)}

# depth-first element list (same numbering as inspect_tags.py) and figure list (same as fig_inventory.py)
els, figs = [], []
def walk(n, parent, pg=None):
    if isinstance(n, pikepdf.Array):
        for k in n: walk(k, parent, pg)
        return
    if not isinstance(n, pikepdf.Dictionary) or "/S" not in n: return
    if "/Pg" in n: pg = pages.get(n.Pg.objgen, pg)
    els.append((n, parent))
    s = str(n.S)
    if s == "/Figure" or str(rm.get(s, "")) == "/Figure":
        figs.append((n, parent)); return
    if "/K" in n: walk(n.K, n, pg)
walk(st.K, st)
# NOTE: inspect_tags.py descends into figures too; repair numbering of elements must match it, so re-walk fully
els = []
def walk_all(n, parent):
    if isinstance(n, pikepdf.Array):
        for k in n: walk_all(k, parent)
        return
    if not isinstance(n, pikepdf.Dictionary) or "/S" not in n: return
    els.append((n, parent))
    if "/K" in n: walk_all(n.K, n)
walk_all(st.K, st)

def mcrs(n, pg=None):
    out = []
    if "/Pg" in n: pg = pages.get(n.Pg.objgen, pg)
    if "/K" not in n: return out
    for k in (n.K if isinstance(n.K, pikepdf.Array) else [n.K]):
        if isinstance(k, int): out.append((pg, int(k)))
        elif isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/MCR": out.append((pages.get(k.Pg.objgen, pg) if "/Pg" in k else pg, int(k.MCID)))
        elif isinstance(k, pikepdf.Dictionary) and "/S" in k: out += mcrs(k, pg)
    return out

def detach(el, parent):
    k = parent.K
    parent.K = pikepdf.Array([x for x in k if not (isinstance(x, pikepdf.Dictionary) and x.objgen == el.objgen)]) if isinstance(k, pikepdf.Array) else pikepdf.Array()

stats = {"alt_set": 0, "auto_cleaned": 0, "decorative": 0, "retagged": 0, "artifact_elems": 0}
to_artifact = {}
def make_artifact(el, parent):
    for pg, m in mcrs(el):
        if pg is not None: to_artifact.setdefault(pg, set()).add(m)
    detach(el, parent)

# role map fixes
for k, v in cfg.get("rolemap", {}).items():
    rm[Name("/" + k.lstrip("/"))] = Name("/" + v.lstrip("/"))
if cfg.get("rolemap"): st.RoleMap = rm
# retag elements
for idx, role in cfg.get("retag", {}).items():
    el, _ = els[int(idx)]; el.S = Name("/" + role.lstrip("/")); stats["retagged"] += 1
# figures: alt text or decorative
AUTO = re.compile(r"\s*\n*\s*(AI-generated content may be incorrect\.?|Description automatically generated( with (medium|low|high) confidence)?)\s*$", re.I)
alts = {int(k): v for k, v in cfg.get("alt", {}).items()}
for i, (el, parent) in enumerate(figs):
    if i in alts:
        if alts[i] == "": make_artifact(el, parent); stats["decorative"] += 1
        else: el.Alt = String(alts[i]); stats["alt_set"] += 1
    elif "/Alt" in el and not cfg.get("keep_auto_alt"):
        a = str(el.Alt); b = AUTO.sub("", a).strip()
        if b != a: el.Alt = String(b); stats["auto_cleaned"] += 1
for idx in cfg.get("artifact_elems", []):
    el, parent = els[int(idx)]; make_artifact(el, parent); stats["artifact_elems"] += 1
missing = [i for i, (el, _) in enumerate(figs) if i not in alts and not str(el.get("/Alt", "")).strip()]
if missing: print("WARNING figures still without alt text:", missing)

# byte-level content edits: only "/Tag <</MCID n>> BDC" -> "/Artifact BMC" for the artifact MCIDs
def mc_rx(ms):
    alts_ = b"|".join(str(m).encode() for m in sorted(ms))
    return re.compile(rb"/[A-Za-z0-9_#.\-]+\s*<<\s*/MCID\s+(?:" + alts_ + rb")\s*>>\s*BDC")
def find_nums(node, key):
    if "/Nums" in node:
        a = node.Nums
        for j in range(0, len(a), 2):
            if int(a[j]) == int(key): return a[j + 1]
    for kid in node.get("/Kids", []):
        r = find_nums(kid, key)
        if r is not None: return r
for pi, ms in to_artifact.items():
    page = p.pages[pi]; streams = page.obj.Contents
    streams = list(streams) if isinstance(streams, pikepdf.Array) else [streams]
    rx = mc_rx(ms); n = 0
    for strm in streams:
        data = strm.read_bytes(); new, k = rx.subn(b"/Artifact BMC", data)
        if k: n += k; strm.write(zlib.compress(new), filter=Name("/FlateDecode"))
    if n < len(ms): print(f"WARNING page {pi+1}: converted {n} of {len(ms)} marked-content sequences (others may be inside form XObjects)")
    sp = page.obj.get("/StructParents")
    if sp is not None and "/ParentTree" in st:
        arr = find_nums(st.ParentTree, sp)
        if arr is not None:
            for m in ms:
                if m < len(arr): arr[m] = pikepdf.Object.parse(b"null")

root.MarkInfo = Dictionary(Marked=True)
p.save(cfg["dst"])
scrub(cfg["dst"], title=cfg["title"], author=cfg.get("author"), lang=cfg.get("lang", "en-US"))
print("saved", cfg["dst"], "| figures", len(figs), stats)
