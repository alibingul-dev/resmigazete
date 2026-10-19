
import requests
from datetime import datetime
from pathlib import Path


def indir_resmi_gazete(tarih: str = None, klasor: str = "rg"):
    """
    Resmî Gazete HTML ve PDF dosyalarını indirir.

    tarih formatı: YYYYMMDD (örn: 20261019)
    Boş bırakılırsa bugünün tarihini kullanır.

    Ana PDF:
    .pdf

    Ek dosyalar:
    -1.htm ... -20.htm
    -1.pdf ... -20.pdf
    """

    if tarih is None:
        tarih = datetime.now().strftime("%Y%m%d")

    yil = tarih[:4]
    ay = tarih[4:6]

    temel_url = (
        f"https://www.resmigazete.gov.tr/eskiler/"
        f"{yil}/{ay}/{tarih}"
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36"
        )
    }

    klasor_yolu = Path(klasor)
    klasor_yolu.mkdir(parents=True, exist_ok=True)

    indirilenler = []

    uzantilar = [".pdf"]

    for i in range(1, 21):
        uzantilar.append(f"-{i}.htm")
        uzantilar.append(f"-{i}.pdf")

    with requests.Session() as session:
        session.headers.update(headers)

        for uzanti in uzantilar:
            url = temel_url + uzanti

            try:
                response = session.get(url, timeout=60)

                if response.status_code != 200:
                    print(
                        f"Bulunamadı ({response.status_code}): {url}"
                    )
                    continue

                if uzanti.endswith(".pdf"):
                    if not response.content.startswith(b"%PDF-"):
                        print(f"Geçerli PDF değil: {url}")
                        continue

                elif uzanti.endswith(".htm"):
                    content_type = response.headers.get(
                        "Content-Type", ""
                    ).lower()

                    icerik = response.content[:1000].lower()

                    if (
                        "html" not in content_type
                        and b"<html" not in icerik
                    ):
                        print(f"Geçerli HTML değil: {url}")
                        continue

                dosya_adi = f"Resmi_Gazete_{tarih}{uzanti}"
                yol = klasor_yolu / dosya_adi

                yol.write_bytes(response.content)
                indirilenler.append(str(yol))

                boyut = len(response.content) / 1024 / 1024

                print(
                    f"İndirildi: {yol} ({boyut:.2f} MB)"
                )

            except requests.RequestException as hata:
                print(f"İndirme hatası: {url} - {hata}")

    print(f"\nToplam indirilen dosya: {len(indirilenler)}")

    return indirilenler


if __name__ == "__main__":
    indir_resmi_gazete()
