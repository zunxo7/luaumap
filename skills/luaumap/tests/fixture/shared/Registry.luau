local ReplicatedStorage = game:GetService("ReplicatedStorage")

local Registry = {}

local function event(folder, name)
	local e = folder:FindFirstChild(name)
	if not e then
		e = Instance.new("RemoteEvent")
		e.Name = name
		e.Parent = folder
	end
	return e
end

function Registry.init()
	local folder = ReplicatedStorage:FindFirstChild("Remotes")
	Registry.Score = event(folder, "Score")
	return Registry
end

return Registry
