"""List the existing tag tree of a PDF with element numbers, roles, pages and text.
The numbers are the ones repair.py uses for "retag" and "artifact_elems".

usage: python3 inspect_tags.py file.pdf [--all]     (--all also lists Span/Link/Lbl elements)"""
import sys, collections, pikepdf, pdfplumber

def elements(path):
    """Depth-first list of (index, depth, element, page_index_or_None, role, mapped_role)."""
    p = pikepdf.open(path); st = p.Root.StructTreeRoot; rm = st.get("/RoleMap", {})
    pages = {pg.objgen: i for i, pg in enumerate(p.pages)}
    out = []
    def walk(n, d=0, pg=None):
        if isinstance(n, pikepdf.Array):
            for k in n: walk(k, d, pg)
            return
        if not isinstance(n, pikepdf.Dictionary) or "/S" not in n: return
        if "/Pg" in n: pg = pages.get(n.Pg.objgen, pg)
        s = str(n.S); out.append((len(out), d, n, pg, s, str(rm.get(s, s))))
        if "/K" in n: walk(n.K, d + 1, pg)
    walk(st.K)
    return p, out, pages

if __name__ == "__main__":
    path = sys.argv[1]; show_all = "--all" in sys.argv
    p, els, pages = elements(path)
    txt = collections.defaultdict(str)
    with pdfplumber.open(path) as pl:
        for i, pg in enumerate(pl.pages):
            for ch in pg.chars:
                if ch.get("mcid") is not None: txt[(i, ch["mcid"])] += ch["text"]
    def text_of(n, pg):
        s = ""
        if "/K" not in n: return s
        if "/Pg" in n: pg = pages.get(n.Pg.objgen, pg)
        for k in (n.K if isinstance(n.K, pikepdf.Array) else [n.K]):
            if isinstance(k, int): s += txt.get((pg, int(k)), "")
            elif isinstance(k, pikepdf.Dictionary) and k.get("/Type") == "/MCR":
                s += txt.get((pages.get(k.Pg.objgen, pg) if "/Pg" in k else pg, int(k.MCID)), "")
            elif isinstance(k, pikepdf.Dictionary) and "/S" in k: s += text_of(k, pg)
        return s
    rm = p.Root.StructTreeRoot.get("/RoleMap", {})
    print("RoleMap:", {str(k): str(v) for k, v in rm.items()} or "none")
    for i, d, n, pg, s, mapped in els:
        if not show_all and mapped in ("/Span", "/Link", "/Lbl"): continue
        extra = f" alt={str(n.Alt)[:50]!r}" if "/Alt" in n else ""
        role = s if s == mapped else f"{s}->{mapped}"
        print(f"{i:5} {'  ' * min(d, 6)}{role:22} p{(pg or 0) + 1:<3}{extra} {text_of(n, pg)[:90]!r}")
