class DiyAPI:
    def __init__(self, bot):
        self.bot = bot

    def __getattr__(self, name):
        async def api_method(**params):
            return await self.bot._diy_call(name, params)

        return api_method
