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
  .pdf  → pdftotext; metin Türkçe değilse (bozuk font kodlaması / taranmış sayfa) → OCR (tesseract -l tur)

Kullanım:
  python3 scripts/rg_reader.py            # rg/ içindeki tüm tarihler
  python3 scripts/rg_reader.py --keep     # kaynak dosyaları silme (test için)

Gerekenler: poppler-utils (pdftotext, pdftoppm), tesseract-ocr + tesseract-ocr-tur
"""
import html
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

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


def pdf_text(p: Path) -> tuple[str, str]:
    t = run(["pdftotext", "-layout", str(p), "-"])
    if looks_turkish(t):
        return t, "pdftotext"
    out = []
    with tempfile.TemporaryDirectory() as d:
        run(["pdftoppm", "-r", "300", "-gray", "-png", str(p), f"{d}/pg"])
        for img in sorted(Path(d).glob("pg-*.png"), key=lambda x: int(x.stem.split("-")[-1])):
            out.append(run(["tesseract", str(img), "-", "-l", "tur", "--psm", "4"]))
    return "\n".join(out), "ocr"


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
        text, method = (htm_text(p), "htm") if ext == "htm" else pdf_text(p)
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
            print(f"  -{it['n']} [{it['method']}] {'⚡' if it['energy'] else ' '} {it['title'][:90]}{dec}")
        if not keep:
            for p in files:
                p.unlink()
            print(f"  silindi: {len(files)} kaynak dosya")


if __name__ == "__main__":
    main(sys.argv[1:])
