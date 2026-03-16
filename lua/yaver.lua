local ActiveWolves = {}
local OpenBackpacks = {} 
local WolfTargets = {} -- YENİ: Kurdun kime saldıracağını aklında tuttuğu liste
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
    
    -- YENİ: Savaş sırasında kurdun daha hızlı tepki vermesi için süreyi 10 tick'e (0.5 saniye) düşürdük
    cRoot:Get():GetDefaultWorld():ScheduleTask(10, PeriodicWolfTask)
    LOG("[YAVER] Saf Obje Modu, Savas AI'si, Hedef Takibi ve Canta Sistemi Aktif!")
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
        local P_UUID = a_Player:GetUUID()
        local Ini2 = cIniFile()
        Ini2:ReadFile("YaverInv.ini")
        for i=0, 26 do
            local Itm = a_Window:GetSlot(a_Player, i)
            if not Itm:IsEmpty() then
                Ini2:SetValue(P_UUID, "Slot_"..i, Itm.m_ItemType .. ";" .. Itm.m_ItemCount .. ";" .. Itm.m_ItemDamage)
            else
                Ini2:SetValue(P_UUID, "Slot_"..i, "")
            end
        end
        Ini2:WriteFile("YaverInv.ini")
        OpenBackpacks[P_UUID] = nil
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
        WolfTargets[ActiveWolves[UUID]] = nil
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

function HandleKurtCommand(Split, Player)
    local UUID = Player:GetUUID()
    if ActiveWolves[UUID] then
        Player:SendMessageWarning("§eKurdun zaten aktif! Yanina isinlaniyor...")
        Player:GetWorld():DoWithEntityByID(ActiveWolves[UUID], function(Ent)
            Ent:TeleportToEntity(Player)
            WolfTargets[Ent:GetUniqueID()] = nil -- Yanına çağırınca hedefi sıfırla
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
        WolfTargets[WolfID] = nil
    end
end

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
                OpenBackpacks[UUID] = Window 
                Player:OpenWindow(Window)
            end
            return true
        end
    end
    return false
end

-- ================= Savas ve Hasar =================
function OnTakeDamage(Receiver, TCA)
    local Attacker = TCA.Attacker
    if not Attacker then return false end
    
    -- Dost ateşi koruması (Sen <-> Kurdun)
    if IsWolf(Receiver) and Attacker:IsPlayer() then
        for uuid, wid in pairs(ActiveWolves) do
            if wid == Receiver:GetUniqueID() and uuid == Attacker:GetUUID() then return true end
        end
    end
    
    if Receiver:IsPlayer() and IsWolf(Attacker) then
        for uuid, wid in pairs(ActiveWolves) do
            if wid == Attacker:GetUniqueID() and uuid == Receiver:GetUUID() then return true end
        end
    end
    
    -- Kurt birine vuruyorsa hasar bonusu
    if IsWolf(Attacker) then
        for uuid, wid in pairs(ActiveWolves) do
            if wid == Attacker:GetUniqueID() then
                local lvl = GetWolfLevel(uuid)
                TCA.FinalDamage = TCA.FinalDamage + (lvl * 1.5)
                AddWolfXP(uuid, 5)
            end
        end
    end
    
    -- YENİ AI: Sana biri saldırırsa, kurda hedef göster
    if Receiver:IsPlayer() then
        local UUID = Receiver:GetUUID()
        local WolfID = ActiveWolves[UUID]
        if WolfID and Attacker:GetUniqueID() ~= WolfID then
            WolfTargets[WolfID] = Attacker:GetUniqueID()
        end
    end

    -- YENİ AI: Sen birine saldırırsan, kurda hedef göster
    if Attacker:IsPlayer() then
        local UUID = Attacker:GetUUID()
        local WolfID = ActiveWolves[UUID]
        if WolfID and Receiver:GetUniqueID() ~= WolfID then
            -- Sadece moblara veya diğer oyunculara saldır (kendi kurduna dalmasın diye)
            if Receiver:IsMob() or (Receiver:IsPlayer() and Receiver:GetUUID() ~= UUID) then
                WolfTargets[WolfID] = Receiver:GetUniqueID()
            end
        end
    end
end

function OnKilled(Victim, TCA, CustomDeathMessage)
    local Attacker = TCA.Attacker
    
    if IsWolf(Victim) then
        for uuid, wid in pairs(ActiveWolves) do
            if wid == Victim:GetUniqueID() then
                ActiveWolves[uuid] = nil
                WolfTargets[wid] = nil
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

-- ================= Periyodik Kontrol (YENI SAVAŞ AI & Işınlanma) =================
function PeriodicWolfTask(World)
    World:ForEachPlayer(function(Player)
        local UUID = Player:GetUUID()
        local WolfID = ActiveWolves[UUID]
        
        if WolfID then
            World:DoWithEntityByID(WolfID, function(Ent)
                local Wolf = tolua.cast(Ent, "cMonster")
                if not Wolf then return end
                
                local TargetID = WolfTargets[WolfID]
                local HasValidTarget = false
                
                -- YENİ: Hedef sistemi
                if TargetID then
                    World:DoWithEntityByID(TargetID, function(TargetEnt)
                        -- Hedef hala hayatta mı kontrol et
                        if (TargetEnt:IsMob() or TargetEnt:IsPlayer()) and TargetEnt:GetHealth() > 0 then
                            HasValidTarget = true
                            local distToTarget = (Ent:GetPosition() - TargetEnt:GetPosition()):Length()
                            
                            if distToTarget > 20 then
                                -- Hedef çok uzaklaştıysa peşini bırak
                                HasValidTarget = false
                            elseif distToTarget > 2.5 then
                                -- Hedefe doğru koş
                                Wolf:MoveToPosition(TargetEnt:GetPosition())
                            else
                                -- Hedefe yeterince yakınsa saldır! (Gerçek hasar verme kısmı)
                                local dmg = 4 + (GetWolfLevel(UUID) * 1.5)
                                pcall(function() 
                                    local dtType = cEntity.dtMobAttack or 3
                                    TargetEnt:TakeDamage(dtType, Ent, dmg, 1)
                                    -- Kurdun vurma animasyonunu oynat
                                    World:BroadcastEntityAnimation(Ent, 0) 
                                end)
                            end
                        end
                    end)
                end
                
                -- Savaşta değilse veya hedef kaybolduysa sahibini takip et
                if not HasValidTarget then
                    WolfTargets[WolfID] = nil -- Hedefi sil
                    local distToPlayer = (Ent:GetPosition() - Player:GetPosition()):Length()
                    
                    if distToPlayer > 15 then 
                        Ent:TeleportToEntity(Player) 
                    elseif distToPlayer > 4 then
                        Wolf:MoveToPosition(Player:GetPosition())
                    end
                end
                
                -- Bufflar ve Can Yenileme (Efektleri artık daha kısa süreli atıyoruz çünkü tick hızlandı)
                local lvl = GetWolfLevel(UUID)
                if lvl >= 5 then Player:AddEntityEffect(cEntityEffect.effSpeed, 20*1, 0) end
                if lvl >= 10 then Player:AddEntityEffect(cEntityEffect.effStrength, 20*1, 0) end
                if lvl >= 20 then Player:AddEntityEffect(cEntityEffect.effRegeneration, 20*1, 0) end
                
                if Wolf:GetHealth() < Wolf:GetMaxHealth() then pcall(function() Wolf:Heal(1) end) end
            end)
        end
    end)
    World:ScheduleTask(10, PeriodicWolfTask) -- 10 tick (0.5 saniyede bir döngüyü tekrarla)
end
