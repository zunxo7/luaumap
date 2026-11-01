local RS = game:GetService("ReplicatedStorage")
local CollectionService = game:GetService("CollectionService")
local Net = require(RS.Shared.Net)
local Attrs = require(RS.Shared.Attrs)
local Gun = require(RS.Shared.Gun)
local player = game:GetService("Players").LocalPlayer

Net.Buy:FireServer()
Net.remote("Sell"):FireServer()
Net.fire("Gift")
Net.setFlag(player, "Flag", true)
player:GetAttribute(Attrs.InRift)
player:GetAttribute(Attrs.Prefix .. "Floor")

local NAME = "Ping"
Net.get(NAME):FireServer()

local get = Net.get
get("Alias"):FireServer()

local r = Net.get("Fire2")
local fire = r.FireServer
fire(r)

local K = { a = "Ka", b = Net.get("Kb") }
player:SetAttribute(K.a, 1)
K.b:FireServer()

for _, n in { "L1", "L2" } do
	player:SetAttribute(n, true)
end

local LIST = { "M1", "M2" }
for _, n in LIST do
	player:GetAttribute(n)
end

for k in { X1 = 1, X2 = 2 } do
	player:GetAttribute(k)
end

player:SetAttribute("Rift" .. "Level", 1)
local PREFIX = "Shop"
player:SetAttribute(`{PREFIX}Open`, 1)
player:SetAttribute(if NAME == "x" then "IfA" else "IfB", 1)

for i = 1, 2 do
	player:SetAttribute(`Slot{i}`, 0)
end

for _, slot in Attrs.Slots do
	player:SetAttribute(`Source{slot}`, 0)
end

local function remote()
	return Net.get("Ret")
end
remote():FireServer()

CollectionService:AddTag(player, Attrs.Prefix)

local g = Gun.new()
g:fire()
g:reload()

local function typed(h: Gun.Gun)
	h:fire()
end

local store
local function use()
	store:fire()
end
local function init()
	store = Gun.new()
end

local dyn = tostring(math.random())
player:SetAttribute(dyn, 1)

return { typed = typed, use = use, init = init }
