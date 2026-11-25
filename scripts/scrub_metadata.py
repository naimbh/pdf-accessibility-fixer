"""Set title/author/language and remove every software/tool name from a PDF's metadata, in place.
Page content is never touched, so the file stays pixel-identical.

usage: python3 scrub_metadata.py file.pdf [--title "..."] [--author "..."] [--lang en-US]
Removes: docinfo Producer/Creator; XMP Producer, CreatorTool, softwareAgent (any prefix or attribute form),
the toolkit stamp x:xmptk, and application-private XMP blocks (e.g. InDesign's idPriv namespace).
Keeps: title, author, subject, keywords, dates, DOI, CrossMark and other publisher data."""
import re, sys, pikepdf

LOCAL = r"(?:Producer|CreatorTool|softwareAgent)"
APP_NS = r"(?:InDesign|Illustrator|Acrobat|Distiller|PowerPoint|Microsoft|Photoshop/private)"

def scrub(path, title=None, author=None, lang=None):
    p = pikepdf.open(path, allow_overwriting_input=True)
    if lang: p.Root.Lang = pikepdf.String(lang)
    if title:
        p.docinfo["/Title"] = title
        vp = p.Root.get("/ViewerPreferences", pikepdf.Dictionary()); vp.DisplayDocTitle = True; p.Root.ViewerPreferences = vp
    if author: p.docinfo["/Author"] = author
    if title or author or lang:
        with p.open_metadata(set_pikepdf_as_editor=False) as meta:
            if title: meta["dc:title"] = title
            if author: meta["dc:creator"] = [author]
            if lang: meta["dc:language"] = [lang]
    if "/Metadata" in p.Root:
        x = p.Root.Metadata.read_bytes().decode("utf8", "ignore")
        x = re.sub(r'\s+x:xmptk="[^"]*"', "", x)
        x = re.sub(rf"<((?:\w+:)?{LOCAL})\b[^>]*/>", "", x)
        x = re.sub(rf"<((?:\w+:)?{LOCAL})\b[^>]*>.*?</\1>", "", x, flags=re.S)
        x = re.sub(rf'\s(?:\w+:)?{LOCAL}="[^"]*"', "", x)
        x = re.sub(r"<(stEvt:parameters)\b[^>]*>[^<]*(?:indesign|acrobat|distiller|quark|powerpoint)[^<]*</\1>", "", x, flags=re.I)
        x = re.sub(rf'<rdf:Description\b[^>]*xmlns:\w+="[^"]*{APP_NS}[^"]*"[^>]*>.*?</rdf:Description>', "", x, flags=re.S | re.I)
        x = re.sub(rf'<rdf:Description\b[^>]*xmlns:\w+="[^"]*{APP_NS}[^"]*"[^>]*/>', "", x, flags=re.I)
        p.Root.Metadata.write(x.encode("utf8"))
    for k in ("/Producer", "/Creator"):     # open_metadata() copies XMP CreatorTool back into docinfo, so delete again
        if k in p.docinfo: del p.docinfo[k]
    for k in list(p.docinfo.keys()):         # e.g. /PTEX.Fullbanner
        if k.startswith("/PTEX"): del p.docinfo[k]
    p.save(path)

if __name__ == "__main__":
    a = sys.argv[1:]; f = a[0]
    def opt(n): return a[a.index(n) + 1] if n in a else None
    scrub(f, title=opt("--title"), author=opt("--author"), lang=opt("--lang"))
    print("scrubbed", f)
