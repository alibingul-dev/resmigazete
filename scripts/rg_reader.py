#!/usr/bin/env python3
"""
Resmî Gazete okuyucu — rg/ klasöründeki günün dosyalarını OKUR, tek bir JSON'a yazar,
sonra kaynak dosyaları (.pdf / .htm / .txt) SİLER. Hiçbir şey indirmez.

Girdi : rg/Resmi_Gazete_YYYYMMDD-N.htm | .pdf   (tam sayı PDF'i — tiresiz olan — okunmaz, silinir)
Çıktı : rg/YYYYMMDD.json
  {
    "date": "2026-10-19", "issue": "33405",
    "items": [ {n, file, official_url, issuer, title, energy, method, text, decisions:[{no,date,text}]} ],
    "energy_count": k
  }

Metin yolu:
  .htm  → windows-1254 çöz, etiketleri sil
  .pdf  → pdftotext; bozuksa gömülü fontun glifleri OCR oylamasıyla çözülür (method "glyph", rakamlar birebir);
          metin katmanı olmayan taranmış sayfalar OCR (method "ocr" — rakamlar doğrulanmalı)

Kullanım:
  python3 scripts/rg_reader.py            # rg/ içindeki tüm tarihler
  python3 scripts/rg_reader.py --keep     # kaynak dosyaları silme (test için)

Gerekenler: poppler-utils, tesseract-ocr + tesseract-ocr-tur, pip: pymupdf fonttools
"""
import collections
import html
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pymupdf
from fontTools.ttLib import TTFont

RG = Path(__file__).resolve().parent.parent / "rg"
NAME_RE = re.compile(r"^Resmi_Gazete_(\d{8})(?:-(\d+))?\.(htm|pdf|txt)$")
ISSUE_RE = re.compile(r"Sayı\s*:?\s*(\d{5})")
DECISION_RE = re.compile(r"Karar\s*N[oa]\s*[:.]?\s*(\d{4,6})\s*Karar\s*Tarihi\s*[:.]?\s*(\d{1,2}[./]\d{1,2}[./]\d{4})", re.I)
ISSUER_RE = re.compile(r"([A-ZÇĞİÖŞÜ][^\n:]{2,120}?(?:Kurumundan|Kurulundan|Bakanlığından|Başkanlığından|"
                       r"Müdürlüğünden|Üniversitesinden|Rektörlüğünden|Ajansından|Cumhurbaşkanlığından))\s*[:;]")
ENERGY_WORDS = ["enerji piyasası", "epdk", "elektrik", "doğal gaz", "botaş", "epiaş", "teiaş", "yekdem",
                "yenilenebilir", "lisanssız", "dengeleme", "uzlaştırma", "petrol", "lpg", "nükleer",
                "enerji ve tabii kaynaklar", "akaryakıt", "depolama", "iletim", "kgüp", "küpst"]
TR_WORDS = [" ve ", " bir ", " ile ", " olarak ", "madde", " için ", "yönetmelik"]


def norm(s: str) -> str:
    return s.replace("İ", "i").replace("I", "ı").lower()


def looks_turkish(t: str) -> bool:
    if len(t.strip()) < 300:
        return False
    low = norm(t)
    hits = sum(low.count(w) for w in TR_WORDS)
    return hits / max(1, len(low) / 1000) >= 3  # 1000 karakterde en az 3 yaygın kelime


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def htm_text(p: Path) -> str:
    raw = p.read_bytes()
    m = re.search(rb"charset=([\w-]+)", raw[:2000], re.I)
    enc = (m.group(1).decode() if m else "windows-1254").lower()
    if enc in ("iso-8859-9", "latin5"):
        enc = "windows-1254"
    t = raw.decode(enc, errors="replace")
    t = re.sub(r"(?is)<(script|style|xml)[^>]*>.*?</\1>", " ", t)
    t = re.sub(r"(?is)<!--.*?-->", " ", t)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</div>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    t = re.sub(r"[ \t\xa0]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


# Resmî Gazete kuralı: her sayı en az 3 bağımsız turda aynı okunmadıkça "onaylı" sayılmaz.
GLYPH_PASSES = [(200, "4"), (300, "4"), (400, "6")]   # (dpi, tesseract psm)
OCR_PASSES = [(300, "4"), (400, "4"), (400, "6")]
NUM_RE = re.compile(r"%?\d+(?:[.,/:]\d+)*")


def ocr_words(png, psm="4"):
    out = subprocess.run(["tesseract", png, "-", "-l", "tur", "--psm", psm, "tsv"],
                         capture_output=True, text=True).stdout.splitlines()[1:]
    words = []
    for line in out:
        c = line.split("\t")
        if len(c) == 12 and c[11].strip() and float(c[10]) > 60:
            x, y, w, h = map(int, c[6:10])
            words.append((x, y, x + w, y + h, c[11].strip()))
    return words


def page_chars(page):
    """Gerçek glif kimlikleriyle (texttrace) karakterler: (key, x0, y0, x1, y1, size, oy)."""
    chars = []
    for t in page.get_texttrace():
        if t["type"] == 3 or t["opacity"] == 0:   # görünmez metin
            continue
        for ch in t["chars"]:
            x0, y0, x1, y1 = ch[3]
            chars.append(((t["font"], ch[1]), x0, y0, x1, y1, t["size"], ch[2][1]))
    return chars


def glyph_votes(doc, dpi, psm, d):
    votes = collections.defaultdict(collections.Counter)
    k = dpi / 72
    for i, page in enumerate(doc):
        chars = page_chars(page)
        if len(chars) < 200:
            continue
        png = f"{d}/g{dpi}_{psm}_{i}.png"
        page.get_pixmap(dpi=dpi, colorspace="gray").save(png)
        for (wx0, wy0, wx1, wy1, word) in ocr_words(png, psm):
            inside = [c for c in chars
                      if wx0 <= (c[1] + c[3]) / 2 * k <= wx1 and wy0 <= (c[2] + c[4]) / 2 * k <= wy1]
            inside = sorted((c for c in inside if c[3] - c[1] > 0.5), key=lambda c: c[1])
            if len(inside) == len(word):
                for c, w in zip(inside, word):
                    votes[c[0]][w] += 1
    return votes


def glyph_decode(path):
    """3 bağımsız turda (farklı dpi/psm) glif → harf eşlemesi çıkarılır.
    Üç turun üçünde de aynı harfe oturan glif 'onaylı'; değilse çoğunluk kullanılır ve işaretlenir."""
    doc = pymupdf.open(path)
    with tempfile.TemporaryDirectory() as d:
        runs = [glyph_votes(doc, dpi, psm, d) for dpi, psm in GLYPH_PASSES]
    tops = [{g: v.most_common(1)[0][0] for g, v in r.items()} for r in runs]
    keys = set().union(*tops)
    mapping, confirmed = {}, set()
    for g in keys:
        picks = [t[g] for t in tops if g in t]
        best, n = collections.Counter(picks).most_common(1)[0]
        mapping[g] = best
        if n == len(GLYPH_PASSES):
            confirmed.add(g)
    # boş (çizimi olmayan) glifler boşluktur — kesin
    seen = set()
    for pno in range(len(doc)):
        for f in doc.get_page_fonts(pno):
            if f[0] in seen:
                continue
            seen.add(f[0])
            base = f[3].split("+", 1)[-1]
            try:
                tt = TTFont(io.BytesIO(doc.extract_font(f[0])[3])); glyf = tt["glyf"]
                for gid, gname in enumerate(tt.getGlyphOrder()):
                    if glyf[gname].numberOfContours == 0:
                        mapping[(base, gid)] = " "; confirmed.add((base, gid))
            except Exception:
                pass
    pages_out, flags_out = [], []
    for page in doc:
        lines, flines = [], []
        rows = collections.defaultdict(list)
        for c in page_chars(page):
            rows[round(c[6] / 2)].append(c)          # aynı taban çizgisi = aynı satır
        for y in sorted(rows):
            out, fl, prev = [], [], None
            for c in sorted(rows[y], key=lambda c: c[1]):
                if prev is not None and c[1] - prev[3] > 0.2 * c[5]:
                    out.append(" "); fl.append(True)
                ch = mapping.get(c[0])
                out.append(ch if ch is not None else "\ufffd"); fl.append(c[0] in confirmed)
                prev = c
            lines.append("".join(out)); flines.append(fl)
        pages_out.append(lines); flags_out.append(flines)
    stats = {"glyphs": len(keys), "unanimous": len(confirmed & keys)}
    return pages_out, flags_out, stats


def unconfirmed_glyph_numbers(lines, flags):
    bad = []
    for line, fl in zip(lines, flags):
        for m in NUM_RE.finditer(line):
            if not all(fl[m.start():m.end()]):
                bad.append(m.group())
    return bad


def ocr_png(png: str, psm: str = "4") -> str:
    return run(["tesseract", png, "-", "-l", "tur", "--psm", psm])


def ocr_page_3pass(page, d, i):
    """Taranmış sayfa: 3 tur OCR. Temel metin 400 dpi/psm4; bir sayı ancak üç turun üçünde de
    aynı geçiyorsa onaylı. Not: sistematik hata (ör. '%' → '9') üç turda da aynı olabilir —
    bu yüzden OCR sayfalarındaki sayılar ayrıca sayfa görüntüsünden kontrol edilmelidir."""
    texts = []
    for dpi, psm in OCR_PASSES:
        png = f"{d}/o{i}_{dpi}_{psm}.png"
        page.get_pixmap(dpi=dpi, colorspace="gray").save(png)
        texts.append(ocr_png(png, psm))
    base = texts[1]
    sets = [collections.Counter(NUM_RE.findall(t)) for t in texts]
    bad = [n for n in NUM_RE.findall(base) if not all(s[n] for s in sets)]
    return base, bad


def pdf_text(p: Path) -> tuple[str, str, dict]:
    """1) pdftotext düzgünse onu kullan.
    2) Metin katmanı var ama font kodlaması bozuksa (ToUnicode yok): glifler 3 turlu OCR oylamasıyla çözülür,
       metin PDF'in kendi glif dizisinden kurulur.
    3) Metin katmanı olmayan (taranmış) sayfalar: 3 tur OCR, sayılar turlar arası karşılaştırılır."""
    t = run(["pdftotext", "-layout", str(p), "-"])
    if looks_turkish(t):
        return t, "pdftotext", {"passes": 1, "unconfirmed_numbers": []}
    doc = pymupdf.open(str(p))
    g_pages, g_flags, gstats = glyph_decode(str(p))
    out, methods, bad, ocr_pages = [], set(), [], []
    with tempfile.TemporaryDirectory() as d:
        for i, page in enumerate(doc):
            if len(page_chars(page)) >= 200:
                out.append(re.sub(r" {2,}", " ", "\n".join(l.rstrip() for l in g_pages[i])))
                bad += unconfirmed_glyph_numbers(g_pages[i], g_flags[i]); methods.add("glyph")
            else:
                txt, b = ocr_page_3pass(page, d, i)
                out.append(txt); bad += b; methods.add("ocr"); ocr_pages.append(i + 1)
    verification = {
        "passes": 3,
        "glyphs_unanimous": f"{gstats['unanimous']}/{gstats['glyphs']}",
        "ocr_pages": ocr_pages,
        "unconfirmed_numbers": bad,
        "note": "ocr_pages içindeki sayılar sayfa görüntüsünden ayrıca doğrulanmalı" if ocr_pages else "",
    }
    return "\n".join(out), "+".join(sorted(methods)), verification


def title_of(text: str, issuer_end: int) -> str:
    lines = [l.strip() for l in text[issuer_end:].splitlines() if l.strip()]
    title = []
    for l in lines[:8]:
        letters = [c for c in l if c.isalpha()]
        if letters and sum(c.isupper() for c in letters) / len(letters) > 0.8:
            title.append(l)
        elif title:
            break
    return " ".join(title)


def decisions_of(text: str) -> list[dict]:
    hits = list(DECISION_RE.finditer(text))
    out = []
    for i, h in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        out.append({"no": h.group(1), "date": h.group(2), "text": text[h.start():end].strip()})
    return out


def read_day(day: str, files: list[Path]) -> dict:
    items, issue = [], None
    for p in sorted(files, key=lambda x: int(NAME_RE.match(x.name).group(2) or 0)):
        n, ext = NAME_RE.match(p.name).group(2), NAME_RE.match(p.name).group(3)
        if n is None or ext == "txt":
            continue  # tam sayı PDF'i ve eski .txt dökümleri okunmaz
        if ext == "htm":
            text, method, ver = htm_text(p), "htm", {"passes": 1, "unconfirmed_numbers": [],
                                                    "note": "HTML metin — doğrudan okunur"}
        else:
            text, method, ver = pdf_text(p)
        issue = issue or (ISSUE_RE.search(text[:3000]).group(1) if ISSUE_RE.search(text[:3000]) else None)
        im = ISSUER_RE.search(text[:3000])
        issuer = im.group(1).strip() if im else ""
        title = title_of(text, im.end()) if im else ""
        items.append({
            "n": int(n),
            "file": p.name,
            "official_url": f"https://www.resmigazete.gov.tr/eskiler/{day[:4]}/{day[4:6]}/{day}-{n}.{ext}",
            "issuer": issuer,
            "title": title,
            "energy": any(w in norm(issuer + " " + title + " " + text[:2000]) for w in ENERGY_WORDS),
            "method": method,
            "chars": len(text),
            "verification": ver,
            "decisions": decisions_of(text),
            "text": text,
        })
    return {"date": f"{day[:4]}-{day[4:6]}-{day[6:]}", "issue": issue,
            "energy_count": sum(i["energy"] for i in items), "items": items}


def main(argv: list[str]) -> None:
    keep = "--keep" in argv
    days: dict[str, list[Path]] = {}
    for p in RG.iterdir():
        m = NAME_RE.match(p.name)
        if m:
            days.setdefault(m.group(1), []).append(p)
    if not days:
        print("rg/: işlenecek dosya yok")
        return
    for day, files in sorted(days.items()):
        res = read_day(day, files)
        out = RG / f"{day}.json"
        out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"OK {day}: {len(res['items'])} madde · {res['energy_count']} enerji · sayı {res['issue']} → {out.name}")
        for it in res["items"]:
            dec = f" · kararlar {','.join(d['no'] for d in it['decisions'])}" if it["decisions"] else ""
            v = it["verification"]
            chk = f" · onay {v.get('glyphs_unanimous','-')} · onaysız sayı {len(v['unconfirmed_numbers'])}" if it["method"] != "htm" else ""
            print(f"  -{it['n']} [{it['method']}] {'⚡' if it['energy'] else ' '} {it['title'][:80]}{dec}{chk}")
        if not keep:
            for p in files:
                p.unlink()
            print(f"  silindi: {len(files)} kaynak dosya")


if __name__ == "__main__":
    main(sys.argv[1:])
