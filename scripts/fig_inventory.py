"""fig_inventory.py deck.pdf outdir
Lists every Figure struct element (index, page, slide, bbox, current alt), renders a crop of each,
groups identical crops, and writes a numbered contact sheet per deck."""
import sys, os, json, hashlib, collections, pikepdf, pymupdf, pdfplumber
from PIL import Image, ImageDraw
src, out = sys.argv[1], sys.argv[2]; os.makedirs(out, exist_ok=True)
p = pikepdf.open(src); pages = {pg.objgen: i for i, pg in enumerate(p.pages)}
rm = p.Root.StructTreeRoot.get("/RoleMap", {})
box = collections.defaultdict(dict)
with pdfplumber.open(src) as pl:
    for i, pg in enumerate(pl.pages):
        for o in pg.chars + pg.images + pg.rects + pg.curves + pg.lines:
            m = o.get("mcid")
            if m is None: continue
            b = box[i].setdefault(m, [1e9, 1e9, -1e9, -1e9])
            b[0] = min(b[0], o["x0"]); b[1] = min(b[1], o["top"]); b[2] = max(b[2], o["x1"]); b[3] = max(b[3], o["bottom"])
def mcrs(n, pg=None):
    out = []
    if "/Pg" in n: pg = pages.get(n.Pg.objgen, pg)
    if "/K" not in n: return out
    for k in (n.K if isinstance(n.K, pikepdf.Array) else [n.K]):
        if isinstance(k, int): out.append((pg, int(k)))
        elif isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/MCR": out.append((pages.get(k.Pg.objgen, pg) if "/Pg" in k else pg, int(k.MCID)))
        elif isinstance(k, pikepdf.Dictionary) and "/S" in k: out += mcrs(k, pg)
    return out
figs = []
def walk(n):
    if isinstance(n, pikepdf.Array):
        for k in n: walk(k)
        return
    if not isinstance(n, pikepdf.Dictionary) or "/S" not in n: return
    s = str(n.S)
    if s == "/Figure" or str(rm.get(s, "")) == "/Figure":
        ms = mcrs(n); bb = None; pg = None
        for (pi, m) in ms:
            b = box.get(pi, {}).get(m)
            if b is None: continue
            pg = pi
            bb = b if bb is None else [min(bb[0], b[0]), min(bb[1], b[1]), max(bb[2], b[2]), max(bb[3], b[3])]
        figs.append({"i": len(figs), "page": pg, "bbox": bb, "alt": str(n.get("/Alt")) if "/Alt" in n else None, "mcrs": ms})
        return
    if "/K" in n: walk(n.K)
walk(p.Root.StructTreeRoot.K)
d = pymupdf.open(src); crops = []; groups = {}
for f in figs:
    if f["bbox"] is None or f["page"] is None: f["hash"] = None; continue
    bb = f["bbox"]; r = pymupdf.Rect(bb[0], bb[1], bb[2], bb[3]) & d[f["page"]].rect
    if r.is_empty or r.width < 2 or r.height < 2: f["hash"] = None; continue
    pix = d[f["page"]].get_pixmap(dpi=110, clip=r); fn = f"{out}/fig{f['i']:03d}.png"; pix.save(fn)
    h = hashlib.md5(pix.samples).hexdigest()[:10]; f["hash"] = h; f["size"] = (round(r.width), round(r.height))
    f["first_of_group"] = groups.setdefault(h, f["i"])
    if f["first_of_group"] == f["i"]: crops.append((f["i"], fn))
json.dump(figs, open(f"{out}/figs.json", "w"), indent=1)
# contact sheets of unique crops (numbered)
W, H, cols = 300, 230, 4
for s in range(0, len(crops), 16):
    chunk = crops[s:s + 16]; rows = (len(chunk) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * W, rows * H), "white"); dr = ImageDraw.Draw(sheet)
    for k, (i, fn) in enumerate(chunk):
        im = Image.open(fn).convert("RGB"); im.thumbnail((W - 10, H - 30))
        x, y = (k % cols) * W, (k // cols) * H; sheet.paste(im, (x + 5, y + 25)); dr.text((x + 5, y + 5), f"#{i}", fill="red")
    sheet.save(f"{out}/sheet{s // 16:02d}.png")
print(src.split("/")[-1], "figures", len(figs), "unique crops", len(crops), "no-geometry", sum(1 for f in figs if f.get("hash") is None))
for f in figs:
    print(f" #{f['i']:3} p{(f['page'] or 0) + 1:<3} grp#{f.get('first_of_group', '-')!s:<4} size={f.get('size')} alt={(f['alt'] or '<NONE>')[:70]!r}")
