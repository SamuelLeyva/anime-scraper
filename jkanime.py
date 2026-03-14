#!/usr/bin/env python3
"""
JKAnime Scraper
───────────────
Scrapea https://jkanime.net/ y extrae:
  1. Últimos episodios de la home
  2. Para cada episodio: todos los reproductores (Desu, Magi, Mega, Streamwish…)
  3. Links de descarga

Genera episodios.json listo para usar con index.html + ver.html

Uso:
    pip install requests beautifulsoup4
    python scraper_jkanime.py

Opciones:
    --out      Archivo de salida  (default: episodios.json)
    --max      Máx. episodios     (default: 40, 0=todos)
    --delay    Delay entre peticiones en segundos (default: 1.0)
    --workers  Hilos paralelos    (default: 4)
"""

import requests
from bs4 import BeautifulSoup
import json, re, time, base64, argparse, sys
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "https://jkanime.net"
CDN  = "https://cdn.jkdesa.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9",
    "Referer": BASE + "/",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ── HTTP ──────────────────────────────────────────────────────────────
def fetch(url: str, retries=3) -> str | None:
    for i in range(1, retries + 1):
        try:
            r = SESSION.get(url, timeout=15)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            print(f"  ✗ [{i}/{retries}] {url} → {e}")
            if i < retries:
                time.sleep(2 * i)
    return None


# ── PARSEAR HOME ──────────────────────────────────────────────────────
def parse_home(html: str) -> list[dict]:
    """
    Extrae tarjetas de la sección Programación (tab Animes).
    Selector: .dir1 .card a
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for card_wrap in soup.select(".dir1"):
        link = card_wrap.select_one("a[href]")
        if not link:
            continue

        href = link.get("href", "")
        # href = https://jkanime.net/anime-slug/episode-number/
        parts = href.rstrip("/").split("/")
        if len(parts) < 2:
            continue
        ep_num  = parts[-1]
        slug    = parts[-2]

        img     = link.select_one("img.card-img-top")
        title   = link.select_one("h5.card-title, .card-title")
        ep_badge = link.select_one(".badge-primary")
        time_badge = link.select_one(".badge-secondary")

        img_src    = img.get("src", "")   if img   else ""
        cover_src  = img.get("data-animepic", "") if img else ""
        title_txt  = title.get_text(strip=True) if title else slug
        ep_txt     = ep_badge.get_text(strip=True).replace("Ep ", "") if ep_badge else ep_num
        time_txt   = time_badge.get_text(strip=True) if time_badge else ""

        results.append({
            "title":      title_txt,
            "episode":    ep_txt,
            "slug":       slug,
            "url":        href,
            "image_url":  img_src,
            "cover_url":  cover_src,
            "aired":      time_txt,
            "servers":    [],
            "downloads":  [],
        })

    return results


# ── PARSEAR PÁGINA DE EPISODIO ────────────────────────────────────────
def parse_episode_page(html: str, ep: dict) -> dict:
    """
    Extrae de la página del episodio:
    - video[] array (Desu = video[0], Magi = video[1])
    - servers[] JS array → reproductores externos
    - Links de descarga
    - OG image / screenshot
    """
    ep = dict(ep)
    soup = BeautifulSoup(html, "html.parser")

    # Screenshot OG
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        ep["screenshot"] = og["content"]
    else:
        ep["screenshot"] = ep.get("image_url", "")

    # ── Extraer video[] ──────────────────────────────────────────────
    # var video[0] = '<iframe ... src="URL" ...></iframe>';
    # var video[1] = '<iframe ... src="URL" ...></iframe>';
    video_matches = re.findall(
        r"video\[(\d+)\]\s*=\s*'(<iframe[^']*>)'",
        html, re.DOTALL
    )

    video_map = {}
    for idx_str, iframe_html in video_matches:
        idx = int(idx_str)
        # Extraer src del iframe
        m = re.search(r'src=["\']([^"\']+)["\']', iframe_html)
        if m:
            src = m.group(1)
            # Resolver src relativo
            if src.startswith("/"):
                src = BASE + src
            video_map[idx] = src

    # Nombres para los primeros reproductores especiales de JK
    jk_names = {0: "Desu", 1: "Magi"}

    servers = []
    for idx, src in sorted(video_map.items()):
        if idx in jk_names:
            servers.append({
                "server": jk_names[idx],
                "title":  jk_names[idx],
                "iframe": src,
                "lang":   "SUB",
                "type":   "jk",
            })

    # ── Extraer servers[] JS array ───────────────────────────────────
    m_servers = re.search(
        r"var\s+servers\s*=\s*(\[.*?\]);",
        html, re.DOTALL
    )
    if m_servers:
        try:
            raw_servers = json.loads(m_servers.group(1))
        except json.JSONDecodeError:
            raw_servers = []

        downloads = []
        for s in raw_servers:
            remote_b64 = s.get("remote", "")
            server_name = s.get("server", "")
            slug_dl     = s.get("slug", "")
            size        = s.get("size", "")
            lang_id     = s.get("lang", 1)
            lang_label  = "SUB" if lang_id == 1 else "LAT"

            # Decodificar URL real
            try:
                real_url = base64.b64decode(remote_b64 + "==").decode("utf-8", errors="replace").strip()
            except Exception:
                real_url = ""

            # Mediafire: solo descarga, nunca reproductor
            if server_name.lower() != "mediafire":
                iframe_src = f"{BASE}/jkplayer/c1?u={remote_b64}&s={server_name.lower()}"
                servers.append({
                    "server":    server_name,
                    "title":     server_name,
                    "iframe":    iframe_src,
                    "real_url":  real_url,
                    "lang":      lang_label,
                    "size":      size,
                    "type":      "c1",
                })

            if slug_dl:
                downloads.append({
                    "server": server_name,
                    "size":   size,
                    "lang":   lang_label,
                    "url":    f"https://c1.jkplayers.com/d/{slug_dl}/",
                })

        ep["downloads"] = downloads

    ep["servers"] = servers
    return ep


# ── SCRAPE EPISODIO (wrapper con delay) ──────────────────────────────
def scrape_ep(ep: dict, delay: float) -> dict:
    time.sleep(delay)
    html = fetch(ep["url"])
    if not html:
        print(f"  ✗ No se pudo descargar: {ep['url']}")
        return ep
    return parse_episode_page(html, ep)


# ── MAIN ─────────────────────────────────────────────────────────────
def scrape(out="episodios.json", max_eps=40, delay=1.0, workers=4):
    print(f"\n{'='*56}")
    print(f"  JKAnime Scraper — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*56}\n")

    print("[1/3] Descargando home de jkanime.net...")
    home_html = fetch(BASE + "/")
    if not home_html:
        print("✗ No se pudo descargar la home.")
        sys.exit(1)

    print("[2/3] Parseando episodios de la home...")
    episodes = parse_home(home_html)
    if max_eps > 0:
        episodes = episodes[:max_eps]
    print(f"  ✓ {len(episodes)} episodios encontrados")

    print(f"\n[3/3] Extrayendo reproductores ({workers} hilos, delay={delay}s)...")
    enriched = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(scrape_ep, ep, delay): ep for ep in episodes}
        done = 0
        for fut in as_completed(futures):
            done += 1
            result = fut.result()
            enriched.append(result)
            n_srv = len(result.get("servers", []))
            n_dl  = len(result.get("downloads", []))
            print(f"  [{done:02d}/{len(episodes)}] {result['title'][:40]:<40} EP{result['episode']:>3}  → {n_srv} servers, {n_dl} descargas")

    # Mantener orden de la home
    order = {ep["slug"] + ep["episode"]: i for i, ep in enumerate(episodes)}
    enriched.sort(key=lambda e: order.get(e["slug"] + e["episode"], 9999))

    data = {
        "scraped_at": datetime.now().isoformat(),
        "source":     BASE,
        "episodes":   enriched,
    }

    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    total_srv = sum(len(e.get("servers", [])) for e in enriched)
    print(f"\n{'='*56}")
    print(f"  ✓ Guardado en:  {out}")
    print(f"  • Episodios:    {len(enriched)}")
    print(f"  • Servidores:   {total_srv} total")
    print(f"{'='*56}\n")


def main():
    p = argparse.ArgumentParser(description="JKAnime scraper → JSON")
    p.add_argument("--out",     default="episodios.json")
    p.add_argument("--max",     default=40,  type=int)
    p.add_argument("--delay",   default=1.0, type=float)
    p.add_argument("--workers", default=4,   type=int)
    args = p.parse_args()
    scrape(args.out, args.max, args.delay, args.workers)

if __name__ == "__main__":
    main()
