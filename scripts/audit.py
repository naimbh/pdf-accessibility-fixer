"""Triage PDFs before fixing them. For each file prints what it needs and which path to take.

usage: python3 audit.py file1.pdf [file2.pdf ...]
Paths:
  REPAIR  - already tagged with a usable tree (headings/paragraphs/figures): use repair.py
  TAG     - untagged, or the tag tree is useless (e.g. only Part/Sect, one P per page): use tag.py
  OCR+TAG - pages that are images with no (or only invisible, poor) text: use tag.py with "ocr": {"pages": [...]}"""
import sys, re, collections, pikepdf, pymupdf

TOOLS = re.compile(r"(?i)(pikepdf|ocrmypdf|tesseract|itext|pdftex|distiller|ghostscript|quartz|print to pdf|indesign|acrobat|pdf library|libreoffice|openoffice|microsoft|pymupdf|chrome|skia|python|qpdf|1t3xt|powerpoint|latex|word)")
for f in sys.argv[1:]:
    d = pymupdf.open(f); p = pikepdf.open(f); r = p.Root
    tagged = "/StructTreeRoot" in r
    c = collections.Counter(); noalt = 0
    if tagged:
        rm = r.StructTreeRoot.get("/RoleMap", {})
        def walk(n):
            global noalt
            if isinstance(n, pikepdf.Array):
                for k in n: walk(k)
                return
            if not isinstance(n, pikepdf.Dictionary) or "/S" not in n: return
            s = str(n.S); s2 = str(rm.get(s, s)); c[s2] += 1
            if s2 == "/Figure" and not str(n.get("/Alt", "")).strip(): noalt += 1
            if "/K" in n: walk(n.K)
        walk(r.StructTreeRoot.K)
    scan_pages, text_pages = [], 0
    for i, pg in enumerate(d):
        tr = pg.get_texttrace(); vis = sum(1 for s in tr if s.get("type") != 3)
        big_img = any(rect.width * rect.height > 0.6 * pg.rect.width * pg.rect.height for im in pg.get_images() for rect in pg.get_image_rects(im[0]))
        if vis == 0 and big_img: scan_pages.append(i + 1)
        if len(pg.get_text().strip()) > 50: text_pages += 1
    useful = c["/P"] + c["/LBody"] + c["/TD"] > 3 * max(1, len(d)) or (c["/P"] > len(d) and sum(c[f"/H{k}"] for k in range(1, 7)) > 0)
    path = "OCR+TAG" if scan_pages else ("REPAIR" if tagged and useful else "TAG")
    sample = " ".join(pg.get_text() for pg in list(d)[:3]); letters = sum(ch.isalpha() for ch in sample)
    ratio = sample.count(" ") / max(1, letters)
    meta = " ".join(str(v) for k, v in p.docinfo.items() if k not in ("/Title", "/Author"))
    xmp = r.Metadata.read_bytes().decode("utf8", "ignore") if "/Metadata" in r else ""
    tools = sorted({t.lower() for t in TOOLS.findall(meta)} | {t.lower() for t in re.findall(r"(?is)<(?:\w+:)?(?:Producer|CreatorTool)\b[^>]*>([^<]{0,40})", xmp)})
    print(f"== {f.split('/')[-1]}")
    print(f"   path: {path} | pages {len(d)} (text on {text_pages}) | scan pages: {scan_pages or 'none'} | images {sum(len(pg.get_images()) for pg in d)}")
    print(f"   tagged: {tagged} {dict(c.most_common(8)) if tagged else ''} | figures without alt: {noalt}")
    print(f"   lang: {r.get('/Lang') or 'MISSING'} | title: {d.metadata.get('title') or 'MISSING'!r} | bookmarks: {len(d.get_toc())} | links: {sum(len(list(pg.links())) for pg in d)}")
    print(f"   space ratio {ratio:.3f}{'  -> words run together: run spaced_actualtext.py after tagging' if letters > 300 and ratio < 0.05 else ''}")
    print(f"   tool names in metadata: {tools or 'none'}")
