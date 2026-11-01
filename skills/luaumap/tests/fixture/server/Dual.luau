local Util, Net
if game then
	Util = require(game:GetService("ReplicatedStorage").Shared.Util)
	Net = require(game:GetService("ReplicatedStorage").Shared.Net)
else
	Util = require("../shared/Util")
	Net = require("../lib/Net")
end

local Missing = require(script.Parent.Missing)

return { Util = Util, Net = Net, Missing = Missing }
