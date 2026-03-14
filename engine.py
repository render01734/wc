#!/usr/bin/env python3
"""
⛏️  Minecraft Distributed World Engine  —  Tek Dosya (MAX OPTIMIZATION)
═══════════════════════════════════════════════════════════
  • MC 1.8 offline protokol MITM (şifresiz → tam kontrol)
  • Cross-server entity sync (PvP, Chat, Blok)
  • Anti-Dupe: 10 Saniyede Bir Otomatik Kayıt
  • MAX PERFORMANCE: ViewDistance=4, Network Threshold Tweak
"""

import asyncio, json, os, pathlib, struct, sys
import threading, zlib, time, http.server, urllib.request, urllib.parse
import subprocess, glob, uuid as _uuid_mod
from collections import deque
import datetime

# ══════════════════════════════════════════════════════════
#  CANLI LOG TAMPONU  (SSE konsolu için)
# ══════════════════════════════════════════════════════════

_LOG_BUF     = deque(maxlen=300) # RAM tasarrufu için 500'den 300'e çekildi
_LOG_LOCK    = threading.Lock()
_SSE_CLIENTS = []
_SSE_LOCK    = threading.Lock()

_LOG_COLORS = {
    "[CONN]":   "#4ecca3", "[JOIN]":   "#4ecca3",
    "[QUIT]":   "#f8b400", "[BORE]":   "#7ec8e3",
    "[REG]":    "#a8edea", "[UNREG]":  "#f8b400",
    "[PROXY]":  "#4ecca3", "[HTTP]":   "#555",
    "[MC]":     "#c5a3ff", "[CFG]":    "#555",
    "[STATE]":  "#555",    "[ERR]":    "#ff6b6b",
    "[WARN]":   "#f8b400", "[HEALTH]": "#f8b400",
    "[PVP]":    "#ff6b6b", "[START]":  "#4ecca3",
    "[SYNC]":   "#c5a3ff", "[YAVER]":  "#f9a8d4",
}

def _log_color(line):
    for tag, color in _LOG_COLORS.items():
        if tag in line: return color
    return "#c8c8c8"

class _TeeLogger:
    def __init__(self, orig): self._orig = orig
    def write(self, text):
        self._orig.write(text)
        stripped = text.strip()
        if stripped:
            ts    = datetime.datetime.now().strftime("%H:%M:%S")
            entry = {"ts": ts, "msg": stripped, "color": _log_color(stripped)}
            with _LOG_LOCK: _LOG_BUF.append(entry)
            payload = f"data: {json.dumps(entry)}\n\n"
            with _SSE_LOCK:
                dead = []
                for q in _SSE_CLIENTS:
                    try:    q.put_nowait(payload)
                    except: dead.append(q)
                for q in dead: _SSE_CLIENTS.remove(q)
    def flush(self):  self._orig.flush()
    def isatty(self): return False

sys.stdout = _TeeLogger(sys.stdout)
sys.stderr = _TeeLogger(sys.stderr)

MODE          = os.environ.get("ENGINE_MODE", "gameserver")
HTTP_PORT     = int(os.environ.get("PORT", 8080))
MC_PORT       = int(os.environ.get("MC_PORT", 25565))
DATA_DIR      = os.environ.get("DATA_DIR", "/data")
SERVER_DIR    = os.environ.get("SERVER_DIR", "/server")
BORE_FILE     = "/tmp/bore_address.txt"
STATE_FILE    = f"{DATA_DIR}/world_state.json"
BACKENDS_FILE = f"{DATA_DIR}/backends.json"

# OPTİMİZASYON: NetworkCompressionThreshold 256'ya çekildi, gereksiz işlemci yükü azaltıldı.
SETTINGS_INI = """
[Authentication]
Authenticate=0
OnlineMode=0
ServerID=CuberiteEngine
PlayerRestrictIP=0

[Plugins]
Plugin=WCSync
Plugin=Yaver

[Server]
Description=Distributed World Engine
MaxPlayers=999
Ports=25565
NetworkCompressionThreshold=256 

[Worlds]
DefaultWorld=world
World=world

[Deadlock]
DeadlockDetect=1
IntervalSec=20

[Limits]
MaxPlayerInWorld=999

[Ranking]
DbFile=Ranking.sqlite

[RCON]
Enabled=0
""".strip()

WEBADMIN_INI = "[WebAdmin]\nEnabled=0\nPort=8081"

# OPTİMİZASYON: MaxViewDistance 4 yapıldı. Render 512MB RAM'de devasa fark yaratır!
WORLD_INI = """
[General]
Gamemode=1
WorldType=FLAT
AllowCommands=1
AllowFlight=1

[Mechanics]
CommandBlocksEnabled=0
UseChatPrefixes=1

[SpawnPosition]
MaxViewDistance=4 
X=0
Y=5
Z=0

[Lighting]
AnimatedTimeSpeed=20

[Mobs]
AnimalsOn=1
MaxAnimals=20
MaxMonsters=15
MonstersOn=1
WolvesOn=1

[Weather]
ChangeWeather=0

[Tick]
TicksPerSecond=20

[Generator]
BiomeGen=Constant
ShapeGen=HeightMap
HeightGen=Flat
FlatHeight=1
CompositionGen=SameBlock
SameBlockType=bedrock
SameBlockBedrocked=0
Finishers=
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

PLUGIN_INFO = """
g_PluginInfo = {
    Name = "WCSync",
    Version = "1",
    Date = "2026-03-14",
    Description = "Merkezi Envanter Senkronizasyonu (Anti-Dupe)"
}
"""

YAVER_PLUGIN_INFO = """
g_PluginInfo = {
    Name = "Yaver",
    Version = "3",
    Date = "2026-03-14",
    Description = "Gelismis Yaver (Dost Kurt) Sistemi — Savaş Zekası, Çanta, Seviye"
}
"""

YAVER_PLUGIN_MAIN = """
local Yaverler = {}
local IsimDegistirenler = {}
local TickSayaci = 0

function Initialize(Plugin)
    Plugin:SetName("Yaver")
    Plugin:SetVersion(3)

    cPluginManager:AddHook(cPluginManager.HOOK_PLAYER_JOINED,           OnPlayerJoined)
    cPluginManager:AddHook(cPluginManager.HOOK_PLAYER_USING_ITEM,       OnPlayerUsingItem)
    cPluginManager:AddHook(cPluginManager.HOOK_PLAYER_WINDOW_CLICK,     OnPlayerWindowClick)
    cPluginManager:AddHook(cPluginManager.HOOK_PLAYER_TOSSED_ITEM,      OnPlayerTossedItem)
    cPluginManager:AddHook(cPluginManager.HOOK_CHAT,                    OnChat)
    cPluginManager:AddHook(cPluginManager.HOOK_PLAYER_RIGHT_CLICKED_ENTITY, OnRightClickEntity)
    cPluginManager:AddHook(cPluginManager.HOOK_PLAYER_BROKEN_BLOCK,     OnBlockBroken)
    cPluginManager:AddHook(cPluginManager.HOOK_ENTITY_TAKE_DAMAGE,      OnEntityTakeDamage)
    cPluginManager:AddHook(cPluginManager.HOOK_TICK,                    OnTick)

    LOG("[YAVER] Gelismis Zeka, Q Kisayolu ve Savas modulleri aktif!")
    return true
end

-- Oyuncu girisinde envanterinin 9. slotuna Yaver Menusu esyasini ver
function OnPlayerJoined(Player)
    local menuEsyasi = cItem(E_ITEM_NETHER_STAR)
    menuEsyasi.m_CustomName = "\\xc2\\xa7eYaver Menusu \\xc2\\xa77(Sag Tik / Q Tusu)"
    Player:GetInventory():SetHotbarSlot(8, menuEsyasi)
    -- Proxy'e bildir (WCSync ile uyumlu)
    LOG("YAVER_JOIN:" .. Player:GetName() .. ":" .. Player:GetUUID())
end

-- Q tusu ile canta acma (esya firlatma hook'u araciligiyla)
function OnPlayerTossedItem(Player, ItemX, ItemY, ItemZ, Item)
    if Item.m_ItemType == E_ITEM_NETHER_STAR
       and Item.m_CustomName == "\\xc2\\xa7eYaver Menusu \\xc2\\xa77(Sag Tik / Q Tusu)" then
        local uuid = Player:GetUUID()
        if Yaverler[uuid] and Yaverler[uuid].Inventory then
            Player:OpenWindow(Yaverler[uuid].Inventory)
            Player:SendMessageSuccess("Yaver cantasi acildi!")
        else
            Player:SendMessageFailure("Once menuye sag tiklayip yaverini cagirmalisin!")
        end
        return true  -- Esyanin yere dusmesini engeller
    end
    return false
end

-- Sag tik ile ana menu
function OnPlayerUsingItem(Player, BlockX, BlockY, BlockZ, BlockFace, CursorX, CursorY, CursorZ)
    local item = Player:GetEquippedItem()
    if item.m_ItemType == E_ITEM_NETHER_STAR
       and item.m_CustomName == "\\xc2\\xa7eYaver Menusu \\xc2\\xa77(Sag Tik / Q Tusu)" then
        ArayuzAc(Player)
        return true
    end
    return false
end

function ArayuzAc(Player)
    local Window = cLuaWindow:Create(cWindow.wtChest, 1, 9, "\\xc2\\xa7lYaver Yonetimi")

    local cagir = cItem(E_ITEM_BONE)
    cagir.m_CustomName = "\\xc2\\xa7aYaveri Cagir / Bilgi"
    Window:SetSlot(0, 2, cagir)

    local canta = cItem(E_BLOCK_CHEST)
    canta.m_CustomName = "\\xc2\\xa76Cantayi Ac (Kisayol: Q)"
    Window:SetSlot(0, 4, canta)

    local isim = cItem(E_ITEM_NAME_TAG)
    isim.m_CustomName = "\\xc2\\xa7dIsim Degistir"
    Window:SetSlot(0, 6, isim)

    Player:OpenWindow(Window)
end

function OnPlayerWindowClick(Player, Window, SlotNum, ClickAction, ClickedItem)
    if Window:GetWindowTitle() == "\\xc2\\xa7lYaver Yonetimi" then
        local uuid = Player:GetUUID()

        -- Slot 2: Yaveri cagir veya bilgileri goster
        if SlotNum == 2 then
            if not Yaverler[uuid] then
                local entityID = Player:GetWorld():SpawnMob(
                    Player:GetPosX(), Player:GetPosY(), Player:GetPosZ(), cMonster.mtWolf)
                Player:GetWorld():DoWithEntityByID(entityID, function(Entity)
                    local Wolf = tolua.cast(Entity, "cWolf")
                    Wolf:SetIsTame(true)
                    Wolf:SetOwner(Player:GetName())
                    Wolf:SetCustomName(Player:GetName() .. "'in Yaveri")
                    Wolf:SetCustomNameAlwaysVisible(true)
                    Yaverler[uuid] = {
                        EntityID  = entityID,
                        Level     = 1,
                        XP        = 0,
                        Inventory = cLuaWindow:Create(cWindow.wtChest, 3, 9, "Yaver Cantasi")
                    }
                    Player:SendMessageSuccess("Yaverin savasa hazir!")
                    LOG("YAVER_SPAWN:" .. Player:GetName() .. ":Sv1")
                end)
            else
                local y = Yaverler[uuid]
                Player:SendMessageInfo("\\xc2\\xa7a--- Yaver Bilgileri ---")
                Player:SendMessageInfo("Seviye: " .. y.Level .. " | XP: " .. y.XP
                    .. "/" .. (y.Level * 100))
            end

        -- Slot 4: Cantayi ac
        elseif SlotNum == 4 then
            if Yaverler[uuid] and Yaverler[uuid].Inventory then
                Player:CloseWindow()
                Player:OpenWindow(Yaverler[uuid].Inventory)
                return true
            end

        -- Slot 6: Isim degistir
        elseif SlotNum == 6 then
            if Yaverler[uuid] then
                IsimDegistirenler[uuid] = true
                Player:CloseWindow()
                Player:SendMessageInfo(
                    "\\xc2\\xa7eLutfen sohbete (chat) yaverinin yeni ismini yazin.")
                return true
            end
        end

        Player:CloseWindow()
        return true
    end
    return false
end

-- Chat'ten isim degistirme
function OnChat(Player, Message)
    local uuid = Player:GetUUID()
    if IsimDegistirenler[uuid] then
        if Yaverler[uuid] then
            Player:GetWorld():DoWithEntityByID(Yaverler[uuid].EntityID, function(Entity)
                Entity:SetCustomName(Message)
                Entity:SetCustomNameAlwaysVisible(true)
                Player:SendMessageSuccess("Yaverinin ismi '" .. Message .. "' oldu!")
            end)
        end
        IsimDegistirenler[uuid] = nil
        return true  -- Mesaji sohbete dusmesini engeller
    end
    return false
end

-- Kurt sahibine sag tiklaninca canta acar
function OnRightClickEntity(Player, Entity)
    if not Entity:IsMob() or Entity:GetMobType() ~= cMonster.mtWolf then return false end
    local Wolf = tolua.cast(Entity, "cWolf")
    if Wolf:GetOwnerName() ~= Player:GetName() then return false end
    local uuid = Player:GetUUID()
    if Yaverler[uuid] and Yaverler[uuid].Inventory then
        Player:OpenWindow(Yaverler[uuid].Inventory)
        return true
    end
    return false
end

-- Blok kirma yardimi + XP kazanma
function OnBlockBroken(Player, BlockX, BlockY, BlockZ, BlockFace, BlockType, BlockMeta)
    local uuid = Player:GetUUID()
    if not Yaverler[uuid] then return false end
    local World    = Player:GetWorld()
    local isNear   = false

    World:DoWithEntityByID(Yaverler[uuid].EntityID, function(Entity)
        local dist = (Entity:GetPosX() - BlockX)^2 + (Entity:GetPosZ() - BlockZ)^2
        if dist < 64 then isNear = true end
    end)

    if not isNear then return false end

    -- Seviye basvuru bonusu: Sv1=%7, Sv5=%15, vs.
    local yardimIhtimali = 5 + (Yaverler[uuid].Level * 2)

    if math.random(1, 100) <= yardimIhtimali then
        if BlockType == E_BLOCK_LOG or BlockType == E_BLOCK_LOG_UPDATE then
            World:SpawnItemPickups(cItems(cItem(E_BLOCK_LOG, 1, BlockMeta)), BlockX, BlockY, BlockZ)
            Yaverler[uuid].XP = Yaverler[uuid].XP + 2
        elseif BlockType == E_BLOCK_IRON_ORE
            or BlockType == E_BLOCK_GOLD_ORE
            or BlockType == E_BLOCK_DIAMOND_ORE then
            World:SpawnItemPickups(cItems(cItem(BlockType, 1, BlockMeta)), BlockX, BlockY, BlockZ)
            Yaverler[uuid].XP = Yaverler[uuid].XP + 5
        end
        CheckLevelUp(Player, Yaverler[uuid], nil)
    end
    return false
end

-- Savas Zekasi: Hasar carpani (sv basa +1 hasar) ve olum tespiti
function OnEntityTakeDamage(Receiver, Attacker, RawDamageType, RawDamage, DamageCalc)
    -- Yaver saldiriyorsa seviyeye gore ekstra hasar ekle
    if Attacker ~= nil
       and Attacker:IsMob()
       and Attacker:GetMobType() == cMonster.mtWolf then
        local Wolf = tolua.cast(Attacker, "cWolf")
        cRoot:Get():FindAndDoWithPlayer(Wolf:GetOwnerName(), function(Player)
            local yInfo = Yaverler[Player:GetUUID()]
            if yInfo then
                DamageCalc:AddDamage(yInfo.Level)  -- Sv basi +1 hasar
            end
        end)
    end

    -- Yaver hasar aliyorsa ve olurse kaydi temizle
    if Receiver:IsMob() and Receiver:GetMobType() == cMonster.mtWolf then
        local Wolf = tolua.cast(Receiver, "cWolf")
        if Wolf:GetHealth() - DamageCalc:GetFinalDamage() <= 0 then
            cRoot:Get():FindAndDoWithPlayer(Wolf:GetOwnerName(), function(Player)
                local uuid = Player:GetUUID()
                LOG("YAVER_DEAD:" .. Player:GetName())
                Yaverler[uuid] = nil
                Player:SendMessageFailure(
                    "Yaverin agir yaralandi! Onu arayuzden tekrar cagirmalisin.")
            end)
        end
    end
    return false
end

-- Gelismis Zeka: Otomatik yemek yeme (saniyede 1 kontrol)
function OnTick(TimeDelta)
    TickSayaci = TickSayaci + 1
    if TickSayaci < 20 then return end
    TickSayaci = 0

    for uuid, yInfo in pairs(Yaverler) do
        cRoot:Get():FindAndDoWithPlayer(uuid, function(Player)
            Player:GetWorld():DoWithEntityByID(yInfo.EntityID, function(Entity)
                local Wolf   = tolua.cast(Entity, "cWolf")
                local hp     = Wolf:GetHealth()
                local maxHp  = Wolf:GetMaxHealth()

                if hp < (maxHp / 2) then
                    local envanter = yInfo.Inventory
                    for i = 0, 26 do
                        local item = envanter:GetSlot(0, i)
                        if item.m_ItemType == E_ITEM_COOKED_BEEF
                           or item.m_ItemType == E_ITEM_RAW_BEEF then
                            item.m_ItemCount = item.m_ItemCount - 1
                            if item.m_ItemCount <= 0 then item:Empty() end
                            envanter:SetSlot(0, i, item)
                            Wolf:Heal(8)
                            Player:SendMessageInfo(
                                "\\xc2\\xa7aYaverin cantasindaki eti yedi ve canini yeniledi!")
                            break
                        end
                    end
                end
            end)
        end)
    end
end

function CheckLevelUp(Player, yInfo, EntityInstance)
    local reqXP = yInfo.Level * 100
    if yInfo.XP < reqXP then return end

    yInfo.Level = yInfo.Level + 1
    yInfo.XP    = 0
    Player:SendMessageSuccess(
        "\\xc2\\xa76Tebrikler! Yaverin Seviye " .. yInfo.Level
        .. " oldu! Artik daha sert vuruyor.")
    LOG("YAVER_LEVELUP:" .. Player:GetName() .. ":Sv" .. yInfo.Level)

    local function applyBuff(Ent)
        Ent:SetMaxHealth(20 + (yInfo.Level * 5))
        Ent:Heal(100)
    end

    if EntityInstance then
        applyBuff(EntityInstance)
    else
        Player:GetWorld():DoWithEntityByID(yInfo.EntityID, applyBuff)
    end
end
"""

PLUGIN_MAIN = """
function Initialize(Plugin)
    Plugin:SetName("WCSync")
    Plugin:SetVersion(1)
    cPluginManager:AddHook(cPluginManager.HOOK_PLAYER_JOINED, OnPlayerJoined)
    cPluginManager:AddHook(cPluginManager.HOOK_PLAYER_DESTROYED, OnPlayerDestroyed)
    cPluginManager:BindConsoleCommand("wcreload", HandleConsoleReload, "Python tetikleyici")
    
    -- Anti-Dupe: Her 10 saniyede bir tum oyunculari diske yaz
    cRoot:Get():GetDefaultWorld():ScheduleTask(200, PeriodicSave)
    
    LOG("[SYNC] WCSync aktif! Anti-Dupe devrede.")
    return true
end

function PeriodicSave(World)
    World:ForEachPlayer(function(Player)
        Player:SaveToDisk()
        LOG("WCSYNC_SAVE:" .. Player:GetName() .. ":" .. Player:GetUUID())
    end)
    World:ScheduleTask(200, PeriodicSave)
end

function OnPlayerJoined(Player)
    LOG("WCSYNC_JOIN:" .. Player:GetName() .. ":" .. Player:GetUUID())
end

function OnPlayerDestroyed(Player)
    Player:SaveToDisk()
    LOG("WCSYNC_QUIT:" .. Player:GetName() .. ":" .. Player:GetUUID())
end

function HandleConsoleReload(Split)
    if #Split > 1 then
        local name = Split[2]
        cRoot:Get():FindAndDoWithPlayer(name, function(P)
            P:LoadFromDisk()
            P:SendMessageSuccess("Verileriniz merkezden esitlendi!")
        end)
    end
    return true
end
"""

def write_configs(server_dir=SERVER_DIR):
    bins = glob.glob(f"{server_dir}/**/Cuberite", recursive=True)
    if bins:
        server_dir = str(pathlib.Path(bins[0]).parent)
    
    pathlib.Path(f"{DATA_DIR}/players").mkdir(parents=True, exist_ok=True)
    
    files = {
        f"{server_dir}/settings.ini":    SETTINGS_INI,
        f"{server_dir}/webadmin.ini":    WEBADMIN_INI,
        f"{server_dir}/world/world.ini": WORLD_INI,
        f"{server_dir}/groups.ini":      GROUPS_INI,
        f"{server_dir}/Plugins/WCSync/Info.lua":  PLUGIN_INFO,
        f"{server_dir}/Plugins/WCSync/main.lua":  PLUGIN_MAIN,
        f"{server_dir}/Plugins/Yaver/Info.lua":   YAVER_PLUGIN_INFO,
        f"{server_dir}/Plugins/Yaver/main.lua":   YAVER_PLUGIN_MAIN,
        "/server/world/world.ini":       WORLD_INI,
    }
    for path, content in files.items():
        try:
            pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(path).write_text(content.strip() + "\n", encoding="utf-8")
        except Exception as e:
            print(f"[CFG] HATA {path}: {e}")


# ══════════════════════════════════════════════════════════
#  MC 1.8 PROTOKOLü  (protocol 47, offline = sifresiz)
# ══════════════════════════════════════════════════════════

def vi_enc(v):
    r = bytearray()
    while True:
        b = v & 0x7F; v >>= 7
        if v: b |= 0x80
        r.append(b)
        if not v: break
    return bytes(r)

def vi_dec(data, pos=0):
    r = shift = 0
    while True:
        b = data[pos]; pos += 1
        r |= (b & 0x7F) << shift
        if not (b & 0x80): return r, pos
        shift += 7
        if shift >= 35: raise ValueError("VarInt too long")

async def vi_rd(reader):
    r = shift = 0
    while True:
        b = (await reader.readexactly(1))[0]
        r |= (b & 0x7F) << shift
        if not (b & 0x80): return r
        shift += 7
        if shift >= 35: raise ValueError("VarInt too long")

async def pkt_read(reader, comp=-1):
    length = await vi_rd(reader)
    raw = await reader.readexactly(length)
    if comp < 0:
        pid, pos = vi_dec(raw)
        return pid, raw[pos:], vi_enc(length) + raw
    data_len, pos = vi_dec(raw)
    inner = raw[pos:]
    if data_len == 0:
        pid, p2 = vi_dec(inner)
        return pid, inner[p2:], vi_enc(length) + raw
    dec = zlib.decompress(inner)
    pid, p2 = vi_dec(dec)
    return pid, dec[p2:], vi_enc(length) + raw

def pkt_make(pid, payload, comp=-1):
    data = vi_enc(pid) + payload
    if comp < 0:
        return vi_enc(len(data)) + data
    if len(data) < comp:
        inner = vi_enc(0) + data
        return vi_enc(len(inner)) + inner
    c = zlib.compress(data)
    inner = vi_enc(len(data)) + c
    return vi_enc(len(inner)) + inner

def mc_str_enc(s):
    b = s.encode("utf-8")
    return vi_enc(len(b)) + b

def mc_str_dec(data, pos=0):
    n, pos = vi_dec(data, pos)
    return data[pos:pos+n].decode("utf-8", errors="replace"), pos+n

def pos_enc(x, y, z):
    v = ((x & 0x3FFFFFF) << 38) | ((y & 0xFFF) << 26) | (z & 0x3FFFFFF)
    return struct.pack(">q", v)

def pos_dec(data, pos=0):
    v = struct.unpack_from(">q", data, pos)[0]
    x = v >> 38; y = (v >> 26) & 0xFFF; z = v & 0x3FFFFFF
    if x >= (1 << 25): x -= (1 << 26)
    if z >= (1 << 25): z -= (1 << 26)
    return x, y, z, pos + 8

def pkt_kick(reason, comp=-1):
    payload = mc_str_enc(json.dumps({"text": reason, "color": "yellow"}))
    return pkt_make(0x00, payload, comp)

PID_JOIN_GAME     = 0x01
PID_CHAT_SC       = 0x02
PID_UPDATE_HEALTH = 0x06
PID_POS_LOOK_SC   = 0x08
PID_SPAWN_PLAYER  = 0x0C
PID_DESTROY_ENT   = 0x13
PID_ENT_TELEPORT  = 0x18
PID_ENT_STATUS    = 0x1A
PID_MULTI_BLK     = 0x22
PID_BLOCK_CHG     = 0x23
PID_LOGIN_OK      = 0x02
PID_SET_COMP      = 0x03
PID_USE_ENTITY    = 0x02
PID_PLAYER_POS    = 0x04
PID_PLAYER_LOOK   = 0x05
PID_PLAYER_PL     = 0x06


# ══════════════════════════════════════════════════════════
#  PAYLAŞIMLI DÜNYA STATE
# ══════════════════════════════════════════════════════════

class WorldState:
    def __init__(self):
        self._lock  = threading.Lock()
        self.blocks = {}
        self._load()

    def record(self, x, y, z, bid):
        key = f"{x},{y},{z}"
        with self._lock:
            if bid == 0: self.blocks.pop(key, None)
            else:        self.blocks[key] = bid
        self._save()

    def record_multi(self, cx, cz, recs):
        with self._lock:
            for rx, ry, rz, bid in recs:
                key = f"{cx*16+rx},{ry},{cz*16+rz}"
                if bid == 0: self.blocks.pop(key, None)
                else:        self.blocks[key] = bid
        self._save()

    def replay_pkts(self, comp=-1):
        pkts = []
        with self._lock:
            items = list(self.blocks.items())
        for key, bid in items:
            try:
                x, y, z = map(int, key.split(","))
                pkts.append(pkt_make(PID_BLOCK_CHG, pos_enc(x,y,z)+vi_enc(bid), comp))
            except Exception: pass
        return pkts

    def _save(self):
        try:
            pathlib.Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(STATE_FILE).write_text(json.dumps({"blocks": self.blocks}))
        except Exception: pass

    def _load(self):
        try:
            self.blocks = json.loads(
                pathlib.Path(STATE_FILE).read_text()).get("blocks", {})
            print(f"[STATE] {len(self.blocks)} blok yuklendi.")
        except Exception: self.blocks = {}

world_state = WorldState()


# ══════════════════════════════════════════════════════════
#  CROSS-SERVER OYUNCU KAYITCISI
# ══════════════════════════════════════════════════════════

class PlayerInfo:
    __slots__ = ["username","uuid_str","x","y","z","yaw","pitch",
                 "health","real_eid","virtual_eid","conn"]
    def __init__(self, username, virtual_eid):
        self.username    = username
        self.uuid_str    = str(_uuid_mod.uuid3(
            _uuid_mod.UUID("00000000-0000-0000-0000-000000000000"),
            f"OfflinePlayer:{username}"))
        self.x = self.y = self.z = 0.0
        self.yaw = self.pitch    = 0.0
        self.health              = 20.0
        self.real_eid            = 0
        self.virtual_eid         = virtual_eid
        self.conn                = None

class CrossServerState:
    def __init__(self):
        self._lock    = threading.Lock()
        self._players = {}
        self._veid    = {}
        self._counter = 100_000

    def register(self, username, conn):
        with self._lock:
            if username in self._players:
                info = self._players[username]
            else:
                info = PlayerInfo(username, self._counter)
                self._counter += 1
                self._players[username] = info
                self._veid[info.virtual_eid] = username
            info.conn = conn
        return info

    def unregister(self, username):
        with self._lock:
            info = self._players.pop(username, None)
            if info: self._veid.pop(info.virtual_eid, None)
        return info

    def by_veid(self, veid):
        with self._lock:
            name = self._veid.get(veid)
            return self._players.get(name) if name else None

    def update_pos(self, username, x=None, y=None, z=None, yaw=None, pitch=None):
        with self._lock:
            info = self._players.get(username)
            if not info: return
            if x     is not None: info.x     = x
            if y     is not None: info.y     = y
            if z     is not None: info.z     = z
            if yaw   is not None: info.yaw   = yaw
            if pitch is not None: info.pitch = pitch

cs_state = CrossServerState()


def _ang(deg): return struct.pack("B", int(deg / 360.0 * 256) & 0xFF)
def _fp(coord): return struct.pack(">i", int(coord * 32))

def pkt_spawn_player(info, comp=-1):
    payload = (vi_enc(info.virtual_eid) + mc_str_enc(info.uuid_str) + mc_str_enc(info.username) +
        vi_enc(0) + _fp(info.x) + _fp(info.y) + _fp(info.z) + _ang(info.yaw) + _ang(info.pitch) +
        struct.pack(">h", 0) + bytes([0x7F]))
    return pkt_make(PID_SPAWN_PLAYER, payload, comp)

def pkt_entity_teleport(info, comp=-1):
    payload = (vi_enc(info.virtual_eid) + _fp(info.x) + _fp(info.y) + _fp(info.z) +
        _ang(info.yaw) + _ang(info.pitch) + bytes([1]))
    return pkt_make(PID_ENT_TELEPORT, payload, comp)

def pkt_destroy_entity(veid, comp=-1):
    return pkt_make(PID_DESTROY_ENT, vi_enc(1) + vi_enc(veid), comp)

def pkt_entity_hurt(eid, comp=-1):
    return pkt_make(PID_ENT_STATUS, struct.pack(">i", eid) + bytes([2]), comp)

def pkt_update_health(hp, comp=-1):
    return pkt_make(PID_UPDATE_HEALTH, struct.pack(">f", max(0.0, hp)) + vi_enc(20) + struct.pack(">f", 5.0), comp)

def pkt_chat_msg(text, color="yellow", comp=-1):
    return pkt_make(PID_CHAT_SC, mc_str_enc(json.dumps({"text": text, "color": color})) + bytes([0]), comp)


# ══════════════════════════════════════════════════════════
#  AKTIF BAGLANTILAR + BROADCAST
# ══════════════════════════════════════════════════════════

_active_lock = asyncio.Lock()
_active = []

def _cross_peers(conn):
    try: snap = list(_active)
    except Exception: return []
    return [c for c in snap if c is not conn and c.play_state and c.cs_info is not None and 
           (c.backend_host != conn.backend_host or c.backend_port != conn.backend_port)]

async def bcast_spawn(new_conn):
    info = new_conn.cs_info
    if not info: return
    peers = _cross_peers(new_conn)
    for c in peers:
        if c.cs_info:
            try: new_conn.client_w.write(pkt_spawn_player(c.cs_info, new_conn.comp))
            except Exception: pass
    if peers:
        try: await new_conn.client_w.drain()
        except Exception: pass
    for c in peers:
        try:
            c.client_w.write(pkt_spawn_player(info, c.comp))
            await c.client_w.drain()
        except Exception: pass

async def bcast_move(mover):
    info = mover.cs_info
    if not info: return
    peers = _cross_peers(mover)
    for c in peers:
        try:
            c.client_w.write(pkt_entity_teleport(info, c.comp))
            await c.client_w.drain()
        except Exception: pass

async def bcast_despawn(leaver):
    info = leaver.cs_info
    if not info: return
    peers = _cross_peers(leaver)
    for c in peers:
        try:
            c.client_w.write(pkt_destroy_entity(info.virtual_eid, c.comp))
            await c.client_w.drain()
        except Exception: pass

async def bcast_chat(sender, msg):
    peers = _cross_peers(sender)
    for c in peers:
        try:
            c.client_w.write(pkt_chat_msg(msg, "white", c.comp))
            await c.client_w.drain()
        except Exception: pass


# ══════════════════════════════════════════════════════════
#  BACKEND YONETIMI + GERCEK PING (HEALTH CHECK)
# ══════════════════════════════════════════════════════════

def load_backends():
    try: return json.loads(pathlib.Path(BACKENDS_FILE).read_text())
    except Exception: return []

def save_backends(b):
    pathlib.Path(BACKENDS_FILE).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(BACKENDS_FILE).write_text(json.dumps(b, indent=2))

async def check_backend_alive(host, port):
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2.0)
        hs = vi_enc(47) + mc_str_enc("127.0.0.1") + struct.pack(">H", int(port)) + vi_enc(1)
        writer.write(pkt_make(0x00, hs, -1))
        writer.write(pkt_make(0x00, b"", -1))
        await writer.drain()
        data = await asyncio.wait_for(reader.read(10), timeout=2.0)
        writer.close()
        return len(data) > 0
    except Exception:
        return False


# ══════════════════════════════════════════════════════════
#  OYUNCU BAGLANTISI  (MITM Proxy)
# ══════════════════════════════════════════════════════════

class PlayerConn:
    def __init__(self, cr, cw):
        self.client_r      = cr
        self.client_w      = cw
        self.server_r      = None
        self.server_w      = None
        self.state         = "handshake"
        self.play_state    = False
        self.comp          = -1
        self.username      = "?"
        self.backend_host  = ""
        self.backend_port  = 0
        self.peer          = cw.get_extra_info("peername", ("?", 0))[0]
        self._replay_sent  = False
        self._spawned      = False
        self.cs_info       = None

    @property
    def s2c_comp(self): return self.comp
    @property
    def c2s_comp(self): return self.comp

    async def connect_backend(self, b):
        self.backend_host = b["host"]
        self.backend_port = b["port"]
        self.server_r, self.server_w = await asyncio.open_connection(
            b["host"], b["port"], limit=2**20)

    async def pipe_s2c(self):
        try:
            while True:
                pid, payload, raw = await pkt_read(self.server_r, self.comp)
                if self.state == "login":
                    if pid == PID_SET_COMP: self.comp, _ = vi_dec(payload)
                    elif pid == PID_LOGIN_OK: self.state = "play"
                    self.client_w.write(raw)
                    await self.client_w.drain()
                    continue

                if pid == PID_JOIN_GAME and self.cs_info and len(payload) >= 4:
                    try: self.cs_info.real_eid = struct.unpack_from(">i", payload, 0)[0]
                    except Exception: pass
                elif pid == PID_BLOCK_CHG and len(payload) >= 9:
                    try:
                        x, y, z, p2 = pos_dec(payload)
                        bid, _ = vi_dec(payload, p2)
                        world_state.record(x, y, z, bid)
                    except Exception: pass
                elif pid == PID_MULTI_BLK and len(payload) >= 8:
                    try:
                        cx = struct.unpack_from(">i", payload, 0)[0]
                        cz = struct.unpack_from(">i", payload, 4)[0]
                        count, p2 = vi_dec(payload, 8)
                        recs = []
                        for _ in range(count):
                            hp = payload[p2]; p2 += 1; ry = payload[p2]; p2 += 1
                            bid, p2 = vi_dec(payload, p2)
                            recs.append(((hp >> 4) & 0xF, ry, hp & 0xF, bid))
                        world_state.record_multi(cx, cz, recs)
                    except Exception: pass
                elif pid == PID_POS_LOOK_SC and not self._replay_sent:
                    self._replay_sent = True
                    self.client_w.write(raw)
                    await self.client_w.drain()
                    for p in world_state.replay_pkts(self.comp):
                        self.client_w.write(p)
                    await self.client_w.drain()
                    if not self._spawned and self.cs_info:
                        self._spawned = True
                        self.play_state = True
                        asyncio.ensure_future(bcast_spawn(self))
                    continue

                self.client_w.write(raw)
                await self.client_w.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError): pass
        except Exception as e: print(f"[S->C] {self.username}: {e}")
        finally: await self._cleanup()

    async def pipe_c2s(self):
        try:
            while True:
                pid, payload, raw = await pkt_read(self.client_r, self.comp)
                if self.state == "handshake":
                    if pid == 0x00:
                        try:
                            p = 0
                            _, p = vi_dec(payload, p); _, p = mc_str_dec(payload, p)
                            p += 2; ns, _ = vi_dec(payload, p)
                            if ns == 2: self.state = "login"
                        except Exception: pass
                    self.server_w.write(raw); await self.server_w.drain()
                    continue

                if self.state == "login":
                    if pid == 0x00:
                        try:
                            self.username, _ = mc_str_dec(payload)
                            self.cs_info = cs_state.register(self.username, self)
                            print(f"[JOIN] {self.username}")
                        except Exception: pass
                    self.server_w.write(raw); await self.server_w.drain()
                    continue

                if pid == PID_PLAYER_POS and len(payload) >= 25:
                    try:
                        x, y, z = struct.unpack_from(">ddd", payload, 0)
                        cs_state.update_pos(self.username, x=x, y=y, z=z)
                        if self._spawned: asyncio.ensure_future(bcast_move(self))
                    except Exception: pass
                elif pid == PID_PLAYER_LOOK and len(payload) >= 9:
                    try:
                        yaw, pitch = struct.unpack_from(">ff", payload, 0)
                        cs_state.update_pos(self.username, yaw=yaw, pitch=pitch)
                    except Exception: pass
                elif pid == PID_PLAYER_PL and len(payload) >= 33:
                    try:
                        x, y, z = struct.unpack_from(">ddd", payload, 0)
                        yaw, pitch = struct.unpack_from(">ff", payload, 24)
                        cs_state.update_pos(self.username, x=x, y=y, z=z, yaw=yaw, pitch=pitch)
                        if self._spawned: asyncio.ensure_future(bcast_move(self))
                    except Exception: pass
                elif pid == PID_USE_ENTITY:
                    try:
                        target_veid, p2 = vi_dec(payload, 0)
                        action, _       = vi_dec(payload, p2)
                        if action == 1:
                            target = cs_state.by_veid(target_veid)
                            if target and target.conn and target.conn is not self:
                                asyncio.ensure_future(self._cross_attack(target))
                                continue
                    except Exception: pass
                elif pid == 0x01 and self.play_state:
                    try:
                        msg, _ = mc_str_dec(payload)
                        asyncio.ensure_future(bcast_chat(self, f"[{self.username}] {msg}"))
                    except Exception: pass

                self.server_w.write(raw); await self.server_w.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError): pass
        except Exception as e: print(f"[C->S] {self.username}: {e}")
        finally: await self._cleanup()

    async def _cross_attack(self, target):
        DMG = 2.0
        target.health = max(0.0, target.health - DMG)
        tc = target.conn
        if not tc or not tc.cs_info: return
        try:
            tc.client_w.write(pkt_entity_hurt(tc.cs_info.real_eid, tc.comp))
            tc.client_w.write(pkt_update_health(target.health, tc.comp))
            await tc.client_w.drain()
        except Exception: pass
        try:
            self.client_w.write(pkt_entity_hurt(target.virtual_eid, self.comp))
            await self.client_w.drain()
        except Exception: pass
        print(f"[PVP] {self.username} -> {target.username} ({target.health:.0f}/20 HP)")
        if target.health <= 0.0:
            target.health = 20.0
            msg = f"[{target.username} oldu! Olduran: {self.username}]"
            try:
                tc.client_w.write(pkt_update_health(20.0, tc.comp))
                tc.client_w.write(pkt_chat_msg(msg, "red", tc.comp))
                await tc.client_w.drain()
            except Exception: pass
            try:
                self.client_w.write(pkt_chat_msg(msg, "red", self.comp))
                await self.client_w.drain()
            except Exception: pass

    async def _cleanup(self):
        if self.cs_info:
            asyncio.ensure_future(bcast_despawn(self))
            cs_state.unregister(self.username)
            self.cs_info = None
        async with _active_lock:
            if self in _active:
                _active.remove(self)
                if self.username != "?":
                    print(f"[QUIT] {self.username} ({len(_active)} aktif)")
        for w in (self.client_w, self.server_w):
            if w:
                try: w.close()
                except Exception: pass

    async def run(self):
        backends = load_backends()
        if not backends:
            self.client_w.close(); return

        counts = {}
        for c in list(_active):
            if c.username != "?":
                k = f"{c.backend_host}:{c.backend_port}"
                counts[k] = counts.get(k, 0) + 1
                
        sorted_backends = sorted(backends, key=lambda x: counts.get(f"{x['host']}:{x['port']}", 0))

        connected = False
        for b in sorted_backends:
            lbl = b.get("label", "")
            if "onrender.com" in lbl:
                async def wakeup(u):
                    try:
                        reader, writer = await asyncio.wait_for(asyncio.open_connection(u, 443, ssl=True), 2.0)
                        writer.write(f"GET /api/status HTTP/1.1\r\nHost: {u}\r\nConnection: close\r\n\r\n".encode())
                        await writer.drain()
                        writer.close()
                    except: pass
                asyncio.ensure_future(wakeup(lbl))

            if await check_backend_alive(b["host"], b["port"]):
                try:
                    await self.connect_backend(b)
                    connected = True
                    break
                except Exception: pass
            else:
                print(f"[WARN] Uykuda olan/Ölü sunucu atlandi: {lbl} ({b['host']}:{b['port']})")

        if not connected:
            print("[ERR] Gecerli/Uyanik GameServer bulunamadi!")
            if self.state == "login":
                msg = "Sunucular uykudaydi ve su an UYANDIRILIYOR!\n\nLutfen 40 saniye sonra TEKRAR GIRIS YAPIN."
                try:
                    self.client_w.write(pkt_kick(msg, self.comp))
                    await self.client_w.drain()
                except: pass
            self.client_w.close()
            return

        async with _active_lock:
            _active.append(self)
        
        await asyncio.gather(self.pipe_s2c(), self.pipe_c2s())


async def handle_player(cr, cw):
    await PlayerConn(cr, cw).run()


# ══════════════════════════════════════════════════════════
#  HTTP DURUM SAYFASI VE API (VERITABANI BURADA)
# ══════════════════════════════════════════════════════════

HTML = """\
<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Minecraft Engine</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@400;600;700&display=swap" rel="stylesheet">
  <style>
    :root{{--bg:#0d0f1a;--panel:#111827;--border:#1e3a5f;--accent:#00ffc8;--accent2:#0099ff;--warn:#f8b400;--err:#ff4f4f;--dim:#4a5568;--text:#cbd5e0;}}
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:var(--bg);color:var(--text);font-family:'Share Tech Mono',monospace;min-height:100vh;padding:20px;
          background-image:radial-gradient(ellipse 80% 60% at 50% -10%,#0a2a4a55,transparent),
          repeating-linear-gradient(0deg,transparent,transparent 39px,#1e3a5f18 39px,#1e3a5f18 40px),
          repeating-linear-gradient(90deg,transparent,transparent 39px,#1e3a5f18 39px,#1e3a5f18 40px);}}
    .wrap{{max-width:920px;margin:0 auto;display:flex;flex-direction:column;gap:14px}}
    .header{{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border);padding-bottom:12px}}
    .logo{{font-family:'Rajdhani',sans-serif;font-size:1.7rem;font-weight:700;color:var(--accent);letter-spacing:.08em;text-shadow:0 0 20px #00ffc855}}
    .logo span{{color:var(--text);font-weight:400}}
    .live-dot{{width:8px;height:8px;border-radius:50%;background:var(--accent);display:inline-block;margin-right:6px;box-shadow:0 0 6px var(--accent);animation:blink 1.4s ease-in-out infinite}}
    @keyframes blink{{0%,100%{{opacity:1;transform:scale(1)}}50%{{opacity:.4;transform:scale(.7)}}}}
    .addr-box{{background:linear-gradient(135deg,#0a2a4a,#0a1a35);border:1px solid var(--accent);border-radius:8px;padding:12px 18px;display:flex;align-items:center;gap:14px;box-shadow:0 0 24px #00ffc81a}}
    .addr-lbl{{font-size:.68rem;color:var(--dim);white-space:nowrap}}
    .addr-val{{font-size:1.25rem;color:var(--accent);flex:1;word-break:break-all;text-shadow:0 0 12px #00ffc844}}
    .copy-btn{{background:#00ffc815;border:1px solid var(--accent);color:var(--accent);border-radius:6px;padding:6px 14px;font-size:.73rem;cursor:pointer;font-family:'Share Tech Mono',monospace;transition:all .15s;white-space:nowrap}}
    .copy-btn:hover{{background:var(--accent);color:#000}}
    .stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}
    .stat{{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px 16px;position:relative;overflow:hidden}}
    .stat::before{{content:'';position:absolute;inset:0;background:linear-gradient(135deg,#00ffc808,transparent);pointer-events:none}}
    .stat-val{{font-size:2rem;color:var(--accent);font-family:'Rajdhani',sans-serif;font-weight:700;line-height:1}}
    .stat-lbl{{font-size:.68rem;color:var(--dim);margin-top:4px}}
    .panel{{background:var(--panel);border:1px solid var(--border);border-radius:8px;overflow:hidden}}
    .panel-hdr{{display:flex;align-items:center;justify-content:space-between;padding:9px 16px;border-bottom:1px solid var(--border);background:#0a1428}}
    .panel-hdr-title{{font-size:.72rem;color:var(--accent);letter-spacing:.1em;text-transform:uppercase}}
    table{{width:100%;border-collapse:collapse}}
    th{{color:var(--dim);font-size:.68rem;padding:7px 16px;text-align:left;text-transform:uppercase;letter-spacing:.08em;background:#0a1428;border-bottom:1px solid var(--border)}}
    td{{padding:8px 16px;font-size:.82rem;border-bottom:1px solid #1e3a5f33}}
    tr:last-child td{{border-bottom:none}}
    tr:hover td{{background:#1e3a5f22}}
    .sdot{{width:7px;height:7px;border-radius:50%;background:var(--accent);display:inline-block;margin-right:6px;box-shadow:0 0 5px var(--accent);animation:blink 1.4s infinite}}
    .son{{color:var(--accent)}}
    .badges{{display:flex;flex-wrap:wrap;gap:6px;padding:12px 16px;border-top:1px solid var(--border)}}
    .badge{{background:#00ffc808;border:1px solid #00ffc830;color:var(--accent);font-size:.63rem;padding:3px 10px;border-radius:20px;letter-spacing:.05em}}
    .con-toolbar{{display:flex;align-items:center;justify-content:space-between;padding:7px 14px;background:#070d1a;border-bottom:1px solid var(--border)}}
    .win-btns{{display:flex;gap:6px}}
    .wb{{width:10px;height:10px;border-radius:50%}}
    .wb-r{{background:#ff5f56}}.wb-y{{background:#ffbd2e}}.wb-g{{background:#27c93f}}
    .con-title{{font-size:.68rem;color:var(--dim);letter-spacing:.1em;margin-left:8px}}
    .con-right{{display:flex;align-items:center;gap:6px;flex-wrap:wrap}}
    .flt{{background:transparent;border:1px solid var(--border);color:var(--dim);font-family:'Share Tech Mono',monospace;font-size:.63rem;border-radius:4px;padding:3px 8px;cursor:pointer;transition:all .15s}}
    .flt.on{{border-color:var(--accent);color:var(--accent)}}
    .flt:hover{{border-color:var(--accent2);color:var(--accent2)}}
    .clr{{background:transparent;border:1px solid var(--border);color:var(--dim);font-family:'Share Tech Mono',monospace;font-size:.63rem;border-radius:4px;padding:3px 8px;cursor:pointer;transition:all .15s}}
    .clr:hover{{border-color:var(--err);color:var(--err)}}
    .sse-ind{{display:flex;align-items:center;gap:4px;font-size:.62rem;color:var(--dim)}}
    .sse-d{{width:6px;height:6px;border-radius:50%;background:var(--dim)}}
    .sse-d.live{{background:var(--accent);box-shadow:0 0 4px var(--accent)}}
    #con{{background:#070d1a;height:340px;overflow-y:auto;padding:10px 14px;font-size:.76rem;line-height:1.75;scroll-behavior:smooth}}
    #con::-webkit-scrollbar{{width:4px}}
    #con::-webkit-scrollbar-track{{background:#0a0f1e}}
    #con::-webkit-scrollbar-thumb{{background:#1e3a5f;border-radius:3px}}
    .ll{{display:flex;gap:8px}}
    .lt{{color:var(--dim);flex-shrink:0;font-size:.66rem;padding-top:1px}}
    .lm{{word-break:break-all;flex:1}}
    @media(max-width:600px){{.stats{{grid-template-columns:repeat(2,1fr)}}.addr-val{{font-size:1rem}}.flt{{font-size:.55rem;padding:2px 5px}}}}
  </style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="logo">⛏ WC<span>-ENGINE</span></div>
    <div style="font-size:.72rem;color:var(--dim)">
      <span class="live-dot"></span>{mode_label} &bull; <span id="hdr-p">{player_count}</span> oyuncu aktif
    </div>
  </div>
  {addr_block}
  <div class="stats">
    <div class="stat"><div class="stat-val" id="s-p">{player_count}</div><div class="stat-lbl">Toplam Oyuncu</div></div>
    <div class="stat"><div class="stat-val" id="s-b">{block_count}</div><div class="stat-lbl">Blok Değişikliği</div></div>
    <div class="stat"><div class="stat-val" id="s-s">{server_count}</div><div class="stat-lbl">Game Server</div></div>
  </div>
  <div class="panel">
    <div class="panel-hdr"><span class="panel-hdr-title">Game Servers</span><span style="font-size:.68rem;color:var(--dim)">otomatik yenilenir</span></div>
    <table id="srv-tbl"><tr><th>Sunucu</th><th>Oyuncu</th><th>Durum</th></tr>{rows}</table>
  </div>
  <div class="panel">
    <div class="con-toolbar">
      <div style="display:flex;align-items:center">
        <div class="win-btns"><div class="wb wb-r"></div><div class="wb wb-y"></div><div class="wb wb-g"></div></div>
        <span class="con-title">CANLI KONSOL</span>
      </div>
      <div class="con-right">
        <button class="flt on" data-f="">TÜMÜ</button>
        <button class="flt" data-f="ERR">HATA</button>
        <button class="flt" data-f="WARN">UYARI</button>
        <button class="flt" data-f="CONN,JOIN,QUIT,SYNC">OYUNCU</button>
        <button class="flt" data-f="BORE,REG">TUNNEL</button>
        <button class="flt" data-f="MC">MC</button>
        <button class="clr" id="clrBtn">TEMİZLE</button>
        <div class="sse-ind"><div class="sse-d" id="sseDot"></div><span id="sseLbl">bağlanıyor</span></div>
      </div>
    </div>
    <div id="con"><div style="color:#2a4a6a;font-size:.73rem;padding:4px">— konsol yükleniyor —</div></div>
  </div>
</div>
<script>
(function(){{
  const con=document.getElementById('con'),dot=document.getElementById('sseDot'),lbl=document.getElementById('sseLbl');
  let autoScroll=true,activeFilter='',allLines=[];
  con.addEventListener('scroll',()=>{{autoScroll=con.scrollTop+con.clientHeight>=con.scrollHeight-30;}});
  function esc(s){{return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}
  function renderLine(e){{
    const d=document.createElement('div');d.className='ll';d.dataset.msg=e.msg;
    d.innerHTML='<span class="lt">'+e.ts+'</span><span class="lm" style="color:'+e.color+'">'+esc(e.msg)+'</span>';
    return d;
  }}
  function applyFilter(){{
    const f=activeFilter;
    con.querySelectorAll('.ll').forEach(el=>{{const m=el.dataset.msg||'';el.style.display=(!f||f.split(',').some(k=>m.includes('['+k+']')))?'':'none';}});
  }}
  document.querySelectorAll('.flt').forEach(btn=>{{
    btn.addEventListener('click',()=>{{document.querySelectorAll('.flt').forEach(b=>b.classList.remove('on'));btn.classList.add('on');activeFilter=btn.dataset.f;applyFilter();}});
  }});
  document.getElementById('clrBtn').addEventListener('click',()=>{{con.innerHTML='';allLines=[];}});
  function addLine(e){{
    allLines.push(e);
    if(allLines.length>300){{allLines.shift();const f=con.querySelector('.ll');if(f)f.remove();}}
    const el=renderLine(e);
    const f=activeFilter;
    if(f&&!f.split(',').some(k=>e.msg.includes('['+k+']')))el.style.display='none';
    con.appendChild(el);if(autoScroll)con.scrollTop=con.scrollHeight;
  }}
  fetch('/api/logs/history').then(r=>r.json()).then(arr=>{{con.innerHTML='';arr.forEach(e=>addLine(e));}}).catch(()=>{{}});
  function connectSSE(){{
    dot.className='sse-d';lbl.textContent='bağlanıyor...';
    const es=new EventSource('/api/logs/stream');
    es.onopen=()=>{{dot.className='sse-d live';lbl.textContent='canlı';}};
    es.onmessage=e=>{{try{{addLine(JSON.parse(e.data));}}catch(x){{}}}};
    es.onerror=()=>{{dot.className='sse-d';lbl.textContent='yeniden bağlanıyor...';es.close();setTimeout(connectSSE,3000);}};
  }}
  connectSSE();
  function refreshStats(){{
    fetch('/api/status').then(r=>r.json()).then(d=>{{
      document.getElementById('s-p').textContent=d.players;
      document.getElementById('s-b').textContent=d.blocks;
      document.getElementById('s-s').textContent=d.servers;
      document.getElementById('hdr-p').textContent=d.players;
      const t=document.getElementById('srv-tbl');
      if(t)t.innerHTML='<tr><th>Sunucu</th><th>Oyuncu</th><th>Durum</th></tr>'+d.table_rows;
      if(d.addr){{const av=document.querySelector('.addr-val');if(av)av.textContent=d.addr;}}
    }}).catch(()=>{{}});
  }}
  setInterval(refreshStats,5000);
}})();
</script>
</body></html>"""


def _build_rows():
    backends = load_backends()
    counts = {}
    for c in list(_active):
        if c.username != "?":
            k = f"{c.backend_host}:{c.backend_port}"
            counts[k] = counts.get(k, 0) + 1
    rows = ""
    for b in backends:
        k     = f"{b['host']}:{b['port']}"
        n     = counts.get(k, 0)
        label = b.get("label", k)
        rows += (f'<tr><td>{label}</td><td>{n} oyuncu</td>'
                 f'<td><span class="sdot"></span><span class="son">Aktif</span></td></tr>')
    if not rows:
        rows = ('<tr><td colspan="3" style="color:#4a5568;text-align:center;padding:18px">'
                'Game server bekleniyor...</td></tr>')
    return rows, backends

def _get_bore():
    try:    return pathlib.Path(BORE_FILE).read_text().strip()
    except: return None

def _build_html():
    bore = _get_bore()
    if bore:
        addr_block = (f'<div class="addr-box"><div><div class="addr-lbl">MİNECRAFT ADRESİ — Sunucu Ekle → bu adresi gir</div>'
                      f'<div class="addr-val">{bore}</div></div>'
                      f'<button class="copy-btn" onclick="navigator.clipboard.writeText(\'{bore}\');'
                      f'this.textContent=\'✓ Kopyalandı\';setTimeout(()=>this.textContent=\'Kopyala\',1500)">Kopyala</button></div>')
    else:
        addr_block = ('<div class="addr-box" style="border-color:var(--warn)">'
                      '<div class="addr-val" style="color:var(--warn);font-size:.9rem">⏳ Tunnel başlatılıyor...</div></div>')

    rows, backends = _build_rows()
    real_player_count = sum(1 for c in list(_active) if c.username != "?")
    return HTML.format(addr_block=addr_block, player_count=real_player_count, block_count=len(world_state.blocks),
                       server_count=len(backends), rows=rows, mode_label=MODE.upper())

class _H(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        try:
            if self.path.startswith("/api/player?name="):
                name = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('name', [''])[0]
                name = "".join(c for c in name if c.isalnum() or c in "-_")
                filepath = f"{DATA_DIR}/players/{name}.json"
                if os.path.exists(filepath):
                    body = pathlib.Path(filepath).read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers(); self.wfile.write(body)
                else:
                    self.send_response(404); self.end_headers()
                return

            if self.path == "/api/logs/stream":
                import queue as _q
                q = _q.Queue(maxsize=200)
                with _SSE_LOCK: _SSE_CLIENTS.append(q)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                try:
                    while True:
                        try:
                            payload = q.get(timeout=25)
                            self.wfile.write(payload.encode()); self.wfile.flush()
                        except _q.Empty:
                            self.wfile.write(b": ping\n\n"); self.wfile.flush()
                except Exception: pass
                finally:
                    with _SSE_LOCK:
                        if q in _SSE_CLIENTS: _SSE_CLIENTS.remove(q)
                return

            if self.path == "/api/logs/history":
                with _LOG_LOCK: data = list(_LOG_BUF)
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
                return

            if self.path == "/api/status":
                rows, backends = _build_rows()
                bore = _get_bore()
                real_player_count = sum(1 for c in list(_active) if c.username != "?")
                payload = {"players": real_player_count, "blocks": len(world_state.blocks),
                           "servers": len(backends), "mode": MODE.upper(), "addr": bore or "", "table_rows": rows}
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
                return

            if MODE != "proxy":
                payload = {"status": "active", "mode": MODE}
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
                return

            body = _build_html().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError): pass
        except Exception: pass

    def do_POST(self):
        try:
            if self.path.startswith("/api/player?name="):
                name = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('name', [''])[0]
                name = "".join(c for c in name if c.isalnum() or c in "-_")
                filepath = f"{DATA_DIR}/players/{name}.json"
                pathlib.Path(f"{DATA_DIR}/players").mkdir(parents=True, exist_ok=True)
                
                length = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(length)
                if data:
                    pathlib.Path(filepath).write_bytes(data)
                    self._r(200, "ok")
                else: self._r(400, "empty")
                return

            length = int(self.headers.get("Content-Length", 0))
            try: data = json.loads(self.rfile.read(length))
            except Exception: self._r(400, "bad json"); return

            if self.path == "/api/register":
                host = data.get("host"); port = data.get("port")
                label = data.get("label", f"{host}:{port}")
                if not host or not port: self._r(400, "missing host/port"); return
                backends = load_backends(); found = False
                for b in backends:
                    if b.get("label") == label: b["host"] = host; b["port"] = int(port); found = True; break
                if not found: backends.append({"host": host, "port": int(port), "label": label})
                save_backends(backends)
                print(f"[REG] {label} ({host}:{port})")
                self._r(200, "ok")

            elif self.path == "/api/unregister":
                label = data.get("label", "")
                if not label: self._r(400, "missing label"); return
                backends = load_backends()
                backends = [b for b in backends if b.get("label") != label]
                save_backends(backends)
                print(f"[UNREG] {label}")
                self._r(200, "ok")
            else: self._r(404, "not found")
        except (BrokenPipeError, ConnectionResetError): pass

    def _r(self, code, msg):
        try:
            b = msg.encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError): pass

    def handle_error(self, request, client_address): pass
    def log_message(self, *_): pass


def run_http():
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), _H)
    print(f"[HTTP] Port {HTTP_PORT}")
    srv.serve_forever()


def _health_check_loop():
    import socket
    while True:
        time.sleep(60)
        try:
            backends = load_backends()
            alive = []
            for b in backends:
                try:
                    s = socket.create_connection((b["host"], b["port"]), timeout=5)
                    s.close()
                    alive.append(b)
                except Exception:
                    print(f"[HEALTH] Olu backend kaldirildi: {b.get('label','?')} ({b['host']}:{b['port']})")
            if len(alive) != len(backends): save_backends(alive)
        except Exception as e: print(f"[HEALTH] Hata: {e}")


# ══════════════════════════════════════════════════════════
#  BORE TUNNEL
# ══════════════════════════════════════════════════════════

def _strip_ansi(text):
    import re
    return re.sub(r'\x1b\[[0-9;]*[mK]|\x1b\[\d*[A-Za-z]|\x1b\(\w', '', text)

def _wait_for_port(port, timeout=60):
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close(); return True
        except Exception: time.sleep(1)
    return False

def run_bore(port=MC_PORT):
    import re
    if MODE == "gameserver":
        print(f"[BORE] Cuberite port {port} bekleniyor...")
        if _wait_for_port(port, timeout=120): print(f"[BORE] Port {port} hazir, tunnel baslatiliyor...")
        else: print(f"[BORE] UYARI: Port {port} acilmadi, deneniyor...")
    while True:
        try:
            pathlib.Path(BORE_FILE).unlink(missing_ok=True)
            proc = subprocess.Popen(["bore", "local", str(port), "--to", "bore.pub"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                line = _strip_ansi(line.rstrip())
                if not line: continue
                print(f"[BORE] {line}")
                m = re.search(r"bore\.pub:(\d+)", line)
                if m:
                    addr = f"bore.pub:{m.group(1)}"
                    pathlib.Path(BORE_FILE).write_text(addr)
                    if MODE == "gameserver": _register_with_proxy(addr)
            proc.wait()
            if MODE == "gameserver": _unregister_from_proxy()
        except FileNotFoundError: print("[BORE] bore bulunamadi...")
        except Exception as e: print(f"[BORE] hata: {e}")
        time.sleep(10)

def _register_with_proxy(bore_addr, retries=5):
    proxy_url = os.environ.get("PROXY_URL", "")
    if not proxy_url: return
    label = os.environ.get("SERVER_LABEL", "GameServer")
    host, port_str = bore_addr.split(":")
    body = json.dumps({"host": host, "port": int(port_str), "label": label}).encode()
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(f"{proxy_url}/api/register", data=body, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
            print(f"[REG] Proxy kayit: {proxy_url} ({label})")
            return
        except Exception as e:
            print(f"[REG] Kayit hatasi (deneme {attempt}/{retries}): {e}")
            if attempt < retries: time.sleep(5 * attempt)

def _unregister_from_proxy():
    proxy_url = os.environ.get("PROXY_URL", "")
    if not proxy_url: return
    label = os.environ.get("SERVER_LABEL", "GameServer")
    try:
        body = json.dumps({"label": label}).encode()
        req  = urllib.request.Request(f"{proxy_url}/api/unregister", data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        print(f"[UNREG] Proxy kayit silindi: {label}")
    except Exception as e: print(f"[UNREG] Silme hatasi: {e}")


# ══════════════════════════════════════════════════════════
#  CUBERITE BASLATICI VE LOG AVCISI (PYTHON <-> LUA)
# ══════════════════════════════════════════════════════════

def run_cuberite():
    write_configs()
    mc_bin = next(iter(glob.glob("/server/**/Cuberite", recursive=True)), None)
    if not mc_bin: print("[MC] HATA: Cuberite bulunamadi!"); return
    mc_dir = str(pathlib.Path(mc_bin).parent)
    
    persistent_world = "/server/world"
    target_world = f"{mc_dir}/world"
    pathlib.Path(persistent_world).mkdir(parents=True, exist_ok=True)
    flag = f"{persistent_world}/.initialized"
    if not pathlib.Path(flag).exists():
        print("[MC] Ilk baslatma: dunya ayarlaniyor...")
        pathlib.Path(flag).touch()

    if target_world != persistent_world:
        if not os.path.islink(target_world):
            import shutil
            if os.path.exists(target_world):
                if os.path.isdir(target_world): shutil.rmtree(target_world, ignore_errors=True)
                else: os.remove(target_world)
            try:
                os.symlink(persistent_world, target_world)
                print(f"[MC] Kalici disk baglandi: {target_world} -> {persistent_world}")
            except Exception as e: print(f"[MC] Disk baglama hatasi: {e}")

    os.chmod(mc_bin, 0o755)

    def _pipe_output(stream, proc, prefix="[MC]"):
        proxy_url = os.environ.get("PROXY_URL", "http://127.0.0.1:8080")
        for raw in stream:
            line = raw.rstrip() if isinstance(raw, str) else raw.decode("utf-8", "replace").rstrip()
            if not line: continue
            print(f"{prefix} {line}")
            
            if "WCSYNC_JOIN:" in line:
                def _do_join(ln):
                    try:
                        parts = ln.split("WCSYNC_JOIN:")[1].strip().split(":")
                        name, uuid = parts[0], parts[1]
                        uuid_clean = uuid.replace("-", "")
                        
                        req = urllib.request.Request(f"{proxy_url}/api/player?name={name}")
                        resp = urllib.request.urlopen(req, timeout=5)
                        data = resp.read()
                        
                        paths = [
                            pathlib.Path(f"/server/world/players/{uuid}.json"),
                            pathlib.Path(f"/server/world/players/{uuid_clean}.json")
                        ]
                        for p in paths:
                            p.parent.mkdir(parents=True, exist_ok=True)
                            p.write_bytes(data)
                            
                        proc.stdin.write(f"wcreload {name}\n")
                        proc.stdin.flush()
                        print(f"[SYNC] {name} envanteri merkezden indirildi.")
                    except Exception as e:
                        if "404" not in str(e):
                            print(f"[SYNC] Hata (Join): {e}")

                threading.Thread(target=_do_join, args=(line,), daemon=True).start()

            elif "YAVER_SPAWN:" in line or "YAVER_DEAD:" in line or "YAVER_LEVELUP:" in line:
                # Yaver olaylarını loglara yansıt (ileride istatistik için genişletilebilir)
                try:
                    if "YAVER_SPAWN:" in line:
                        player = line.split("YAVER_SPAWN:")[1].strip().split(":")[0]
                        print(f"[YAVER] {player} yeni yaver cagirdi.")
                    elif "YAVER_DEAD:" in line:
                        player = line.split("YAVER_DEAD:")[1].strip()
                        print(f"[YAVER] {player}'in yaveri oldu.")
                    elif "YAVER_LEVELUP:" in line:
                        parts = line.split("YAVER_LEVELUP:")[1].strip().split(":")
                        print(f"[YAVER] {parts[0]} yaveri {parts[1] if len(parts)>1 else '?'} oldu!")
                except Exception as e:
                    print(f"[YAVER] Log parse hatasi: {e}")

            elif "WCSYNC_QUIT:" in line or "WCSYNC_SAVE:" in line:
                def _do_upload(ln):
                    try:
                        time.sleep(1.5) 
                        
                        tag = "WCSYNC_QUIT:" if "WCSYNC_QUIT:" in ln else "WCSYNC_SAVE:"
                        parts = ln.split(tag)[1].strip().split(":")
                        name, uuid = parts[0], parts[1]
                        uuid_clean = uuid.replace("-", "")
                        
                        p1 = pathlib.Path(f"/server/world/players/{uuid}.json")
                        p2 = pathlib.Path(f"/server/world/players/{uuid_clean}.json")
                        target_p = p1 if p1.exists() else (p2 if p2.exists() else None)
                        
                        if target_p:
                            data = target_p.read_bytes()
                            req = urllib.request.Request(f"{proxy_url}/api/player?name={name}", data=data, method="POST")
                            req.add_header("Content-Type", "application/json")
                            urllib.request.urlopen(req, timeout=5)
                            
                            if "QUIT" in tag:
                                print(f"[SYNC] {name} envanteri merkeze kaydedildi. ({target_p.name})")
                        else:
                            if "QUIT" in tag:
                                print(f"[WARN] Senkronizasyon Atlandı: {name} kayit dosyasi bulunamadi!")
                    except Exception as e: 
                        print(f"[SYNC] Hata (Upload): {e}")

                threading.Thread(target=_do_upload, args=(line,), daemon=True).start()

    while True:
        print(f"[MC] Cuberite baslatiliyor: {mc_bin}")
        proc = subprocess.Popen([mc_bin], cwd=mc_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=_pipe_output, args=(proc.stdout, proc, "[MC]"), daemon=True).start()
        ret = proc.wait()
        print(f"[MC] Cuberite kapandi (kod={ret}), 5sn sonra yeniden baslatiliyor...")
        time.sleep(5)


# ══════════════════════════════════════════════════════════
#  ASYNC PROXY
# ══════════════════════════════════════════════════════════

async def run_proxy_async():
    pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    if not pathlib.Path(BACKENDS_FILE).exists(): save_backends([])
    server = await asyncio.start_server(handle_player, "0.0.0.0", MC_PORT, limit=2**20)
    print(f"[PROXY] Port {MC_PORT} - Cross-server entity sync + PvP AKTIF")
    async with server: await server.serve_forever()


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════

def main():
    print(f"""
+--------------------------------------------------+
|  Minecraft Distributed World Engine              |
|  Mod : {MODE:<42}|
|  Cross-server: Entity + PvP + Chat + Blok        |
+--------------------------------------------------+""")

    if MODE == "config": write_configs(); return

    threading.Thread(target=run_http, daemon=True).start()

    if MODE == "proxy":
        threading.Thread(target=run_bore, args=(MC_PORT,), daemon=True).start()
        threading.Thread(target=_health_check_loop, daemon=True).start()
        asyncio.run(run_proxy_async())
    elif MODE == "gameserver":
        threading.Thread(target=run_bore, args=(MC_PORT,), daemon=True).start()
        run_cuberite()
    elif MODE == "all":
        threading.Thread(target=run_bore, args=(MC_PORT,), daemon=True).start()
        threading.Thread(target=run_cuberite, daemon=True).start()
        time.sleep(3)
        save_backends([{"host": "127.0.0.1", "port": MC_PORT, "label": "LocalCuberite"}])
        asyncio.run(run_proxy_async())
    elif MODE == "http": run_http()
    else: print(f"Bilinmeyen mod: {MODE}"); sys.exit(1)

if __name__ == "__main__": main()
