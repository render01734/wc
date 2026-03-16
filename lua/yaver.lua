local ActiveWolves = {}
local Ini = nil

local function Split(str, sep)
    local res = {}
    for w in string.gmatch(str, "([^"..sep.."]+)") do table.insert(res, w) end
    return res
end

local function DoWithPlayer(UUID, Callback)
    cRoot:Get():ForEachPlayer(function(P)
        if P:GetUUID() == UUID then Callback(P) end
    end)
end

local function GetWolfType()
    if cMonster and cMonster.mtWolf then return cMonster.mtWolf end
    local wt = nil
    if cMonster and cMonster.StringToMobType then
        pcall(function() wt = cMonster.StringToMobType("wolf") end)
        if wt and wt ~= -1 then return wt end
        pcall(function() wt = cMonster.StringToMobType("Wolf") end)
        if wt and wt ~= -1 then return wt end
    end
    if mtWolf then return mtWolf end
    return 95 
end

local function IsWolf(Entity)
    if not Entity or not Entity:IsMob() then return false end
    local t = Entity:GetMobType()
    local wt = GetWolfType()
    return (t == wt) or (t == 95)
end

function Initialize(Plugin)
    Plugin:SetName("yaver")
    Plugin:SetVersion(11)
    
    Ini = cIniFile()
    Ini:ReadFile("YaverData.ini")
    
    cPluginManager:AddHook(cPluginManager.HOOK_PLAYER_SPAWNED, OnPlayerSpawned)
    cPluginManager:AddHook(cPluginManager.HOOK_PLAYER_DESTROYED, OnPlayerDestroyed)
    cPluginManager:AddHook(cPluginManager.HOOK_PLAYER_RIGHT_CLICKING_ENTITY, OnRightClickingEntity)
    cPluginManager:AddHook(cPluginManager.HOOK_TAKE_DAMAGE, OnTakeDamage)
    cPluginManager:AddHook(cPluginManager.HOOK_KILLED, OnKilled)
    
    cPluginManager:BindCommand("/kurt", "", HandleKurtCommand, "Koruyucu kurdunu yanina cagirir.")
    
    cRoot:Get():GetDefaultWorld():ScheduleTask(20 * 2, PeriodicWolfTask)
    LOG("[YAVER] Saf Obje Modu, Anti-Disconnect ve NIL Fix Sistemi Aktif!")
    return true
end

-- ================= XP ve Seviye Sistemi =================
function GetWolfLevel(UUID) return Ini:GetValueI(UUID, "Level", 1) end
function GetWolfXP(UUID) return Ini:GetValueI(UUID, "XP", 0) end

function AddWolfXP(UUID, Amount)
    local lvl = GetWolfLevel(UUID)
    local xp = GetWolfXP(UUID) + Amount
    local req = lvl * 100
    
    if xp >= req then
        lvl = lvl + 1
        xp = 0
        Ini:SetValueI(UUID, "Level", lvl)
        
        DoWithPlayer(UUID, function(Player)
            Player:SendMessageSuccess("§6[Yaver] §aKoruyucu Kurdun Seviye Atladi! Yeni Seviye: §e" .. lvl)
            local WolfID = ActiveWolves[UUID]
            if WolfID then
                Player:GetWorld():DoWithEntityByID(WolfID, function(Ent)
                    Ent:SetCustomName("§b" .. Player:GetName() .. " §7Kurdu §e[Lv " .. lvl .. "] §8| §a(Shift+Tık)")
                    local Monster = tolua.cast(Ent, "cMonster")
                    if Monster then
                        local maxHp = 20 + (lvl * 2)
                        Monster:SetMaxHealth(maxHp)
                        Monster:Heal(maxHp)
                    end
                    pcall(function() Player:GetWorld():BroadcastEntityAnimation(Ent, 18) end)
                end)
            end
        end)
    end
    Ini:SetValueI(UUID, "XP", xp)
    Ini:WriteFile("YaverData.ini")
end

-- ================= Kurt Cantasi (Envanter) =================
function GetBackpack(UUID)
    local Window = cLuaWindow(cWindow.wtChest, 9, 3, "§8Yaver Cantasi")
    local InvIni = cIniFile()
    InvIni:ReadFile("YaverInv.ini")
    for i=0, 26 do
        local str = InvIni:GetValue(UUID, "Slot_"..i, "")
        if str ~= "" then
            local parts = Split(str, ";")
            local Itm = cItem(tonumber(parts[1] or 0), tonumber(parts[2] or 0), tonumber(parts[3] or 0))
            Window:SetSlot(nil, i, Itm)
        end
    end
    Window:SetOnClosed(function(a_Window, a_Player)
        local Ini2 = cIniFile()
        Ini2:ReadFile("YaverInv.ini")
        for i=0, 26 do
            local Itm = a_Window:GetSlot(a_Player, i)
            if not Itm:IsEmpty() then
                Ini2:SetValue(UUID, "Slot_"..i, Itm.m_ItemType .. ";" .. Itm.m_ItemCount .. ";" .. Itm.m_ItemDamage)
            else
                Ini2:SetValue(UUID, "Slot_"..i, "")
            end
        end
        Ini2:WriteFile("YaverInv.ini")
    end)
    return Window
end

-- ================= Kurt Çagirma =================
function SpawnWolfForPlayer(Player)
    local UUID = Player:GetUUID()
    local World = Player:GetWorld()
    
    if ActiveWolves[UUID] then
        World:DoWithEntityByID(ActiveWolves[UUID], function(Ent) Ent:Destroy() end)
        ActiveWolves[UUID] = nil
    end
    
    local WolfType = GetWolfType()
    local WolfID = World:SpawnMob(Player:GetPosX(), Player:GetPosY() + 1.0, Player:GetPosZ(), WolfType)
    
    if WolfID and WolfID ~= cEntity.INVALID_ID then
        ActiveWolves[UUID] = WolfID
        
        World:DoWithEntityByID(WolfID, function(Ent)
            local lvl = GetWolfLevel(UUID)
            Ent:SetCustomName("§b" .. Player:GetName() .. " §7Kurdu §e[Lv " .. lvl .. "] §8| §a(Shift+Tık)")
            Ent:SetCustomNameAlwaysVisible(true)
            
            local Monster = tolua.cast(Ent, "cMonster")
            if Monster then
                local maxHp = 20 + (lvl * 2)
                Monster:SetMaxHealth(maxHp)
                Monster:SetHealth(maxHp)
            end
        end)
    else
        Player:SendMessageFailure("§cOyun motoru kurt uretemedi.")
    end
end

-- MANUEL KURT CAGIRMA KOMUTU
function HandleKurtCommand(Split, Player)
    local UUID = Player:GetUUID()
    if ActiveWolves[UUID] then
        Player:SendMessageWarning("§eKurdun zaten aktif! Yanina isinlaniyor...")
        Player:GetWorld():DoWithEntityByID(ActiveWolves[UUID], function(Ent)
            Ent:TeleportToEntity(Player)
        end)
    else
        SpawnWolfForPlayer(Player)
        Player:SendMessageSuccess("§aSadik kurdun yanina cagirildi!")
    end
    return true
end

function OnPlayerSpawned(Player)
    local UUID = Player:GetUUID()
    if not ActiveWolves[UUID] then
        Player:GetWorld():ScheduleTask(40, function()
            DoWithPlayer(UUID, function(P) SpawnWolfForPlayer(P) end)
        end)
    end
end

function OnPlayerDestroyed(Player)
    local UUID = Player:GetUUID()
    local WolfID = ActiveWolves[UUID]
    if WolfID then
        Player:GetWorld():DoWithEntityByID(WolfID, function(Ent) Ent:Destroy() end)
        ActiveWolves[UUID] = nil
    end
end

-- ================= Etkilesim =================
function OnRightClickingEntity(Player, Entity)
    if IsWolf(Entity) then
        local UUID = Player:GetUUID()
        if ActiveWolves[UUID] == Entity:GetUniqueID() then
            
            if Player:IsCrouched() then
                cPluginManager:Get():ExecuteCommand(Player, "/hub")
                return true
            end
            
            local Item = Player:GetEquippedItem()
            local MeatIDs = { [319]=true, [320]=true, [363]=true, [364]=true, [365]=true, [366]=true, [367]=true, [423]=true, [424]=true, [411]=true, [412]=true }
            
            if MeatIDs[Item.m_ItemType] then
                Item.m_ItemCount = Item.m_ItemCount - 1
                if Item.m_ItemCount <= 0 then Item:Empty() end
                Player:GetInventory():SetEquippedItem(Item)
                
                local Monster = tolua.cast(Entity, "cMonster")
                if Monster then Monster:Heal(10) end
                
                pcall(function() Player:GetWorld():BroadcastEntityAnimation(Entity, 18) end)
                AddWolfXP(UUID, 50)
                Player:SendMessageInfo("§6[Yaver] §aKurdunu besledin! (+50 XP, +10 Can)")
            else
                local Window = GetBackpack(UUID)
                Player:OpenWindow(Window)
            end
            return true
        end
    end
    return false
end

-- ================= Savas ve Hasar (Özel AI ve Dost Ateşi Koruması) =================
function OnTakeDamage(Receiver, TCA)
    local Attacker = TCA.Attacker
    if not Attacker then return false end
    
    if IsWolf(Receiver) and Attacker:IsPlayer() then
        for uuid, wid in pairs(ActiveWolves) do
            if wid == Receiver:GetUniqueID() and uuid == Attacker:GetUUID() then
                return true 
            end
        end
    end
    
    if Receiver:IsPlayer() and IsWolf(Attacker) then
        for uuid, wid in pairs(ActiveWolves) do
            if wid == Attacker:GetUniqueID() and uuid == Receiver:GetUUID() then
                return true 
            end
        end
    end
    
    if IsWolf(Attacker) then
        for uuid, wid in pairs(ActiveWolves) do
            if wid == Attacker:GetUniqueID() then
                local lvl = GetWolfLevel(uuid)
                TCA.FinalDamage = TCA.FinalDamage + (lvl * 1.5)
                AddWolfXP(uuid, 5)
            end
        end
    end
    
    if Receiver:IsPlayer() then
        local UUID = Receiver:GetUUID()
        local WolfID = ActiveWolves[UUID]
        if WolfID and Attacker:GetUniqueID() ~= WolfID then
            Receiver:GetWorld():DoWithEntityByID(WolfID, function(Ent)
                local Wolf = tolua.cast(Ent, "cMonster")
                if Wolf then Wolf:MoveToPosition(Attacker:GetPosition()) end
            end)
        end
    end
end

function OnKilled(Victim, TCA, CustomDeathMessage)
    local Attacker = TCA.Attacker
    
    if IsWolf(Victim) then
        for uuid, wid in pairs(ActiveWolves) do
            if wid == Victim:GetUniqueID() then
                ActiveWolves[uuid] = nil
                DoWithPlayer(uuid, function(P)
                    P:SendMessageWarning("§cKoruyucu kurdun ağır yaralandı! 30 saniye içinde iyileşip dönecek.")
                end)
                
                Victim:GetWorld():ScheduleTask(20 * 30, function()
                    DoWithPlayer(uuid, function(Player)
                        SpawnWolfForPlayer(Player)
                        Player:SendMessageSuccess("§aKoruyucu kurdun iyileşti ve yanına döndü!")
                    end)
                end)
                break
            end
        end
    end
    
    if Attacker and Attacker:IsPlayer() then
        local uuid = Attacker:GetUUID()
        if ActiveWolves[uuid] then AddWolfXP(uuid, 25) end
    end
end

-- ================= Periyodik Kontrol (AI & Işınlanma) =================
function PeriodicWolfTask(World)
    World:ForEachPlayer(function(Player)
        local UUID = Player:GetUUID()
        local WolfID = ActiveWolves[UUID]
        if WolfID then
            World:DoWithEntityByID(WolfID, function(Ent)
                local dist = (Ent:GetPosition() - Player:GetPosition()):Length()
                
                if dist > 15 then 
                    Ent:TeleportToEntity(Player) 
                elseif dist > 4 then
                    local Wolf = tolua.cast(Ent, "cMonster")
                    if Wolf then Wolf:MoveToPosition(Player:GetPosition()) end
                end
                
                local lvl = GetWolfLevel(UUID)
                if lvl >= 5 then Player:AddEntityEffect(cEntityEffect.effSpeed, 20*4, 0) end
                if lvl >= 10 then Player:AddEntityEffect(cEntityEffect.effStrength, 20*4, 0) end
                if lvl >= 20 then Player:AddEntityEffect(cEntityEffect.effRegeneration, 20*4, 0) end
                
                local Monster = tolua.cast(Ent, "cMonster")
                if Monster and Monster:GetHealth() < Monster:GetMaxHealth() then Monster:Heal(1) end
            end)
        end
    end)
    World:ScheduleTask(20 * 2, PeriodicWolfTask)
end
