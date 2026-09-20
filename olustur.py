#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
filmler.txt dosyasını okur ve her film için şunları yapar:
  - master listesini indirir (sesler korunur),
  - altyazıyı indirir, videoyla eşleşen zaman bilgisini ekler,
  - dosyalar/ klasörüne çok altyazılı HLS dosyalarını yazar,
  - liste.m3u dosyasını üretir (IPTV uygulamasına eklenecek olan).
"""
import concurrent.futures as cf
import hashlib
import math
import os
import posixpath
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Linux; Android 13; Tablet) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DILLER = {"tr": "Türkçe", "en": "English", "de": "Deutsch", "fr": "Français",
          "es": "Español", "it": "Italiano", "ru": "Русский", "ar": "العربية",
          "pt": "Português", "nl": "Nederlands", "ja": "日本語", "ko": "한국어"}
TR = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def depo_bul():
    """GitHub'da ortam değişkenlerinden, telefonda git adresinden depoyu bulur."""
    depo = os.environ.get("GITHUB_REPOSITORY")
    dal = os.environ.get("GITHUB_REF_NAME")
    if not depo:
        try:
            url = subprocess.check_output(["git", "config", "--get", "remote.origin.url"],
                                          text=True, stderr=subprocess.DEVNULL).strip()
            m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", url)
            if m:
                depo = m.group(1)
        except Exception:
            pass
    if depo and not dal:
        try:
            dal = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                          text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            pass
    return depo or "KULLANICI/DEPO", dal or "main"


DEPO, DAL = depo_bul()
BASE = "https://raw.githubusercontent.com/%s/%s" % (DEPO, DAL)
KLASOR = Path("dosyalar")
YEREL = Path("altyazilar")
VARSAYILAN_SURE = 4 * 3600
URL_RE = re.compile(r"^https?://\S+$")


# ---------- İndirme ----------
def indir(url, en_fazla=None):
    basliklar = {"User-Agent": UA, "Accept": "*/*"}
    if en_fazla:
        basliklar["Range"] = "bytes=0-%d" % (en_fazla - 1)
    istek = urllib.request.Request(url, headers=basliklar)
    with urllib.request.urlopen(istek, timeout=30) as yanit:
        return yanit.read(en_fazla) if en_fazla else yanit.read()


def metin_indir(url):
    veri = indir(url)
    for kodlama in ("utf-8-sig", "cp1254"):
        try:
            return veri.decode(kodlama)
        except UnicodeDecodeError:
            pass
    return veri.decode("utf-8", "replace")


def yerel_dosya(url):
    """altyazilar/ klasöründe adresin dosya adıyla aynı adlı dosya var mı?"""
    ad = posixpath.basename(urllib.parse.urlsplit(url).path)
    if ad and YEREL.is_dir():
        for d in YEREL.iterdir():
            if d.is_file() and d.name.lower() == ad.lower():
                return ad, d
    return ad, None


def dosya_oku(yol):
    veri = yol.read_bytes()
    for kodlama in ("utf-8-sig", "cp1254"):
        try:
            return veri.decode(kodlama)
        except UnicodeDecodeError:
            pass
    return veri.decode("utf-8", "replace")


# ---------- Video bilgisi ----------
def ilk_varyant(metin, master_url):
    satirlar = [s.strip() for s in metin.replace("\r", "").split("\n")]
    for i, s in enumerate(satirlar):
        if s.startswith("#EXT-X-STREAM-INF:"):
            for sonraki in satirlar[i + 1:]:
                if sonraki and not sonraki.startswith("#"):
                    return urllib.parse.urljoin(master_url, sonraki)
    return None


def medya_bilgisi(medya_url):
    """(toplam süre sn, ilk parça adresi, fMP4 mi)"""
    metin = metin_indir(medya_url)
    sure, ilk, fmp4 = 0.0, None, False
    for s in metin.replace("\r", "").split("\n"):
        s = s.strip()
        if s.startswith("#EXTINF:"):
            try:
                sure += float(s[8:].split(",")[0])
            except ValueError:
                pass
        elif s.startswith("#EXT-X-MAP:"):
            fmp4 = True
        elif s and not s.startswith("#") and ilk is None:
            ilk = urllib.parse.urljoin(medya_url, s)
    return sure, ilk, fmp4


def ts_ilk_pts(veri):
    """MPEG-TS verisindeki ilk videonun başlangıç zamanı (90 kHz)."""
    bas = None
    for i in range(min(len(veri) - 376, 4096)):
        if veri[i] == 0x47 and veri[i + 188] == 0x47 and veri[i + 376] == 0x47:
            bas = i
            break
    if bas is None:
        return None
    for ofs in range(bas, len(veri) - 187, 188):
        p = veri[ofs:ofs + 188]
        if p[0] != 0x47 or not (p[1] & 0x40):
            continue
        afc = (p[3] >> 4) & 3
        if afc == 2:
            continue
        i = 4
        if afc == 3:
            i += 1 + p[4]
        if i + 14 > 188 or p[i:i + 3] != b"\x00\x00\x01":
            continue
        if not (0xE0 <= p[i + 3] <= 0xEF) or not (p[i + 7] & 0x80):
            continue
        b = p[i + 9:i + 14]
        return (((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15
                | b[3] << 7 | (b[4] >> 1))
    return None


# ---------- Altyazı ----------
ZAMAN = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})")


def zaman_yaz(t):
    ms = int(round(max(0.0, t) * 1000))
    return "%02d:%02d:%02d.%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def vtt_isle(metin, pts, kaydir, konum=None):
    satirlar = metin.replace("\r", "").lstrip("\ufeff").split("\n")
    if pts is not None:   # kendi haritamızı yazacaksak eskisini sil
        satirlar = [s for s in satirlar if not s.startswith("X-TIMESTAMP-MAP")]
    while satirlar and not satirlar[0].strip():
        satirlar.pop(0)
    if not satirlar or not satirlar[0].startswith("WEBVTT"):
        satirlar[0:0] = ["WEBVTT", ""]   # .srt dosyası: başlık ekle

    def degistir(m):
        t = (int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60
             + int(m.group(3)) + int(m.group(4)) / 1000.0)
        return zaman_yaz(t + kaydir)

    for i, s in enumerate(satirlar):
        if "-->" in s:
            s = ZAMAN.sub(degistir, s)
            if konum is not None and "line:" not in s:
                s = s.rstrip() + " line:%d%%" % konum   # altyazıyı yukarı al
            satirlar[i] = s
    if pts is not None:
        satirlar.insert(1, "X-TIMESTAMP-MAP=MPEGTS:%d,LOCAL:00:00:00.000" % pts)
    return "\n".join(satirlar) + "\n"


def altyazilari_ayikla(metin):
    sonuc = []
    for parca in re.split(r"[\s,]+", metin.strip()):
        m = re.match(r"^([A-Za-z]{2,3})=(https?://\S+)$", parca)
        if m:
            sonuc.append((m.group(1).lower(), m.group(2)))
        elif URL_RE.match(parca):
            sonuc.append(("tr", parca))
    return sonuc


# ---------- Liste dosyaları ----------
def sarmalayici(url, sure):
    return "\n".join(["#EXTM3U", "#EXT-X-VERSION:4",
                      "#EXT-X-TARGETDURATION:%d" % math.ceil(sure),
                      "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD",
                      "#EXTINF:%.3f," % sure, url, "#EXT-X-ENDLIST"]) + "\n"


def grup_bul(metin):
    for s in metin.split("\n"):
        if s.startswith("#EXT-X-MEDIA:") and "TYPE=SUBTITLES" in s:
            m = re.search(r'GROUP-ID="([^"]+)"', s)
            if m:
                return m.group(1)
    return None


def master_birlestir(metin, master_url, medya_satirlari, gid):
    cikti, eklendi = [], False
    for ham in metin.replace("\r", "").split("\n"):
        s = ham.strip()
        if not s:
            continue
        if s.startswith("#EXT-X-STREAM-INF:"):
            if not eklendi:
                cikti += medya_satirlari
                eklendi = True
            if "SUBTITLES=" not in s:
                s += ',SUBTITLES="%s"' % gid
            cikti.append(s)
        elif s.startswith(("#EXT-X-I-FRAME-STREAM-INF:", "#EXT-X-MEDIA:")):
            cikti.append(re.sub(r'URI="([^"]*)"',
                                lambda m: 'URI="%s"' % urllib.parse.urljoin(master_url, m.group(1)), s))
        elif s.startswith("#"):
            cikti.append(s)
        else:
            cikti.append(urllib.parse.urljoin(master_url, s))
    if not cikti or cikti[0] != "#EXTM3U":
        cikti.insert(0, "#EXTM3U")
    return "\n".join(cikti) + "\n"


def slug(ad, url):
    s = re.sub(r"[^a-z0-9]+", "-", ad.translate(TR).lower()).strip("-")[:40] or "film"
    return "%s-%s" % (s, hashlib.md5(url.encode()).hexdigest()[:4])


def giris_satiri(f):
    parcalar = ["#EXTINF:-1"]
    if URL_RE.match(f["logo"]):
        parcalar.append('tvg-logo="%s"' % f["logo"].replace('"', "'"))
    if f["grup"]:
        parcalar.append('group-title="%s"' % f["grup"].replace('"', "'"))
    return " ".join(parcalar) + "," + f["ad"]


# ---------- Bir filmi işle ----------
def isle(f, ayar):
    master_url = f["master"]
    if not URL_RE.match(master_url):
        raise ValueError("master adresi eksik veya hatalı")
    altlar = altyazilari_ayikla(f["alt"])
    if not altlar:
        return {"giris": giris_satiri(f), "hedef": master_url, "dosyalar": {},
                "notlar": ["altyazı yok, video doğrudan bağlandı"]}

    metin = metin_indir(master_url)
    if not metin.lstrip().startswith("#EXTM3U"):
        raise ValueError("master listesi açılamadı (site GitHub'ı engellemiş olabilir)")
    ana_mi = "#EXT-X-STREAM-INF" in metin
    medya_url = ilk_varyant(metin, master_url) if ana_mi else master_url

    notlar, sure, pts = [], 0.0, None
    try:
        sure, ilk_parca, fmp4 = medya_bilgisi(medya_url)
        if ayar["harita"]:
            if fmp4:
                notlar.append("video fMP4, zaman haritası eklenemedi")
            elif ilk_parca:
                pts = ts_ilk_pts(indir(ilk_parca, 524288))
                if pts is None:
                    notlar.append("videonun başlangıç zamanı okunamadı")
    except Exception as e:
        notlar.append("video listesi okunamadı: %s" % e)
    if not sure:
        sure = VARSAYILAN_SURE
        notlar.append("süre bulunamadı, 4 saat varsayıldı")

    try:
        kaydir = float(f["kaydir"].replace(",", ".")) if f["kaydir"] else ayar["kaydir"]
    except ValueError:
        kaydir = ayar["kaydir"]

    ad = slug(f["ad"], master_url)
    mevcut = grup_bul(metin) if ana_mi else None
    gid = mevcut or "sub"
    dosyalar, medya = {}, []
    for i, (dil, url) in enumerate(altlar, 1):
        vad, lad = "%s-alt%d.vtt" % (ad, i), "%s-alt%d.m3u8" % (ad, i)
        dosya_adi, yerel = yerel_dosya(url)
        try:
            ham = dosya_oku(yerel) if yerel else metin_indir(url)
            ozgun = re.search(r"X-TIMESTAMP-MAP=(\S+)", ham)
            notlar.append("özgün altyazıda harita: %s" % (ozgun.group(1) if ozgun else "yok"))
            dosyalar[vad] = vtt_isle(ham, pts, kaydir, ayar["konum"])
            vtt_adresi = "%s/dosyalar/%s" % (BASE, vad)
            if yerel:
                notlar.append("%s altyazısı altyazilar/ klasöründen alındı" % dil)
        except Exception as e:
            notlar.append("%s altyazısı indirilemedi (%s); özgün adrese bağlandı. "
                          "Kendin yüklemek istersen dosyayı altyazilar/%s adıyla yükle"
                          % (dil, e, dosya_adi))
            vtt_adresi = url
        dosyalar[lad] = sarmalayici(vtt_adresi, sure)
        medya.append('#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="%s",NAME="%s",LANGUAGE="%s",'
                     'DEFAULT=%s,AUTOSELECT=YES,FORCED=NO,URI="%s/dosyalar/%s"'
                     % (gid, DILLER.get(dil, dil.upper()), dil,
                        "YES" if (i == 1 and not mevcut) else "NO", BASE, lad))

    if ana_mi:
        govde = master_birlestir(metin, master_url, medya, gid)
    else:
        govde = "\n".join(["#EXTM3U", "#EXT-X-VERSION:4"] + medya
                          + ['#EXT-X-STREAM-INF:BANDWIDTH=5000000,SUBTITLES="%s"' % gid,
                             master_url]) + "\n"
    dosyalar[ad + ".m3u8"] = govde
    if pts is not None:
        notlar.append("zaman haritası eklendi (%.2f sn)" % (pts / 90000.0))
    if kaydir:
        notlar.append("altyazı %+g sn kaydırıldı" % kaydir)
    return {"giris": giris_satiri(f), "hedef": "%s/dosyalar/%s.m3u8" % (BASE, ad),
            "dosyalar": dosyalar, "notlar": notlar}


# ---------- Ana akış ----------
def oku(dosya):
    ayar, filmler = {"harita": True, "kaydir": 0.0, "konum": None}, []
    # @dizi, @kategori ve @logo satırları, kendinden sonraki tüm satırlar için geçerlidir
    varsayilan = {"dizi": "", "kategori": "", "logo": ""}
    for no, ham in enumerate(Path(dosya).read_text(encoding="utf-8-sig").splitlines(), 1):
        s = ham.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("@"):
            k, _, v = s[1:].partition("=")
            k, v_ham = k.strip().lower(), v.strip()
            v = v_ham.lower()
            if k == "harita":
                ayar["harita"] = v not in ("hayir", "hayır", "yok", "0", "kapali", "kapalı")
            elif k == "konum":
                try:
                    ayar["konum"] = int(float(v.replace(",", "."))) if v else None
                except ValueError:
                    print("[UYARI] satır %d: @konum sayı olmalı (örn. 80)" % no)
            elif k == "kaydir":
                try:
                    ayar["kaydir"] = float(v.replace(",", "."))
                except ValueError:
                    print("[UYARI] satır %d: @kaydir sayı olmalı" % no)
            elif k in ("dizi", "kategori", "grup", "logo"):
                varsayilan["kategori" if k == "grup" else k] = v_ham
            else:
                print("[UYARI] satır %d: bilinmeyen ayar @%s" % (no, k))
            continue
        p = [x.strip() for x in re.split(r"\t|\|", s)] + [""] * 6
        ad = p[0] or "Adsız film"
        if varsayilan["dizi"]:
            ad = varsayilan["dizi"] + " " + ad
        filmler.append({"no": no, "ad": ad, "master": p[1], "alt": p[2],
                        "logo": p[3] or varsayilan["logo"],
                        "grup": p[4] or varsayilan["kategori"], "kaydir": p[5]})
    return ayar, filmler


def main():
    ayar, filmler = oku(sys.argv[1] if len(sys.argv) > 1 else "filmler.txt")
    with cf.ThreadPoolExecutor(max_workers=6) as havuz:
        vadeler = [havuz.submit(isle, f, ayar) for f in filmler]
    shutil.rmtree(KLASOR, ignore_errors=True)
    KLASOR.mkdir()
    liste, rapor, tamam = ["#EXTM3U"], [], 0
    for f, v in zip(filmler, vadeler):
        try:
            r = v.result()
        except Exception as e:
            rapor.append("[HATA]  %s: %s" % (f["ad"], e))
            continue
        for ad, icerik in r["dosyalar"].items():
            (KLASOR / ad).write_text(icerik, encoding="utf-8")
        liste += [r["giris"], r["hedef"]]
        tamam += 1
        rapor.append("[TAMAM] %s%s" % (f["ad"], (" (" + "; ".join(r["notlar"]) + ")") if r["notlar"] else ""))
    Path("liste.m3u").write_text("\n".join(liste) + "\n", encoding="utf-8")
    link = "%s/liste.m3u" % BASE
    metin = "IPTV linkin: %s\n%d film hazır, %d hatalı\n\n%s\n" % (
        link, tamam, len(filmler) - tamam, "\n".join(rapor))
    Path("rapor.txt").write_text(metin, encoding="utf-8")
    print(metin)
    ozet = os.environ.get("GITHUB_STEP_SUMMARY")
    if ozet:
        with open(ozet, "a", encoding="utf-8") as o:
            o.write("```\n" + metin + "```\n")


if __name__ == "__main__":
    main()
