"""
webserver.py - servidor web del bot (corre dentro del mismo event loop que discord.py).

Rutas:
  GET /            -> "Bot online" (keepalive para Render/Railway/UptimeRobot)
  GET /health      -> JSON minimo {"ok": true, "ready": bool}   (publico)
  GET /api/status  -> JSON con estadisticas del bot            (requiere token)
  GET /dashboard   -> panel HTML que se refresca solo           (requiere token)

Variables de entorno:
  PORT o WEB_PORT      puerto (si no hay ninguno, el servidor no arranca)
  WEB_HOST             por defecto 0.0.0.0
  DASHBOARD_TOKEN      token para /api y /dashboard (si no esta, esas rutas quedan desactivadas)

Uso en el bot:
  import webserver
  ...
  async def main():
      await webserver.start(client, extra=my_extra_stats)   # extra es opcional
      await client.start(DISCORD_TOKEN)
"""
import hmac
import os
import time
from typing import Callable, Optional

from aiohttp import web

START_TIME = time.time()

DASHBOARD_HTML = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Panel del bot</title>
<style>
  :root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mut:#6b7385;--ok:#2ecc71;--bad:#e74c3c;--bd:#e3e6ec}
  @media (prefers-color-scheme:dark){:root{--bg:#14161b;--card:#1d2027;--fg:#e8eaf0;--mut:#8b93a5;--bd:#2b2f39}}
  *{box-sizing:border-box}
  body{margin:0;padding:24px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
  h1{margin:0 0 4px;font-size:22px} .sub{color:var(--mut);margin-bottom:20px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
  .card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px}
  .k{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
  .v{font-size:24px;font-weight:600;margin-top:2px}
  table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--bd);border-radius:12px;overflow:hidden}
  th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--bd)}
  th{color:var(--mut);font-size:12px;text-transform:uppercase}
  tr:last-child td{border-bottom:0}
  .wrap{overflow-x:auto}
  .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;background:var(--bad)}
  .dot.on{background:var(--ok)}
  #err{color:var(--bad);margin:10px 0}
</style></head><body>
<h1><span id="dot" class="dot"></span><span id="name">Bot</span></h1>
<div class="sub" id="sub">Cargando...</div>
<div id="err"></div>
<div class="grid" id="cards"></div>
<div class="wrap"><table><thead><tr><th>Servidor</th><th>Miembros</th><th>Musica</th></tr></thead>
<tbody id="guilds"></tbody></table></div>
<script>
const token = new URLSearchParams(location.search).get('token') || '';
const fmtUp = s => { const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  return (d?d+'d ':'')+(h?h+'h ':'')+m+'m'; };
function card(k,v){ const c=document.createElement('div'); c.className='card';
  const a=document.createElement('div'); a.className='k'; a.textContent=k;
  const b=document.createElement('div'); b.className='v'; b.textContent=v;
  c.append(a,b); return c; }
async function load(){
  try{
    const r = await fetch('/api/status', {headers:{'Authorization':'Bearer '+token}});
    if(!r.ok) throw new Error(r.status===401 ? 'Token invalido (usa ?token=...)' : 'HTTP '+r.status);
    const d = await r.json();
    document.getElementById('err').textContent='';
    document.getElementById('dot').className='dot'+(d.ready?' on':'');
    document.getElementById('name').textContent=d.bot.name||'Bot';
    document.getElementById('sub').textContent='Activo hace '+fmtUp(d.uptime_seconds);
    const cards=document.getElementById('cards'); cards.replaceChildren(
      card('Latencia', d.latency_ms+' ms'), card('Servidores', d.guild_count),
      card('Miembros', d.member_count), card('Reproductores', d.music_players),
      ...Object.entries(d.extra||{}).map(([k,v])=>card(k,v)));
    const tb=document.getElementById('guilds'); tb.replaceChildren();
    d.guilds.forEach(g=>{ const tr=document.createElement('tr');
      [g.name,g.members,g.music?'Sonando':'-'].forEach(x=>{const td=document.createElement('td'); td.textContent=x; tr.append(td);});
      tb.append(tr); });
  }catch(e){ document.getElementById('err').textContent=String(e.message||e); document.getElementById('dot').className='dot'; }
}
load(); setInterval(load, 5000);
</script></body></html>
"""


def _authorized(request: web.Request, token: str) -> bool:
    if not token:
        return False
    header = request.headers.get('Authorization', '')
    supplied = header[7:] if header.lower().startswith('bearer ') else request.query.get('token', '')
    return hmac.compare_digest(supplied.encode(), token.encode())


def _build_app(client, extra: Optional[Callable[[], dict]], token: str) -> web.Application:
    routes = web.RouteTableDef()

    def guard(request):
        if not token:
            raise web.HTTPForbidden(text='Define DASHBOARD_TOKEN para activar esta ruta.')
        if not _authorized(request, token):
            raise web.HTTPUnauthorized(text='Token invalido.')

    @routes.get('/')
    async def index(request):
        return web.Response(text='Bot online')

    @routes.get('/health')
    async def health(request):
        return web.json_response({'ok': True, 'ready': client.is_ready()})

    @routes.get('/api/status')
    async def status(request):
        guard(request)
        guilds = list(client.guilds)
        players = getattr(request.app['music_getter'](), 'keys', lambda: [])()
        playing = set(players)
        data = {
            'ready': client.is_ready(),
            'uptime_seconds': int(time.time() - START_TIME),
            'latency_ms': round(client.latency * 1000) if client.latency == client.latency else None,  # NaN-safe
            'bot': {'name': str(client.user) if client.user else None,
                    'id': str(client.user.id) if client.user else None},
            'guild_count': len(guilds),
            'member_count': sum(g.member_count or 0 for g in guilds),
            'music_players': len(playing),
            'guilds': [
                {'id': str(g.id), 'name': g.name, 'members': g.member_count or 0, 'music': g.id in playing}
                for g in sorted(guilds, key=lambda g: -(g.member_count or 0))
            ],
        }
        if extra:
            try:
                data['extra'] = extra()
            except Exception as e:
                data['extra'] = {'error': str(e)[:100]}
        return web.json_response(data)

    @routes.get('/dashboard')
    async def dashboard(request):
        guard(request)
        return web.Response(text=DASHBOARD_HTML, content_type='text/html')

    app = web.Application()
    app.add_routes(routes)
    return app


async def start(client, extra: Optional[Callable[[], dict]] = None,
                music_players_getter: Optional[Callable[[], dict]] = None) -> Optional[web.AppRunner]:
    """Arranca el servidor en el loop actual. Devuelve el runner (o None si no hay puerto)."""
    port = os.environ.get('PORT') or os.environ.get('WEB_PORT')
    if not port:
        return None
    host = os.environ.get('WEB_HOST', '0.0.0.0')
    token = os.environ.get('DASHBOARD_TOKEN', '').strip()

    app = _build_app(client, extra, token)
    app['music_getter'] = music_players_getter or (lambda: {})

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, int(port)).start()
    print(f'[Web] Servidor en {host}:{port} | dashboard={"ON" if token else "OFF (falta DASHBOARD_TOKEN)"}')
    return runner


async def stop(runner: Optional[web.AppRunner]):
    if runner is not None:
        await runner.cleanup()
