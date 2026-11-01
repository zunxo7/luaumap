local Registry = require(game:GetService("ReplicatedStorage").Shared.Registry).init()
Registry.Score:FireAllClients(1)
Registry.Score.OnServerEvent:Connect(function() end)

local function relay(bag)
	bag.Ghost:FireAllClients()
end

for slot = 1, Registry.SlotCount do
	workspace:SetAttribute("Slot" .. slot .. "Tier", 1)
end

relay({})
