local Sight = {}
Sight.__index = Sight

function Sight.new()
	return setmetatable({}, Sight)
end

function Sight:aim() end

return Sight
