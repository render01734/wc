-- ══════════════════════════════════════════════════════════════════
--  PlayerSave — Cuberite Lua Plugin  v1.0
--  Cuberite'in player/stats dosyası yönetimini stabilize eder.
--
--  SORUN:
--    Cuberite, oyuncu giriş yaparken world/data/stats/NAME.json'ı
--    C++ iostream stream'iyle okur. Dosya yoksa veya sadece {} ise
--    "basic_ios::clear: iostream error" → oyuncu atılır.
--
--  ÇÖZÜM:
--    HOOK_LOGIN (player entity oluşmadan ÖNCE ateşlenir) → stats
--    dosyası geçerli formatta yoksa yaz: {"stats":{}}
--    HOOK_PLAYER_DESTROYED → çıkışta stats dosyası bozulduysa onar.
-- ══════════════════════════════════════════════════════════════════

PLUGIN = nil

-- Geçerli Cuberite stats JSON içeriği (boş, ama doğru yapı)
-- {} YAZMA — Cuberite "stats" key'ini bekliyor
local EMPTY_STATS = '{"stats":{}}'

-- Dizin varlığını garantile (Lua'da mkdir -p yok, os.execute kullan)
local function EnsureDir(path)
    os.execute('mkdir -p "' .. path .. '" 2>/dev/null')
    os.execute('chmod 777 "' .. path .. '" 2>/dev/null')
end

-- Dosyanın geçerli Cuberite stats JSON'u içerip içermediğini kontrol et
local function IsValidStatsFile(path)
    local f = io.open(path, "r")
    if not f then return false end
    local content = f:read("*all")
    f:close()
    if not content or content == "" then return false end
    -- "stats" key'i olmalı — Cuberite bunu bekliyor
    return content:find('"stats"') ~= nil
end

-- Güvenli stats dosyası yaz
local function WriteStatsFile(path, username)
    local f = io.open(path, "w")
    if f then
        f:write(EMPTY_STATS)
        f:close()
        os.execute('chmod 666 "' .. path .. '" 2>/dev/null')
        LOG("[PlayerSave] " .. username .. " stats dosyasi olusturuldu: " .. path)
        return true
    end
    LOG("[PlayerSave] UYARI: " .. path .. " yazılamadı!")
    return false
end

-- ── Giriş hook'u ─────────────────────────────────────────────────
-- HOOK_LOGIN: player entity daha oluşmadı → dosya henüz okunmadı
-- Bu noktada stats dosyasını hazırlarsak Cuberite sorunsuz okur.
function OnLogin(Client, ProtocolVersion, Username)
    if not Username or Username == "" then return false end

    local statsDir  = "world/data/stats"
    local statsPath = statsDir .. "/" .. Username .. ".json"

    EnsureDir(statsDir)

    if not IsValidStatsFile(statsPath) then
        -- Bozuk dosyayı yedekle
        local corrupt = statsDir .. "/_corrupt_" .. Username .. ".json.bak"
        os.rename(statsPath, corrupt)
        -- Geçerli içerikle yeniden yaz
        WriteStatsFile(statsPath, Username)
    end

    -- players/ dizini de garantile
    EnsureDir("players")

    return false  -- girişi engelleme, sadece hazırlık yap
end

-- ── Çıkış hook'u ─────────────────────────────────────────────────
-- HOOK_PLAYER_DESTROYED: oyuncu çıktıktan sonra stats dosyasını
-- kontrol et. Bozulduysa sıfırla → bir sonraki girişte sorun olmaz.
function OnPlayerDestroyed(Player)
    local username  = Player:GetName()
    local statsPath = "world/data/stats/" .. username .. ".json"

    -- Kısa gecikme sonrası kontrol — Cuberite diske yazma zamanı geçsin
    -- Not: Lua'da sleep yok, ama dosya yoksa veya bozuksa düzelt
    if not IsValidStatsFile(statsPath) then
        -- Cuberite bazen çıkışta sıfır byte'lık dosya bırakır
        WriteStatsFile(statsPath, username)
    end

    return false
end

-- ── Başlatma ─────────────────────────────────────────────────────
function Initialize(Plugin)
    Plugin:SetName("PlayerSave")
    Plugin:SetVersion(1)
    PLUGIN = Plugin

    cPluginManager.AddHook(cPluginManager.HOOK_LOGIN,             OnLogin)
    cPluginManager.AddHook(cPluginManager.HOOK_PLAYER_DESTROYED,  OnPlayerDestroyed)

    -- Başlarken mevcut stats dizinini tara — bozuk dosyaları temizle
    local statsDir = "world/data/stats"
    EnsureDir(statsDir)

    LOG("====================================")
    LOG("[PlayerSave] v1.0 yuklenicdi")
    LOG("[PlayerSave] HOOK_LOGIN + HOOK_PLAYER_DESTROYED aktif")
    LOG("[PlayerSave] Stats dizini: " .. statsDir)
    LOG("====================================")

    return true
end

function OnDisable()
    LOG("[PlayerSave] devre dışı")
end
