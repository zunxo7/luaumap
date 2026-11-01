local Util = require(script.Parent.Util)
local Shared = game:GetService("ReplicatedStorage").Shared
local SharedUtil = require(Shared.Util)
local Net = require(Shared.Net)

Util.server()
SharedUtil.shared()

Net.get("Buy").OnServerEvent:Connect(function(player) end)
