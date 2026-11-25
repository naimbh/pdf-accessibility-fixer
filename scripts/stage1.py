"""Stage 1: wrap every visible content unit of the ORIGINAL pdf in a marked-content sequence with an MCID.
Page appearance is unchanged (only BDC/EMC operators are inserted).
Units: each text-show op (Tj TJ ' "), each existing non-OC BDC..EMC group that doesn't cross BT/ET,
each XObject Do / sh / inline image, and each painted path (construction ops .. paint op).
Form XObjects that contain text are inlined first (unless listed in keep_forms) so their text can be tagged.
Existing structure and non-OC marked content are removed.
Writes <out> and <out>.units.json  ({page: [{mcid, kind, name}]}).
"""
import pikepdf, json, sys
from pikepdf import Name, Operator, Dictionary

TEXT_OPS = {"Tj", "TJ", "'", '"'}
PATH_CONS = {"m", "l", "c", "v", "y", "h", "re"}
PAINT = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"}
RES_CATS = ["/Font", "/XObject", "/ExtGState", "/ColorSpace", "/Pattern", "/Shading", "/Properties"]

def form_has_text(xo, depth=0):
    if depth > 6: return False
    try: ops = pikepdf.parse_content_stream(xo)
    except Exception: return False
    for o, op in ops:
        if str(op) == "BT": return True
        if str(op) == "Do":
            sub = xo.get("/Resources", {}).get("/XObject", {}).get(str(o[0]))
            if sub is not None and sub.get("/Subtype") == "/Form" and form_has_text(sub, depth + 1): return True
    return False

class Inliner:
    def __init__(self, pdf, page, keep_forms):
        self.pdf, self.page, self.keep = pdf, page, set(keep_forms)
        self.n = 0
        if "/Resources" not in page.obj: page.obj.Resources = Dictionary()
        self.res = page.obj.Resources

    def merge_res(self, fres):
        ren = {}
        if fres is None: return ren
        self.n += 1
        for cat in RES_CATS:
            if cat not in fres: continue
            if cat not in self.res: self.res[cat] = Dictionary()
            for k, v in fres[cat].items():
                nk = f"/I{self.n}_{k[1:]}"
                self.res[cat][nk] = v
                ren[(cat, k)] = Name(nk)
        return ren

    def rename(self, operands, op, ren):
        o = list(operands)
        def r(cat, x):
            return ren.get((cat, str(x)), x) if isinstance(x, Name) else x
        if op == "Tf": o[0] = r("/Font", o[0])
        elif op == "Do": o[0] = r("/XObject", o[0])
        elif op == "gs": o[0] = r("/ExtGState", o[0])
        elif op in ("cs", "CS"): o[0] = r("/ColorSpace", o[0])
        elif op in ("scn", "SCN") and o and isinstance(o[-1], Name): o[-1] = r("/Pattern", o[-1])
        elif op == "sh": o[0] = r("/Shading", o[0])
        elif op in ("BDC", "DP") and len(o) > 1 and isinstance(o[1], Name): o[1] = r("/Properties", o[1])
        return o

    def expand(self, ops, resdict, ren, depth=0):
        out = []
        for operands, op in ops:
            s = str(op)
            if ren: operands = self.rename(operands, s, ren)
            if s == "Do" and depth < 6:
                name = str(operands[0])
                xo = resdict.get("/XObject", {}).get(name) if resdict is not None else None
                if xo is None: xo = self.res.get("/XObject", {}).get(name)
                orig = [k for (c, k), v in ren.items() if c == "/XObject" and str(v) == name]
                keyname = orig[0] if orig else name
                if xo is not None and xo.get("/Subtype") == "/Form" and keyname not in self.keep and form_has_text(xo):
                    fres = xo.get("/Resources")
                    sub_ren = self.merge_res(fres)
                    sub_ops = pikepdf.parse_content_stream(xo)
                    out.append(([], Operator("q")))
                    if "/Matrix" in xo: out.append((list(xo.Matrix), Operator("cm")))
                    bb = [float(x) for x in xo.BBox]
                    out.append(([bb[0], bb[1], bb[2] - bb[0], bb[3] - bb[1]], Operator("re")))
                    out.append(([], Operator("W"))); out.append(([], Operator("n")))
                    out += self.expand(sub_ops, fres, sub_ren, depth + 1)
                    out.append(([], Operator("Q")))
                    continue
            out.append((operands, op))
        return out

def is_oc(operands):
    return len(operands) >= 1 and str(operands[0]) == "/OC"

def wrap_page(ops):
    out, units = [], []
    mcid = 0
    i, N = 0, len(ops)
    path = []
    def emit_unit(unit_ops, kind, name=""):
        nonlocal mcid
        out.append(([Name("/Span"), Dictionary(MCID=mcid)], Operator("BDC")))
        out.extend(unit_ops)
        out.append(([], Operator("EMC")))
        units.append({"mcid": mcid, "kind": kind, "name": name})
        mcid += 1
    def find_emc(j):
        depth = 0
        for k in range(j, N):
            s = str(ops[k][1])
            if s in ("BDC", "BMC"): depth += 1
            elif s == "EMC":
                depth -= 1
                if depth == 0: return k
        return None
    drop_emc = []
    while i < N:
        operands, op = ops[i]; s = str(op)
        if s in ("BDC", "BMC"):
            if s == "BDC" and is_oc(operands):
                drop_emc.append(False); out.append((operands, op)); i += 1; continue
            k = find_emc(i)
            inner = [str(x[1]) for x in ops[i + 1:k]] if k else []
            nested = any(x in ("BDC", "BMC") for x in inner)
            if k is not None and not nested and "BT" not in inner and "ET" not in inner and not path \
               and any(x in TEXT_OPS or x in ("Do", "sh", "INLINE IMAGE") or x in PAINT for x in inner):
                grp = ops[i:k + 1]
                if str(operands[0]) == "/Artifact":
                    out.extend(grp); i = k + 1; continue
                props = operands[1] if s == "BDC" and len(operands) > 1 else None
                if isinstance(props, Dictionary) and "/MCID" in props:
                    grp = ops[i + 1:k]
                kind = "text" if any(x in TEXT_OPS for x in inner) else ("image" if "Do" in inner or "INLINE IMAGE" in inner else "path")
                emit_unit(grp, kind); i = k + 1; continue
            drop_emc.append(True); i += 1; continue
        if s == "EMC":
            if drop_emc:
                if not drop_emc.pop(): out.append((operands, op))
            i += 1; continue
        if s in TEXT_OPS:
            emit_unit([(operands, op)], "text"); i += 1; continue
        if s in ("Do", "sh") or s == "INLINE IMAGE":
            emit_unit([(operands, op)], "image" if s != "sh" else "path", str(operands[0]) if operands else ""); i += 1; continue
        if s in PATH_CONS:
            path.append((operands, op)); i += 1; continue
        if s in ("W", "W*") and path:
            path.append((operands, op)); i += 1; continue
        if s in PAINT and path:
            path.append((operands, op))
            if s == "n": out.extend(path)
            else: emit_unit(path, "path")
            path = []; i += 1; continue
        if path:
            out.extend(path); path = []
        out.append((operands, op)); i += 1
    if path: out.extend(path)
    return out, units

def main(src, dst, keep_forms=()):
    pdf = pikepdf.open(src)
    for k in ("/StructTreeRoot", "/MarkInfo"):
        if k in pdf.Root: del pdf.Root[k]
    allunits = {}
    for pi, page in enumerate(pdf.pages):
        if "/StructParents" in page.obj: del page.obj["/StructParents"]
        for a in page.obj.get("/Annots", []):
            if "/StructParent" in a: del a["/StructParent"]
        inl = Inliner(pdf, page, keep_forms)
        ops = pikepdf.parse_content_stream(page)
        ops = inl.expand(ops, page.obj.get("/Resources"), {})
        new, units = wrap_page(ops)
        page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(new))
        allunits[pi] = units
    pdf.save(dst)
    json.dump(allunits, open(dst + ".units.json", "w"))
    print("stage1:", dst, "pages", len(pdf.pages), "units", sum(len(u) for u in allunits.values()))

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3].split(",") if len(sys.argv) > 3 and sys.argv[3] else ())
