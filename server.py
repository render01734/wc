#!/usr/bin/env python3
"""
Cuberite Minecraft Sunucu — Tek Dosya Yöneticisi
─────────────────────────────────────────────────
• Tüm .ini config dosyalarını diske yazar
• Port 8080'de HTTP durum sayfası çalıştırır
• bore.pub adresini otomatik yakalar ve gösterir
"""

import http.server
import os
import pathlib

# ══════════════════════════════════════════════════════════════
#  CONFIG DOSYALARI  (hepsi burada, ayrı .ini dosyası yok)
# ══════════════════════════════════════════════════════════════

SETTINGS_INI = """
[Authentication]
# Cuberite'de offline mod için doğru anahtar: Authenticate=0
Authenticate=0
OnlineMode=0
ServerID=CuberiteServer
PlayerRestrictIP=0

[Server]
Description=Lightweight Minecraft Server (bore.pub)
MaxPlayers=10
Ports=25565
ResourcePackUrl=
ResourcePackHash=

[Worlds]
DefaultWorld=world
World=world

[Deadlock]
DeadlockDetect=1
IntervalSec=20

[Limits]
MaxPlayerInWorld=10

[Ranking]
DbFile=Ranking.sqlite

[RCON]
Enabled=0

[SlowSQL]
LogSlowQueries=0
SlowQueryThresholdMs=100
""".strip()

WEBADMIN_INI = """
[WebAdmin]
Enabled=0
Port=8081
""".strip()

WORLD_INI = """
[General]
Gamemode=0
WorldType=FLAT
AllowCommands=1
AllowFlight=1

[Mechanics]
CommandBlocksEnabled=0
UseChatPrefixes=1

[SpawnPosition]
MaxViewDistance=4
X=0
Y=64
Z=0

[Lighting]
AnimatedTimeSpeed=20

[Mobs]
AnimalsOn=0
MaxAnimals=0
MaxMonsters=0
MonstersOn=0
WolvesOn=0

[Weather]
ChangeWeather=0

[Tick]
TicksPerSecond=20
""".strip()

GROUPS_INI = """
[Default]
Permissions=core.spawn,core.help,core.list
Color=f
Inherits=

[Admin]
Permissions=*
Color=c
Inherits=Default
""".strip()

# ──────────────────────────────────────────────────────────────
#  Config → disk'e yaz
# ──────────────────────────────────────────────────────────────

def write_configs(server_dir: str = "/server") -> None:
    """Tüm .ini dosyalarını Cuberite'nin beklediği yerlere yazar.
    server_dir: Cuberite binary'sinin bulunduğu dizin."""

    # Binary'yi bul, gerçek dizine yaz
    import glob
    bins = glob.glob("/server/**/Cuberite", recursive=True)
    if bins:
        server_dir = str(pathlib.Path(bins[0]).parent)
        print(f"[CFG] Cuberite dizini tespit edildi: {server_dir}")

    files = {
        f"{server_dir}/settings.ini":      SETTINGS_INI,
        f"{server_dir}/webadmin.ini":      WEBADMIN_INI,
        f"{server_dir}/world/world.ini":   WORLD_INI,
        f"{server_dir}/groups.ini":        GROUPS_INI,
    }

    for path, content in files.items():
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(path).write_text(content + "\n", encoding="utf-8")
        print(f"[CFG] Yazıldı: {path}")


# ══════════════════════════════════════════════════════════════
#  HTTP DURUM SAYFASI
# ══════════════════════════════════════════════════════════════

BORE_ADDR_FILE = "/tmp/bore_address.txt"
PORT = int(os.environ.get("PORT", 8080))

HTML = """\
<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="10">
  <title>🎮 Minecraft Sunucu</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#1a1a2e;color:#e0e0e0;font-family:'Courier New',monospace;
          display:flex;align-items:center;justify-content:center;
          min-height:100vh;padding:20px}}
    .card{{background:#16213e;border:2px solid #0f3460;border-radius:12px;
           padding:40px;max-width:600px;width:100%;text-align:center;
           box-shadow:0 0 30px rgba(15,52,96,.5)}}
    h1{{font-size:2rem;color:#4ecca3;margin-bottom:8px}}
    .sub{{color:#888;margin-bottom:30px;font-size:.9rem}}
    .addr{{background:#0f3460;border:1px solid #4ecca3;border-radius:8px;
           padding:20px;margin:20px 0;font-size:1.3rem;color:#4ecca3;
           letter-spacing:1px;word-break:break-all}}
    .wait{{color:#f8b400;font-size:1.1rem}}
    .info{{font-size:.85rem;color:#666;margin-top:20px}}
    .dot{{display:inline-block;width:10px;height:10px;background:#4ecca3;
          border-radius:50%;margin-right:6px;animation:pulse 1.5s infinite}}
    @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
    .badge{{display:inline-block;background:#0f3460;border:1px solid #4ecca3;
            color:#4ecca3;font-size:.75rem;padding:3px 10px;
            border-radius:20px;margin:4px}}
  </style>
</head>
<body>
<div class="card">
  <h1>⛏️ Minecraft Sunucu</h1>
  <p class="sub">Cuberite &bull; Offline Mode &bull; bore.pub Tunnel</p>
  {section}
  <div style="margin-top:25px">
    <span class="badge">🪶 Ultra Hafif</span>
    <span class="badge">🔓 Crack Girişi</span>
    <span class="badge">🌍 Düz Dünya</span>
    <span class="badge">⚡ Performans Modu</span>
  </div>
  <div class="info">Sayfa her 10 saniyede otomatik yenilenir.<br>
  Sunucu başlarken adres 1-2 dakika içinde görünür.</div>
</div>
</body>
</html>"""


def _get_bore_address() -> str | None:
    try:
        return pathlib.Path(BORE_ADDR_FILE).read_text().strip() or None
    except OSError:
        return None


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):                                   # noqa: N802
        addr = _get_bore_address()
        if addr:
            section = (
                f'<p style="margin-bottom:8px"><span class="dot"></span>Sunucu Çalışıyor</p>'
                f'<div class="addr">🌐 {addr}</div>'
                f'<p style="color:#aaa;font-size:.9rem">Minecraft\'ta <b>Sunucu Ekle</b> → bu adresi gir</p>'
            )
        else:
            section = (
                '<p class="wait">⏳ Tunnel başlatılıyor… (~1-2 dk)</p>'
                '<p style="color:#666;font-size:.85rem;margin-top:10px">Lütfen bekleyin ve sayfayı yenileyin.</p>'
            )
        body = HTML.format(section=section).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass  # sessiz


def run_http_server() -> None:
    server = http.server.HTTPServer(("0.0.0.0", PORT), _Handler)
    print(f"[HTTP] Durum sayfası: http://0.0.0.0:{PORT}")
    server.serve_forever()


# ══════════════════════════════════════════════════════════════
#  GİRİŞ NOKTASI
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    # Çağrı biçimi:
    #   python3 server.py config          → sadece .ini dosyalarını yaz
    #   python3 server.py http            → sadece HTTP sunucuyu başlat
    #   python3 server.py config_and_http → ikisini birden yap (start.sh kullanır)
    #   python3 server.py                 → varsayılan: config + http

    mode = sys.argv[1] if len(sys.argv) > 1 else "config_and_http"

    if mode in ("config", "config_and_http"):
        write_configs()

    if mode in ("http", "config_and_http"):
        run_http_server()
