PLUGIN = {
    "name": "hello",
    "description": "Ein kleines Beispiel-Plugin fuer CAINE.",
}


async def setup_plugin(api):
    @api.command("hello", description="Begruesst dich.")
    async def hello(ctx, args):
        name = ctx.author.display_name
        await api.reply(ctx, f"Hallo {name}. CAINE ist online.")
