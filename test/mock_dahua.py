"""Mock Dahua VTO HTTP API with digest challenge and an event stream."""
import asyncio
from aiohttp import web

def need_auth(req):
    if not req.headers.get("Authorization", "").startswith("Digest "):
        return web.Response(status=401, headers={
            "WWW-Authenticate": 'Digest realm="Login to VTO", qop="auth", nonce="abc123", opaque="xyz"'})
    return None

async def magic(req):
    if (r := need_auth(req)): return r
    a = req.query.get("action")
    return web.Response(text={"getDeviceType": "type=VTO2111D-P",
                              "getSerialNo": "sn=7F012345PAZ0001",
                              "getSoftwareVersion": "version=4.300.0000000.6.R,build:2022-05-10"}[a] + "\r\n")

async def config(req):
    if (r := need_auth(req)): return r
    return web.Response(text="\r\n".join([
        "table.Encode[0].MainFormat[0].Video.Width=1280",
        "table.Encode[0].MainFormat[0].Video.Height=960",
        "table.Encode[0].MainFormat[0].Video.FPS=25",
        "table.Encode[0].MainFormat[0].Video.BitRate=2048",
        "table.Encode[0].MainFormat[0].Video.Compression=H.264",
        "table.Encode[0].MainFormat[0].Video.GOP=50",
        "table.Encode[0].ExtraFormat[0].Video.Width=704",
        "table.Encode[0].ExtraFormat[0].Video.Height=576",
        "table.Encode[0].ExtraFormat[0].Video.FPS=15",
        "table.Encode[0].ExtraFormat[0].Video.BitRate=512",
        "table.Encode[0].ExtraFormat[0].Video.Compression=H.264"]) + "\r\n")

async def snapshot(req):
    if (r := need_auth(req)): return r
    return web.Response(body=b"\xff\xd8\xff\xe0FAKEJPEG\xff\xd9", content_type="image/jpeg")

def part(body):
    return (f"--myboundary\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n\r\n{body}\r\n")

async def events(req):
    if (r := need_auth(req)): return r
    resp = web.StreamResponse(headers={"Content-Type": "multipart/x-mixed-replace; boundary=myboundary"})
    await resp.prepare(req)
    seq = [(1, "Heartbeat"),
           (2, "Code=VideoMotion;action=Start;index=0;data={\n   \"Id\" : [ 0 ]\n}"),
           (1, 'Code=BackKeyLight;action=Pulse;index=0;data={\n   "State" : 1\n}'),
           (2, "Code=VideoMotion;action=Stop;index=0"),
           (5, "Heartbeat")]
    while True:
        for delay, body in seq:
            await asyncio.sleep(delay)
            await resp.write(part(body).encode())
    return resp

app = web.Application()
app.router.add_get("/cgi-bin/magicBox.cgi", magic)
app.router.add_get("/cgi-bin/configManager.cgi", config)
app.router.add_get("/cgi-bin/snapshot.cgi", snapshot)
app.router.add_get("/cgi-bin/eventManager.cgi", events)
web.run_app(app, host="127.0.0.1", port=8080, print=None)
