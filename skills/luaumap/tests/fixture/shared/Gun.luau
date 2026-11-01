local Net = require(script.Parent.Net)
local Sight = require(script.Parent.Sight)

local Gun = {}
Gun.__index = Gun

export type Gun = typeof(setmetatable({} :: { ammo: number }, Gun))

--- Builds a gun.

function Gun.new()
	local self = setmetatable({}, Gun)
	self.remote = Net.get("Shoot")
	self.sight = Sight.new()
	return self
end

function Gun:fire()
	self.remote:FireServer()
	self.sight:aim()
end

function Gun.reload(self: Gun)
	self.remote:FireServer("reload")
end

return Gun
