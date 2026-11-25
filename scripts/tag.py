"""Tag an UNTAGGED (or uselessly tagged) PDF in place, keeping its exact appearance (pixel-identical).

usage:  python3 tag.py cfg.json dump   -> print layout regions per page (key, class, size, bold, bbox, text)
        python3 tag.py cfg.json        -> write the tagged PDF to cfg["dst"]

config keys (all optional except src, dst, work, title):
  src, dst, work, title, author, lang (default "en-US")
  keep_forms: [form XObject names kept as one unit, e.g. a watermark stamp]
  ocr: {"pages": [2,3,...], "lang": "eng"}  scan pages: strip their old invisible text, add a fresh invisible OCR
       layer (ocrmypdf --skip-text --output-type pdf --optimize 0; page images untouched)
  artifact_text: [regex]        artifact_regions: [key]
  retag: {key: "H1"|"H2"|"P"|"Caption"|"Note"|"LI"|"BlockQuote"|"Table"|"Figure"|"Artifact"|...}
  heading_levels: [[regex, level]]   (section-header regions; matched case-insensitively, first match wins)
  promote: [[regex, level]]          (text regions -> heading; level 0 = P)
  type_by_text: [[regex, type]]      split_head: {key: level}   geo_order_pages: [page]
  alt: {key: "text" | "" (decorative)}
  add_regions: [{"page": n, "bbox": [x0,y0,x1,y1], "cls": "picture", "after": key}]
  merge_next: [key]  no_merge: [key]  auto_merge (default true)  move_to_end: [key]  move_after: {key: key}
  table_headers: {key: n}   lang_regions: {key: "es-ES"}   content_order (default true)
  ocr_fix: [[regex, replacement]]    (cleans the spoken ActualText on OCR pages)
Region keys look like "p3:7" (page 3, region 7) and come from the dump.
"""
import sys, json, re, os, subprocess, collections
import pikepdf, pymupdf, pdfplumber
from pikepdf import Name, Dictionary, Array, String, Operator
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stage1
from scrub_metadata import scrub

cfg = json.load(open(sys.argv[1]))
MODE = sys.argv[2] if len(sys.argv) > 2 else "build"
W = cfg["work"]; os.makedirs(W, exist_ok=True)
src = cfg["src"]

# ---------- OCR for scan pages (page images untouched) ----------
scan_pages = set()
if cfg.get("ocr"):
    scan_pages = {p - 1 for p in cfg["ocr"]["pages"]}
    ocr_pdf = f"{W}/ocr.pdf"
    if not os.path.exists(ocr_pdf):
        p = pikepdf.open(src)
        for pi in scan_pages:            # remove old (invisible) text objects only
            page = p.pages[pi]; out = []; inbt = False
            for o, op in pikepdf.parse_content_stream(page):
                s = str(op)
                if s == "BT": inbt = True; continue
                if s == "ET": inbt = False; continue
                if not inbt: out.append((o, op))
            page.obj.Contents = p.make_stream(pikepdf.unparse_content_stream(out))
        p.save(f"{W}/stripped.pdf")
        subprocess.run(["ocrmypdf", "--skip-text", "--output-type", "pdf", "--optimize", "0", "-l", cfg["ocr"].get("lang", "eng"),
                        f"{W}/stripped.pdf", ocr_pdf], check=True)
    src = ocr_pdf

# ---------- stage 1: an MCID on every visible unit ----------
s1 = f"{W}/s1.pdf"
stage1.main(src, s1, cfg.get("keep_forms", []))
units = {int(k): v for k, v in json.load(open(s1 + ".units.json")).items()}
kind_of = {pi: {u["mcid"]: u["kind"] for u in us} for pi, us in units.items()}

# ---------- geometry of every MCID (pdfplumber: top-left origin, MediaBox) ----------
geo = collections.defaultdict(dict)
with pdfplumber.open(s1) as pl:
    for pi, pg in enumerate(pl.pages):
        d = geo[pi]
        for o in pg.chars + pg.lines + pg.rects + pg.curves + pg.images:
            m = o.get("mcid")
            if m is None: continue
            e = d.setdefault(m, {"x0": 1e9, "top": 1e9, "x1": -1e9, "bottom": -1e9, "text": "", "sizes": [], "fonts": []})
            e["x0"] = min(e["x0"], o["x0"]); e["x1"] = max(e["x1"], o["x1"])
            e["top"] = min(e["top"], o["top"]); e["bottom"] = max(e["bottom"], o["bottom"])
            if o.get("object_type") == "char":
                e["text"] += o["text"]; e["sizes"].append(round(o["size"], 1)); e["fonts"].append(o.get("fontname", ""))

# ---------- layout regions ----------
import pymupdf4llm
layout_src = src
if scan_pages:   # the layout model ignores invisible text: analyse a copy where the OCR text is visible
    layout_src = f"{W}/layout_visible.pdf"
    p = pikepdf.open(src)
    for pi in scan_pages:
        page = p.pages[pi]
        ops = [([0], Operator("Tr")) if str(op) == "Tr" else (o, op) for o, op in pikepdf.parse_content_stream(page)]
        page.obj.Contents = p.make_stream(pikepdf.unparse_content_stream(ops))
    p.save(layout_src)
chunks = pymupdf4llm.to_markdown(layout_src, page_chunks=True, use_ocr=False)
mupdf_doc = pymupdf.open(src)
def offs(pi):
    pg = mupdf_doc[pi]; return pg.cropbox.x0 - pg.mediabox.x0, pg.mediabox.y1 - pg.cropbox.y1
regions = {}
for pi, ch in enumerate(chunks):
    ox, oy = offs(pi)
    regions[pi] = [{"key": f"p{pi+1}:{b['index']}", "cls": b["class"],
                    "bbox": (b["bbox"][0] + ox, b["bbox"][1] + oy, b["bbox"][2] + ox, b["bbox"][3] + oy)} for b in ch["page_boxes"]]
for r in cfg.get("add_regions", []):
    pi = r["page"] - 1; key = f"p{r['page']}:a{len([x for x in regions[pi] if ':a' in x['key']])}"
    reg = {"key": key, "cls": r.get("cls", "picture"), "bbox": tuple(r["bbox"])}
    if r.get("after"):
        idx = next(i for i, x in enumerate(regions[pi]) if x["key"] == r["after"]); regions[pi].insert(idx + 1, reg)
    else: regions[pi].append(reg)

def contains(bb, x, y, pad=2):
    return bb[0] - pad <= x <= bb[2] + pad and bb[1] - pad <= y <= bb[3] + pad
def center(g): return ((g["x0"] + g["x1"]) / 2, (g["top"] + g["bottom"]) / 2)

assign = {pi: collections.defaultdict(list) for pi in units}
unassigned = {pi: [] for pi in units}
for pi in units:
    regs = regions.get(pi, [])
    for u in units[pi]:
        m = u["mcid"]; g = geo[pi].get(m)
        if g is None: continue
        cx, cy = center(g)
        cand = [r for r in regs if contains(r["bbox"], cx, cy)]
        if cand:
            r = min(cand, key=lambda r: (r["bbox"][2] - r["bbox"][0]) * (r["bbox"][3] - r["bbox"][1]))
            assign[pi][r["key"]].append(m)
        else: unassigned[pi].append(m)
    for m in list(unassigned[pi]):
        g = geo[pi][m]
        if kind_of[pi].get(m) != "text" or not g["text"].strip(): continue
        cx, cy = center(g); best, bd = None, 1e9
        for r in regs:
            if r["cls"] in ("picture", "page-header", "page-footer"): continue
            b = r["bbox"]; dx = max(b[0] - cx, 0, cx - b[2]); dy = max(b[1] - cy, 0, cy - b[3]); dd = (dx * dx + dy * dy) ** .5
            if dd < bd: best, bd = r, dd
        if best is not None and bd < 12:
            assign[pi][best["key"]].append(m); unassigned[pi].remove(m)

CO = cfg.get("content_order", True)
def order(pi, ms):
    if CO: return sorted(ms)       # drawn order: keeps ligatures and apostrophes in place
    return sorted(ms, key=lambda m: (round(geo[pi][m]["top"] / 3), geo[pi][m]["x0"]))
def rtext_bbox(pi, bb):
    ox, oy = offs(pi)
    ws = [w for w in mupdf_doc[pi].get_text("words") if contains(bb, (w[0] + w[2]) / 2 + ox, (w[1] + w[3]) / 2 + oy, 1)]
    ws.sort(key=lambda w: (w[5], w[6], w[7])); return " ".join(w[4] for w in ws)
def rtext(pi, key):
    return rtext_bbox(pi, next(r["bbox"] for r in regions[pi] if r["key"] == key))
def rsize(pi, key):
    s = [x for m in assign[pi].get(key, []) for x in geo[pi][m]["sizes"]]
    return collections.Counter(s).most_common(1)[0][0] if s else 0
def rbold(pi, key):
    f = [x for m in assign[pi].get(key, []) for x in geo[pi][m]["fonts"]]
    return bool(f) and sum(1 for x in f if re.search(r"Bold|Black|Semibold|Heavy|,B|-B\b|Medi", x)) > len(f) / 2

if MODE == "dump":
    for pi in sorted(regions):
        print(f"=== page {pi+1}")
        for r in regions[pi]:
            t = rtext(pi, r["key"])
            print(f"  {r['key']:8} {r['cls']:15} sz={rsize(pi, r['key']):<5} {'B' if rbold(pi, r['key']) else ' '} {[round(v) for v in r['bbox']]} {t[:120]!r}" + (f" ...{t[-45:]!r}" if os.environ.get("TAIL") and len(t) > 160 else ""))
        un = [geo[pi][m]["text"] for m in unassigned[pi] if geo[pi][m]["text"].strip()]
        if un: print("  UNASSIGNED text:", un[:10])
    sys.exit()

# ---------- logical elements ----------
art_rx = [re.compile(x) for x in cfg.get("artifact_text", [])]
retag = cfg.get("retag", {}); alts = cfg.get("alt", {}); hl = [(re.compile(a, re.I), l) for a, l in cfg.get("heading_levels", [])]
promote = [(re.compile(a), l) for a, l in cfg.get("promote", [])]
art_regions = set(cfg.get("artifact_regions", [])); merge_next = set(cfg.get("merge_next", [])); no_merge = set(cfg.get("no_merge", []))
hsizes = sorted({rsize(pi, r["key"]) for pi in regions for r in regions[pi] if r["cls"] == "section-header"}, reverse=True)
elements = []; seen_title = False
for pi in sorted(regions):
    for r in regions[pi]:
        key = r["key"]; ms = assign[pi].get(key, []); t = rtext(pi, key); cls = r["cls"]
        typ = {"title": "H1", "section-header": "H", "text": "P", "caption": "Caption", "footnote": "Note", "list-item": "LI",
               "table": "Table", "picture": "Figure", "formula": "Formula", "page-header": "Artifact", "page-footer": "Artifact"}.get(cls, "P")
        if cls == "title":
            typ = "H1" if not seen_title else "H"; seen_title = True
        if typ == "H":
            lvl = None
            for rx, l in hl:
                if rx.search(t): lvl = l; break
            if lvl is None: lvl = min(6, 2 + (hsizes.index(rsize(pi, key)) if rsize(pi, key) in hsizes else 0))
            typ = f"H{lvl}" if lvl else "P"
        elif typ in ("P", "LI", "Note", "Caption"):
            for rx, l in promote:
                if rx.search(t): typ = f"H{l}" if l else "P"; break
        for rx, ty in cfg.get("type_by_text", []):
            if re.search(rx, t) and typ != "Artifact": typ = ty; break
        if key in retag: typ = retag[key]
        if key in art_regions or any(rx.search(t) for rx in art_rx): typ = "Artifact"
        if typ == "Figure" and alts.get(key, None) == "": typ = "Artifact"
        if typ == "Figure" and key not in alts: print("WARNING no alt for", key, t[:60])
        if not ms and typ != "Figure": continue
        if key in cfg.get("split_head", {}) and ms:
            top = min(geo[pi][m]["top"] for m in ms); lh = max(3, min(geo[pi][m]["bottom"] - geo[pi][m]["top"] for m in ms))
            hm = [m for m in ms if geo[pi][m]["top"] < top + 0.6 * lh]; rest = [m for m in ms if m not in hm]
            hb = (r["bbox"][0], top - 1, r["bbox"][2], top + lh + 1)
            ht = rtext_bbox(pi, hb); bt = t[len(ht):].strip() if t.startswith(ht) else t
            elements.append({"type": "H%d" % cfg["split_head"][key], "page": pi, "mcids": hm, "key": key + "h", "text": ht, "bbox": hb, "alt": None, "lang": None})
            ms, t = rest, bt
        elements.append({"type": typ, "page": pi, "mcids": ms, "key": key, "text": t, "bbox": r["bbox"],
                         "alt": alts.get(key), "lang": cfg.get("lang_regions", {}).get(key)})
if not any(e["type"] == "H1" for e in elements):   # no H1 configured: the first heading on the first pages becomes the H1
    first = next((e for e in elements if re.fullmatch(r"H[1-6]", e["type"]) and e["page"] <= 1), None)
    if first is not None: first["type"] = "H1"; print("note: no H1 configured; using", repr(first["text"][:60]))
for gp in cfg.get("geo_order_pages", []):
    pi = gp - 1; idx = [i for i, e in enumerate(elements) if e["page"] == pi]
    mid = float(mupdf_doc[pi].rect.width) / 2
    def gkey(e):
        x0, y0, x1, y1 = e["bbox"]; return (0 if (x0 + x1) / 2 < mid or (x1 - x0) > 1.2 * mid else 1, y0)
    sub = sorted([elements[i] for i in idx], key=gkey)
    for i, e in zip(idx, sub): elements[i] = e
mv = set(cfg.get("move_to_end", []))
elements = [e for e in elements if e["key"] not in mv] + [e for e in elements if e["key"] in mv]
for a, b in cfg.get("move_after", {}).items():
    ea = next(e for e in elements if e["key"] == a); elements.remove(ea)
    ib = next(i for i, e in enumerate(elements) if e["key"] == b); elements.insert(ib + 1, ea)

# ---------- tables: grid from drawn rule lines, else find_tables, else one row per text line ----------
owner = None   # set below; rule_grid skips MCIDs already owned
def rule_grid(el):
    pi = el["page"]; bb = el["bbox"]; pg = mupdf_doc[pi]; ox, oy = offs(pi)
    V, Hs = [], []
    for dr in pg.get_drawings():
        for it in dr["items"]:
            if it[0] == "l":
                a, b = it[1], it[2]
                if abs(a.x - b.x) < 1: V.append((a.x + ox, min(a.y, b.y) + oy, max(a.y, b.y) + oy))
                elif abs(a.y - b.y) < 1: Hs.append((a.y + oy, min(a.x, b.x) + ox, max(a.x, b.x) + ox))
            elif it[0] == "re":
                r = it[1]
                if r.width < 2 and r.height > 3: V.append((r.x0 + ox, r.y0 + oy, r.y1 + oy))
                elif r.height < 2 and r.width > 3: Hs.append((r.y0 + oy, r.x0 + ox, r.x1 + ox))
    m = 8; V_all = list(V)
    V = [v for v in V if bb[0] - m <= v[0] <= bb[2] + m and v[2] >= bb[1] - m and v[1] <= bb[3] + m]
    if V:   # the drawn table may extend beyond the layout region: grow to the rules' full extent
        for _ in range(3):
            y0 = min(v[1] for v in V); y1 = max(v[2] for v in V)
            bb = (min(bb[0], min(v[0] for v in V)), min(bb[1], y0), max(bb[2], max(v[0] for v in V)), max(bb[3], y1))
            hn = [h for h in Hs if bb[1] - m <= h[0] <= bb[3] + m and h[2] >= bb[0] - m and h[1] <= bb[2] + m]
            if hn: bb = (min(bb[0], min(h[1] for h in hn)), bb[1], max(bb[2], max(h[2] for h in hn)), bb[3])
            V2 = [v for v in V_all if bb[0] - m <= v[0] <= bb[2] + m and v[2] >= bb[1] - m and v[1] <= bb[3] + m]
            if len(V2) == len(V): break
            V = V2
    Hs = [h for h in Hs if bb[1] - m <= h[0] <= bb[3] + m and h[2] >= bb[0] - m and h[1] <= bb[2] + m]
    if not V or not Hs: return None
    def cluster(vals):
        out = []
        for v in sorted(vals):
            if out and v - out[-1][-1] < 2.5: out[-1].append(v)
            else: out.append([v])
        return [sum(c) / len(c) for c in out]
    xs = cluster([v[0] for v in V]); ys = cluster([h[0] for h in Hs])
    if len(xs) < 2: return None
    width = max(xs) - min(xs)
    ys = [y for y in ys if sum(h[2] - h[1] for h in Hs if abs(h[0] - y) < 2.5) > 0.5 * width]
    if len(ys) < 2: return None
    tms = [mm for mm in el["mcids"] if kind_of[pi].get(mm) == "text"]
    tms += [mm for mm, g in geo[pi].items() if kind_of[pi].get(mm) == "text" and mm not in tms and mm not in owner[pi]
            and contains((xs[0], ys[0], xs[-1], ys[-1]), *center(g), 0)]
    rows = []
    for y0, y1 in zip(ys, ys[1:]):
        if y1 - y0 < 3: continue
        bounds = [xs[0]] + [x for x in xs[1:-1] if any(abs(v[0] - x) < 2.5 and v[1] <= y0 + 2 and v[2] >= y1 - 2 for v in V)] + [xs[-1]]
        cells = []
        for x0, x1 in zip(bounds, bounds[1:]):
            span = sum(1 for x in xs if x0 - 1 < x < x1 - 1)
            cells.append(([mm for mm in tms if contains((x0, y0, x1, y1), *center(geo[pi][mm]), 0.5)], max(1, span)))
        rows.append(cells)
    if len({mm for r in rows for c, _ in r for mm in c}) < 0.9 * len(tms): return None
    return rows
def table_rows(el):
    pi = el["page"]; tms = [m for m in el["mcids"] if kind_of[pi].get(m) == "text"]
    lines = collections.defaultdict(list); rows = []
    for m in tms: lines[round(geo[pi][m]["top"] / 4)].append(m)
    for k in sorted(lines):
        ms = sorted(lines[k], key=lambda m: geo[pi][m]["x0"]); cells, last = [], None
        for m in ms:
            if last is None or geo[pi][m]["x0"] - geo[pi][last]["x1"] > 8: cells.append([m])
            else: cells[-1].append(m)
            last = m
        rows.append([(c, 1) for c in cells])
    return rows

# ---------- structure tree ----------
pdf = pikepdf.open(s1)
root = pdf.make_indirect(Dictionary(Type=Name("/StructTreeRoot")))
doc = pdf.make_indirect(Dictionary(Type=Name("/StructElem"), S=Name("/Document"), P=root, K=Array()))
root.K = Array([doc]); pages = list(pdf.pages)
owner = {pi: {} for pi in range(len(pages))}; heads = []

def new_el(S, parent, pi=None):
    e = pdf.make_indirect(Dictionary(Type=Name("/StructElem"), S=Name("/" + S), P=parent, K=Array()))
    if pi is not None: e.Pg = pages[pi].obj
    parent.K.append(e); return e
def add(el, pi, ms, tag):
    for m in order(pi, ms):
        if m in owner[pi]: continue
        el.K.append(Dictionary(Type=Name("/MCR"), Pg=pages[pi].obj, MCID=m)); owner[pi][m] = (el, tag)

ocr_fix = [(re.compile(a), b) for a, b in cfg.get("ocr_fix", [])]
at_text = {}
def note_text(se, el):   # cleaned spoken text for OCR pages (stored as ActualText; invisible)
    if el["page"] not in scan_pages or not el.get("text"): return
    t = re.sub(r"^[*¢«•©°]+\s*", "", el["text"].strip())
    for rx, rep in ocr_fix: t = rx.sub(rep, t)
    cur = at_text.setdefault(se.objgen, [se, ""]); cur[1] = (cur[1] + " " + t).strip()

TERM = re.compile(r'[.?!:;"”’)\]]\s*$')
prev = {"el": None, "type": None, "text": "", "key": None}; cur_list = None
for el in elements:
    t, pi = el["type"], el["page"]
    if t == "Artifact": continue
    auto = (cfg.get("auto_merge", True) and prev["el"] is not None and t in ("P", "LI") and prev["type"] == t and prev["key"] not in no_merge
            and el["key"] not in no_merge and prev["text"] and not TERM.search(prev["text"]) and re.match(r"^[a-z(]", el["text"] or ""))
    if (prev["key"] in merge_next or auto) and prev["el"] is not None and t in ("P", "LI", "Note", "BlockQuote", "Caption"):
        add(prev["el"], pi, el["mcids"], "LBody" if prev["type"] == "LI" else prev["type"]); note_text(prev["el"], el)
        prev.update(text=el["text"], key=el["key"]); continue
    if t == "LI":
        if cur_list is None: cur_list = new_el("L", doc, pi)
        li = new_el("LI", cur_list, pi); lb = new_el("LBody", li, pi); add(lb, pi, el["mcids"], "LBody"); note_text(lb, el)
        prev.update(el=lb, type="LI", text=el["text"], key=el["key"]); continue
    cur_list = None
    if t == "Figure":
        f = new_el("Figure", doc, pi); f.Alt = String(el["alt"] or "")
        bb = el["bbox"]; H = float(pages[pi].mediabox[3])
        f.A = Dictionary(O=Name("/Layout"), BBox=Array([bb[0], H - bb[3], bb[2], H - bb[1]]))
        ms = [m for m, g in geo[pi].items() if contains(bb, *center(g)) and m not in owner[pi]
              and not (pi in scan_pages and kind_of[pi].get(m) == "image")]     # a whole-page scan image is not the figure
        add(f, pi, ms, "Figure"); prev.update(el=f, type="Figure", text="", key=el["key"]); continue
    if t == "Table":
        tb = new_el("Table", doc, pi); nh = cfg.get("table_headers", {}).get(el["key"], 1)
        grid = rule_grid(el)
        for ri, row in enumerate(grid if grid is not None else table_rows(el)):
            tr = new_el("TR", tb, pi)
            for cm, span in row:
                c = new_el("TH" if ri < nh else "TD", tr, pi)
                attrs = {"O": Name("/Table")}
                if ri < nh: attrs["Scope"] = Name("/Column")
                if span > 1: attrs["ColSpan"] = span
                if len(attrs) > 1: c.A = Dictionary(**attrs)
                add(c, pi, cm, "TH" if ri < nh else "TD")
        rest = [m for m in el["mcids"] if m not in owner[pi] and kind_of[pi].get(m) == "text"]
        if rest: add(new_el("P", doc, pi), pi, rest, "P")
        prev.update(el=tb, type="Table", text="", key=el["key"]); continue
    e = new_el(t, doc, pi); note_text(e, el)
    if el.get("lang"): e.Lang = String(el["lang"])
    if t == "Formula": e.Alt = String(el["text"])
    add(e, pi, [m for m in el["mcids"] if kind_of[pi].get(m) == "text" or t == "Formula"], t)
    if re.fullmatch(r"H[1-6]", t): heads.append((int(t[1]), el["text"], pi))
    prev.update(el=e, type=t, text=el["text"], key=el["key"])

# ---------- rewrite content: tagged units keep (renumbered) MCIDs, everything else becomes an Artifact ----------
nums = Array()
for pi, page in enumerate(pages):
    out = []; nid = 0; arr = []; remap = {}
    for o, op in pikepdf.parse_content_stream(page):
        if str(op) == "BDC" and len(o) == 2 and isinstance(o[1], Dictionary) and "/MCID" in o[1] and str(o[0]) == "/Span":
            m = int(o[1].MCID)
            if m in owner[pi]:
                el, tag = owner[pi][m]
                out.append(([Name("/" + tag), Dictionary(MCID=nid)], op)); remap[m] = nid; arr.append(el); nid += 1
            else: out.append(([Name("/Artifact")], Operator("BMC")))
            continue
        out.append((o, op))
    page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(out))
    page.obj.StructParents = pi; nums.append(pi); nums.append(pdf.make_indirect(arr))
    seen = set()
    for m, (el, tag) in owner[pi].items():
        if el.objgen in seen: continue
        seen.add(el.objgen)
        for item in el.K:
            if isinstance(item, Dictionary) and item.get("/Type") == "/MCR" and item.Pg.objgen == page.obj.objgen and "/Done" not in item:
                item.MCID = remap[int(item.MCID)]; item.Done = True
for pi in range(len(pages)):
    for m, (el, tag) in owner[pi].items():
        for item in el.K:
            if isinstance(item, Dictionary) and "/Done" in item: del item["/Done"]

next_sp = len(pages)
for pi, page in enumerate(pages):
    for a in page.obj.get("/Annots", []):
        if a.get("/Subtype") != "/Link": continue
        r = [float(x) for x in a.Rect]; H = float(page.mediabox[3]); cx, cy = (r[0] + r[2]) / 2, H - (r[1] + r[3]) / 2
        host = next((el for m, (el, tag) in owner[pi].items() if geo[pi].get(m) and contains((geo[pi][m]["x0"], geo[pi][m]["top"], geo[pi][m]["x1"], geo[pi][m]["bottom"]), cx, cy, 1)), doc)
        ln = pdf.make_indirect(Dictionary(Type=Name("/StructElem"), S=Name("/Link"), P=host, Pg=page.obj, K=Array([Dictionary(Type=Name("/OBJR"), Obj=a, Pg=page.obj)])))
        if "/Contents" not in a:
            uri = a.get("/A", {}).get("/URI"); a.Contents = String("Link: " + str(uri) if uri else "Link")
        host.K.append(ln); a.StructParent = next_sp; nums.append(next_sp); nums.append(ln); next_sp += 1
    page.obj.Tabs = Name("/S")
for se, t in at_text.values():
    if t: se.ActualText = String(t)

root.ParentTree = pdf.make_indirect(Dictionary(Nums=nums)); root.ParentTreeNextKey = next_sp
pdf.Root.StructTreeRoot = root; pdf.Root.MarkInfo = Dictionary(Marked=True)
pdf.Root.Lang = String(cfg.get("lang", "en-US"))
vp = pdf.Root.get("/ViewerPreferences", Dictionary()); vp.DisplayDocTitle = True; pdf.Root.ViewerPreferences = vp
if not ("/Outlines" in pdf.Root and pdf.Root.Outlines.get("/First") is not None) and heads:
    with pdf.open_outline() as ol:
        stack = []
        for lvl, text, pi in heads:
            it = pikepdf.OutlineItem(text[:150], pi)
            while stack and stack[-1][0] >= lvl: stack.pop()
            (stack[-1][1].children if stack else ol.root).append(it); stack.append((lvl, it))
    pdf.Root.PageMode = Name("/UseOutlines")
pdf.save(cfg["dst"])
scrub(cfg["dst"], title=cfg["title"], author=cfg.get("author"), lang=cfg.get("lang", "en-US"))
print("saved", cfg["dst"], dict(collections.Counter(e["type"] for e in elements)), "headings", len(heads))
