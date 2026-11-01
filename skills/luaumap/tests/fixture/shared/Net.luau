local ReplicatedStorage = game:GetService("ReplicatedStorage")

local Net = {}

function Net.get(name: string): RemoteEvent
	return ReplicatedStorage:WaitForChild("Remotes"):WaitForChild(name)
end

Net.Buy = Net.get("Buy")

function Net.fire(name: string, ...)
	Net.get(name):FireServer(...)
end

function Net.remote(name: string)
	return Net.get(name)
end

function Net.setFlag(player: Player, key: string, value: any)
	player:SetAttribute(key, value)
end

return Net
