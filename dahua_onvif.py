#!/usr/bin/env python3
"""
dahua-onvif - expose a Dahua device (e.g. VTO2111D-P doorbell) as a virtual ONVIF camera.

Input side talks to the device the same way the rroller/dahua Home Assistant
integration does:
  * HTTP API on port 80 with digest auth (magicBox.cgi, configManager.cgi)
  * snapshots from /cgi-bin/snapshot.cgi (fetched here, so the NVR needs no digest auth)
  * long-lived event stream /cgi-bin/eventManager.cgi?action=attach&codes=[All]
  * RTSP on port 554 for video

Output side: ONVIF Profile S (device + media services), PullPoint events
(motion + doorbell button) and WS-Discovery.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import struct
import sys
import time
import uuid
from urllib.parse import quote
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx
import yaml
from aiohttp import web

log = logging.getLogger("dahua-onvif")

# --------------------------------------------------------------------------- config

DEFAULTS = {
    "dahua": {"host": None, "port": 80, "rtsp_port": 554, "username": "admin",
              "password": "", "channel": 1, "events": True},
    "onvif": {"port": 8000, "name": None, "uuid": None, "mac": None,
              "advertise_ip": None, "username": "", "password": "", "discovery": True},
    "stream": {"embed_credentials": True, "main_url": None, "sub_url": None},
    "doorbell": {"hold_seconds": 3, "extra_codes": []},
    "log_level": "INFO",
}


def load_config(path: str) -> dict:
    with open(path) as f:
        user = yaml.safe_load(f) or {}
    cfg = {}
    for section, default in DEFAULTS.items():
        if isinstance(default, dict):
            cfg[section] = {**default, **(user.get(section) or {})}
        else:
            cfg[section] = user.get(section, default)
    cfg["dahua"]["password"] = os.environ.get("DAHUA_PASSWORD", cfg["dahua"]["password"])
    cfg["onvif"]["password"] = os.environ.get("ONVIF_PASSWORD", cfg["onvif"]["password"])
    if not cfg["dahua"]["host"]:
        sys.exit("config: dahua.host is required")
    return cfg


# --------------------------------------------------------------------------- helpers

def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime | float) -> str:
    if isinstance(t, (int, float)):
        t = dt.datetime.fromtimestamp(t, dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def find(el, name):
    if el is None:
        return None
    for e in el.iter():
        if local(e.tag) == name:
            return e
    return None


def findtext(el, name, default=None):
    e = find(el, name)
    return e.text.strip() if e is not None and e.text else default


DUR_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


def parse_timeout(text: str | None, default: float) -> float:
    """ISO 8601 duration (PT60S) or absolute time -> seconds from now."""
    if not text:
        return default
    text = text.strip()
    m = DUR_RE.match(text)
    if m and text not in ("P", "PT"):
        d, h, mi, s = m.groups()
        return int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)
    try:
        t = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return max(1.0, (t - utcnow()).total_seconds())
    except ValueError:
        return default


def parse_kv(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def local_ip_towards(host: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 80))
        return s.getsockname()[0]
    finally:
        s.close()


# --------------------------------------------------------------------------- Dahua side

EVENT_RE = re.compile(
    r"Code=([^;\r\n]+);action=([^;\r\n]+);index=([^;\r\n]+)(?:;data=(\{.*\}))?", re.S)


class Dahua:
    """Dahua HTTP API client (port 80, digest auth) - same endpoints as rroller/dahua."""

    def __init__(self, c: dict):
        self.c = c
        self.client = httpx.AsyncClient(
            base_url=f"http://{c['host']}:{c['port']}",
            auth=httpx.DigestAuth(c["username"], c["password"]),
            timeout=10,
        )
        self._snap = (0.0, b"")
        self._snap_lock = asyncio.Lock()

    async def kv(self, path: str) -> dict:
        r = await self.client.get(path)
        r.raise_for_status()
        return parse_kv(r.text)

    async def probe(self) -> dict:
        info = {"model": "Dahua", "serial": "", "firmware": "", "streams": {}}
        for key, path, field in (
            ("model", "/cgi-bin/magicBox.cgi?action=getDeviceType", "type"),
            ("serial", "/cgi-bin/magicBox.cgi?action=getSerialNo", "sn"),
            ("firmware", "/cgi-bin/magicBox.cgi?action=getSoftwareVersion", "version"),
        ):
            try:
                v = (await self.kv(path)).get(field)
                if v:
                    info[key] = v
            except Exception as e:  # noqa: BLE001
                log.warning("probe %s failed: %s", key, e)
        try:
            enc = await self.kv("/cgi-bin/configManager.cgi?action=getConfig&name=Encode")
            ch = int(self.c["channel"]) - 1
            for name, fmt in (("main", "MainFormat"), ("sub", "ExtraFormat")):
                p = f"table.Encode[{ch}].{fmt}[0].Video."
                if p + "Width" in enc:
                    info["streams"][name] = {
                        "width": int(enc[p + "Width"]),
                        "height": int(enc[p + "Height"]),
                        "fps": int(float(enc.get(p + "FPS", 25))),
                        "bitrate": int(enc.get(p + "BitRate", 2048)),
                        "codec": enc.get(p + "Compression", "H.264"),
                        "gop": int(enc.get(p + "GOP", 50)),
                    }
        except Exception as e:  # noqa: BLE001
            log.warning("could not read Encode config: %s", e)
        return info

    async def snapshot(self) -> bytes:
        async with self._snap_lock:  # max one request per second towards the device
            ts, data = self._snap
            if data and time.monotonic() - ts < 1.0:
                return data
            r = await self.client.get(f"/cgi-bin/snapshot.cgi?channel={self.c['channel']}")
            r.raise_for_status()
            self._snap = (time.monotonic(), r.content)
            return r.content

    async def events(self, handler):
        url = "/cgi-bin/eventManager.cgi?action=attach&codes=[All]&heartbeat=5"
        backoff = 2
        while True:
            try:
                async with self.client.stream(
                        "GET", url, timeout=httpx.Timeout(10, read=30)) as r:
                    r.raise_for_status()
                    m = re.search(r"boundary=\"?([^\";]+)", r.headers.get("content-type", ""))
                    boundary = "--" + (m.group(1) if m else "myboundary")
                    log.info("event stream connected")
                    backoff = 2
                    buf = ""
                    async for chunk in r.aiter_text():
                        buf += chunk
                        parts = buf.split(boundary)
                        buf = parts.pop()
                        if self._complete(buf):  # don't wait for the next boundary
                            parts.append(buf)
                            buf = ""
                        for part in parts:
                            self._dispatch(part, handler)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("event stream error: %s - reconnecting in %ss", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    @staticmethod
    def _complete(part: str) -> bool:
        hdr, sep, body = part.partition("\r\n\r\n")
        if not sep:
            return False
        m = re.search(r"Content-Length:\s*(\d+)", hdr, re.I)
        return bool(m) and len(body.encode()) >= int(m.group(1))

    @staticmethod
    def _dispatch(part: str, handler):
        m = EVENT_RE.search(part)
        if not m:
            return
        code, action, index, raw = m.groups()
        data = {}
        if raw:
            try:
                data = json.loads(raw)
            except ValueError:
                pass
        try:
            handler(code.strip(), action.strip(), index.strip(), data)
        except Exception:  # noqa: BLE001
            log.exception("event handler failed")


# --------------------------------------------------------------------------- events bus

TOPICS = {
    "motion": [
        ("tns1:VideoSource/MotionAlarm", [("Source", "VideoSource")], "State"),
        ("tns1:RuleEngine/CellMotionDetector/Motion",
         [("VideoSourceConfigurationToken", "VideoSourceConfig"),
          ("VideoAnalyticsConfigurationToken", "VideoAnalyticsConfig"),
          ("Rule", "MotionDetectorRule")], "IsMotion"),
    ],
    "ring": [
        ("tns1:Device/Trigger/DigitalInput", [("InputToken", "DoorBell")], "LogicalState"),
    ],
}


def notification(topic, source, data_name, value, op) -> str:
    src = "".join(f'<tt:SimpleItem Name="{n}" Value="{v}"/>' for n, v in source)
    return (
        "<wsnt:NotificationMessage>"
        '<wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet">'
        f"{topic}</wsnt:Topic><wsnt:Message>"
        f'<tt:Message UtcTime="{iso(utcnow())}" PropertyOperation="{op}">'
        f"<tt:Source>{src}</tt:Source>"
        f'<tt:Data><tt:SimpleItem Name="{data_name}" Value="{str(value).lower()}"/></tt:Data>'
        "</tt:Message></wsnt:Message></wsnt:NotificationMessage>"
    )


class Subscription:
    def __init__(self, ttl: float):
        self.id = uuid.uuid4().hex
        self.queue: list[str] = []
        self.event = asyncio.Event()
        self.expires = time.time() + ttl

    def push(self, msg: str):
        self.queue.append(msg)
        del self.queue[:-500]
        self.event.set()


class EventBus:
    def __init__(self):
        self.subs: dict[str, Subscription] = {}
        self.state = {"motion": False, "ring": False}

    def messages(self, key, op):
        return [notification(t, s, d, self.state[key], op) for t, s, d in TOPICS[key]]

    def set(self, key: str, value: bool):
        if self.state[key] == value:
            return
        self.state[key] = value
        log.info("event %s -> %s", key, value)
        for msg in self.messages(key, "Changed"):
            for sub in self.subs.values():
                sub.push(msg)

    def initialize(self, sub: Subscription):
        for key in self.state:
            for msg in self.messages(key, "Initialized"):
                sub.push(msg)

    async def reaper(self):
        while True:
            await asyncio.sleep(10)
            now = time.time()
            for sid in [s for s, sub in self.subs.items() if sub.expires < now]:
                log.debug("subscription %s expired", sid)
                del self.subs[sid]


# --------------------------------------------------------------------------- ONVIF side

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "xs": "http://www.w3.org/2001/XMLSchema",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "ter": "http://www.onvif.org/ver10/error",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "wsa": "http://www.w3.org/2005/08/addressing",
    "wstop": "http://docs.oasis-open.org/wsn/t-1",
    "tns1": "http://www.onvif.org/ver10/topics",
}
NS_DECL = " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items())
PUBLIC_OPS = {"GetSystemDateAndTime", "GetCapabilities", "GetServices",
              "GetServiceCapabilities", "GetEndpointReference", "GetWsdlUrl", "GetHostname"}


def envelope(body: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {NS_DECL}>'
            f"<s:Header/><s:Body>{body}</s:Body></s:Envelope>")


class Onvif:
    def __init__(self, cfg: dict, info: dict, dahua: Dahua, bus: EventBus, ip: str):
        self.cfg, self.info, self.dahua, self.bus, self.ip = cfg, info, dahua, bus, ip
        o = cfg["onvif"]
        self.port = int(o["port"])
        self.base = f"http://{ip}:{self.port}"
        seed = info.get("serial") or cfg["dahua"]["host"]
        self.uuid = o["uuid"] or str(uuid.uuid5(uuid.NAMESPACE_URL, "dahua-onvif:" + seed))
        h = hashlib.sha1(seed.encode()).hexdigest()
        self.mac = o["mac"] or "02:" + ":".join(h[i:i + 2] for i in range(0, 10, 2))
        self.name = o["name"] or info["model"]
        defaults = {
            "main": {"width": 1280, "height": 960, "fps": 25, "bitrate": 2048, "codec": "H.264", "gop": 50},
            "sub": {"width": 704, "height": 576, "fps": 15, "bitrate": 512, "codec": "H.264", "gop": 30},
        }
        self.profiles = {}
        for token, key, subtype in (("MainStream", "main", 0), ("SubStream", "sub", 1)):
            s = info["streams"].get(key) or defaults[key]
            enc = "JPEG" if "MJPG" in s["codec"].upper() else "H264"
            if "265" in s["codec"]:
                log.warning("%s is H.265 - ONVIF media1 can only advertise it as H264; "
                            "set the VTO to H.264 if the NVR has trouble", token)
            self.profiles[token] = {**s, "key": key, "subtype": subtype, "encoding": enc,
                                    "encoder": f"{token}Encoder"}

    # ---------------------------------------------------------------- http glue

    def routes(self, app: web.Application):
        app.router.add_post("/onvif/device_service", self.soap("device"))
        app.router.add_post("/onvif/media_service", self.soap("media"))
        app.router.add_post("/onvif/events_service", self.soap("events"))
        app.router.add_post("/onvif/events/sub/{sub}", self.soap("sub"))
        app.router.add_get("/snapshot/{token}.jpg", self.http_snapshot)
        app.router.add_get("/healthz", lambda r: web.json_response(
            {"ok": True, "state": self.bus.state, "subscriptions": len(self.bus.subs)}))

    def soap(self, service: str):
        async def handler(request: web.Request):
            try:
                root = ET.fromstring(await request.read())
            except ET.ParseError:
                return self.fault("s:Sender", "ter:WellFormed", "Malformed XML")
            body = find(root, "Body")
            if body is None or not len(body):
                return self.fault("s:Sender", "ter:WellFormed", "Empty body")
            op = body[0]
            action = local(op.tag)
            log.debug("%s %s", service, action)
            if action not in PUBLIC_OPS and not self.authorized(root):
                return self.fault("s:Sender", "ter:NotAuthorized", "Sender not authorized")
            prefixes = {"device": "d", "media": "m", "events": "e", "sub": "e"}
            fn = None
            for p in [prefixes[service], "d", "m", "e"]:
                fn = getattr(self, f"{p}_{action}", None)
                if fn:
                    break
            if fn is None:
                return self.fault("s:Receiver", "ter:ActionNotSupported", f"{action} not supported")
            try:
                result = fn(op, request)
                if asyncio.iscoroutine(result):
                    result = await result
            except LookupError as e:
                return self.fault("s:Sender", "ter:InvalidArgVal", str(e))
            if isinstance(result, web.Response):
                return result
            return web.Response(text=envelope(result), content_type="application/soap+xml",
                                charset="utf-8")
        return handler

    @staticmethod
    def fault(code, sub, reason, status=400):
        body = (f"<s:Fault><s:Code><s:Value>{code}</s:Value><s:Subcode><s:Value>{sub}</s:Value>"
                f'</s:Subcode></s:Code><s:Reason><s:Text xml:lang="en">{escape(reason)}</s:Text>'
                "</s:Reason></s:Fault>")
        return web.Response(text=envelope(body), status=status,
                            content_type="application/soap+xml", charset="utf-8")

    def authorized(self, root) -> bool:
        user, password = self.cfg["onvif"]["username"], self.cfg["onvif"]["password"]
        if not user:
            return True
        tok = find(root, "UsernameToken")
        if tok is None or findtext(tok, "Username") != user:
            return False
        pw_el = find(tok, "Password")
        given = (pw_el.text or "").strip() if pw_el is not None else ""
        nonce, created = findtext(tok, "Nonce"), findtext(tok, "Created", "")
        if pw_el is not None and "Digest" in (pw_el.get("Type") or "") and nonce:
            raw = base64.b64decode(nonce) + created.encode() + password.encode()
            expect = base64.b64encode(hashlib.sha1(raw).digest()).decode()
            return hmac.compare_digest(expect, given)
        return hmac.compare_digest(given, password)

    async def http_snapshot(self, request):
        try:
            data = await self.dahua.snapshot()
        except Exception as e:  # noqa: BLE001
            log.warning("snapshot failed: %s", e)
            return web.Response(status=502, text="snapshot failed")
        return web.Response(body=data, content_type="image/jpeg",
                            headers={"Cache-Control": "no-cache"})

    # ---------------------------------------------------------------- shared xml

    def profile(self, token: str):
        p = self.profiles.get(token or "MainStream")
        if p is None:
            raise LookupError(f"unknown profile {token}")
        return token or "MainStream", p

    def stream_uri(self, p) -> str:
        override = self.cfg["stream"].get(f"{p['key']}_url")
        if override:
            return override
        d = self.cfg["dahua"]
        cred = ""
        if self.cfg["stream"]["embed_credentials"]:
            cred = f"{quote(d['username'], safe='')}:{quote(d['password'], safe='')}@"
        return (f"rtsp://{cred}{d['host']}:{d['rtsp_port']}/cam/realmonitor"
                f"?channel={d['channel']}&subtype={p['subtype']}")

    def vsc_xml(self, tag="tt:VideoSourceConfiguration") -> str:
        m = self.profiles["MainStream"]
        return (f'<{tag} token="VideoSourceConfig"><tt:Name>VideoSourceConfig</tt:Name>'
                "<tt:UseCount>2</tt:UseCount><tt:SourceToken>VideoSource</tt:SourceToken>"
                f'<tt:Bounds x="0" y="0" width="{m["width"]}" height="{m["height"]}"/></{tag}>')

    def vec_xml(self, p, tag="tt:VideoEncoderConfiguration") -> str:
        h264 = (f"<tt:H264><tt:GovLength>{p['gop']}</tt:GovLength>"
                "<tt:H264Profile>Main</tt:H264Profile></tt:H264>") if p["encoding"] == "H264" else ""
        return (
            f'<{tag} token="{p["encoder"]}"><tt:Name>{p["encoder"]}</tt:Name><tt:UseCount>1</tt:UseCount>'
            f"<tt:Encoding>{p['encoding']}</tt:Encoding>"
            f"<tt:Resolution><tt:Width>{p['width']}</tt:Width><tt:Height>{p['height']}</tt:Height></tt:Resolution>"
            "<tt:Quality>4</tt:Quality>"
            f"<tt:RateControl><tt:FrameRateLimit>{p['fps']}</tt:FrameRateLimit>"
            f"<tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>{p['bitrate']}</tt:BitrateLimit></tt:RateControl>"
            f"{h264}<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0</tt:IPv4Address>"
            "</tt:Address><tt:Port>0</tt:Port><tt:TTL>0</tt:TTL><tt:AutoStart>false</tt:AutoStart></tt:Multicast>"
            f"<tt:SessionTimeout>PT60S</tt:SessionTimeout></{tag}>")

    def profile_xml(self, token, p, tag) -> str:
        return (f'<{tag} token="{token}" fixed="true"><tt:Name>{token}</tt:Name>'
                f"{self.vsc_xml()}{self.vec_xml(p)}</{tag}>")

    def scopes(self) -> list[str]:
        return ["onvif://www.onvif.org/type/video_encoder",
                "onvif://www.onvif.org/type/Network_Video_Transmitter",
                "onvif://www.onvif.org/Profile/Streaming",
                f"onvif://www.onvif.org/name/{quote(self.name)}",
                f"onvif://www.onvif.org/hardware/{quote(self.info['model'])}",
                "onvif://www.onvif.org/location/door"]

    # ---------------------------------------------------------------- device service

    def d_GetSystemDateAndTime(self, op, req):
        n = utcnow()
        return ("<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
                "<tt:DateTimeType>NTP</tt:DateTimeType><tt:DaylightSavings>false</tt:DaylightSavings>"
                "<tt:TimeZone><tt:TZ>UTC0</tt:TZ></tt:TimeZone><tt:UTCDateTime>"
                f"<tt:Time><tt:Hour>{n.hour}</tt:Hour><tt:Minute>{n.minute}</tt:Minute><tt:Second>{n.second}</tt:Second></tt:Time>"
                f"<tt:Date><tt:Year>{n.year}</tt:Year><tt:Month>{n.month}</tt:Month><tt:Day>{n.day}</tt:Day></tt:Date>"
                "</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>")

    def d_GetDeviceInformation(self, op, req):
        i = self.info
        return ("<tds:GetDeviceInformationResponse><tds:Manufacturer>Dahua</tds:Manufacturer>"
                f"<tds:Model>{escape(i['model'])}</tds:Model>"
                f"<tds:FirmwareVersion>{escape(i['firmware'] or 'unknown')}</tds:FirmwareVersion>"
                f"<tds:SerialNumber>{escape(i['serial'] or self.uuid)}</tds:SerialNumber>"
                f"<tds:HardwareId>{escape(i['model'])}</tds:HardwareId></tds:GetDeviceInformationResponse>")

    def d_GetCapabilities(self, op, req):
        b = self.base
        return ("<tds:GetCapabilitiesResponse><tds:Capabilities>"
                f"<tt:Device><tt:XAddr>{b}/onvif/device_service</tt:XAddr>"
                "<tt:Network><tt:IPFilter>false</tt:IPFilter><tt:ZeroConfiguration>false</tt:ZeroConfiguration>"
                "<tt:IPVersion6>false</tt:IPVersion6><tt:DynDNS>false</tt:DynDNS></tt:Network>"
                "<tt:System><tt:DiscoveryResolve>false</tt:DiscoveryResolve><tt:DiscoveryBye>true</tt:DiscoveryBye>"
                "<tt:RemoteDiscovery>false</tt:RemoteDiscovery><tt:SystemBackup>false</tt:SystemBackup>"
                "<tt:SystemLogging>false</tt:SystemLogging><tt:FirmwareUpgrade>false</tt:FirmwareUpgrade>"
                "<tt:SupportedVersions><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor></tt:SupportedVersions></tt:System>"
                "</tt:Device>"
                f"<tt:Events><tt:XAddr>{b}/onvif/events_service</tt:XAddr>"
                "<tt:WSSubscriptionPolicySupport>false</tt:WSSubscriptionPolicySupport>"
                "<tt:WSPullPointSupport>true</tt:WSPullPointSupport>"
                "<tt:WSPausableSubscriptionManagerInterfaceSupport>false</tt:WSPausableSubscriptionManagerInterfaceSupport>"
                "</tt:Events>"
                f"<tt:Media><tt:XAddr>{b}/onvif/media_service</tt:XAddr><tt:StreamingCapabilities>"
                "<tt:RTPMulticast>false</tt:RTPMulticast><tt:RTP_TCP>true</tt:RTP_TCP>"
                "<tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP></tt:StreamingCapabilities></tt:Media>"
                "</tds:Capabilities></tds:GetCapabilitiesResponse>")

    def d_GetServices(self, op, req):
        svc = [("http://www.onvif.org/ver10/device/wsdl", "device_service"),
               ("http://www.onvif.org/ver10/media/wsdl", "media_service"),
               ("http://www.onvif.org/ver10/events/wsdl", "events_service")]
        items = "".join(
            f"<tds:Service><tds:Namespace>{ns}</tds:Namespace><tds:XAddr>{self.base}/onvif/{path}</tds:XAddr>"
            "<tds:Version><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor></tds:Version></tds:Service>"
            for ns, path in svc)
        return f"<tds:GetServicesResponse>{items}</tds:GetServicesResponse>"

    def d_GetServiceCapabilities(self, op, req):
        return ("<tds:GetServiceCapabilitiesResponse><tds:Capabilities>"
                '<tds:Network IPFilter="false" ZeroConfiguration="false" IPVersion6="false" DynDNS="false"/>'
                '<tds:Security UsernameToken="true"/>'
                '<tds:System DiscoveryResolve="false" DiscoveryBye="true" RemoteDiscovery="false" '
                'SystemBackup="false" SystemLogging="false" FirmwareUpgrade="false"/>'
                "</tds:Capabilities></tds:GetServiceCapabilitiesResponse>")

    def d_GetScopes(self, op, req):
        items = "".join(f"<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>{s}</tt:ScopeItem></tds:Scopes>"
                        for s in self.scopes())
        return f"<tds:GetScopesResponse>{items}</tds:GetScopesResponse>"

    def d_GetHostname(self, op, req):
        name = re.sub(r"[^A-Za-z0-9-]", "-", self.name)
        return (f"<tds:GetHostnameResponse><tds:HostnameInformation><tt:FromDHCP>false</tt:FromDHCP>"
                f"<tt:Name>{name}</tt:Name></tds:HostnameInformation></tds:GetHostnameResponse>")

    def d_GetNetworkInterfaces(self, op, req):
        return ("<tds:GetNetworkInterfacesResponse><tds:NetworkInterfaces token=\"eth0\">"
                "<tt:Enabled>true</tt:Enabled><tt:Info><tt:Name>eth0</tt:Name>"
                f"<tt:HwAddress>{self.mac}</tt:HwAddress><tt:MTU>1500</tt:MTU></tt:Info>"
                "<tt:IPv4><tt:Enabled>true</tt:Enabled><tt:Config><tt:Manual>"
                f"<tt:Address>{self.ip}</tt:Address><tt:PrefixLength>24</tt:PrefixLength></tt:Manual>"
                "<tt:DHCP>false</tt:DHCP></tt:Config></tt:IPv4></tds:NetworkInterfaces>"
                "</tds:GetNetworkInterfacesResponse>")

    def d_GetNetworkProtocols(self, op, req):
        rtsp = self.cfg["dahua"]["rtsp_port"]
        return ("<tds:GetNetworkProtocolsResponse>"
                f"<tds:NetworkProtocols><tt:Name>HTTP</tt:Name><tt:Enabled>true</tt:Enabled><tt:Port>{self.port}</tt:Port></tds:NetworkProtocols>"
                f"<tds:NetworkProtocols><tt:Name>RTSP</tt:Name><tt:Enabled>true</tt:Enabled><tt:Port>{rtsp}</tt:Port></tds:NetworkProtocols>"
                "</tds:GetNetworkProtocolsResponse>")

    def d_GetDiscoveryMode(self, op, req):
        return ("<tds:GetDiscoveryModeResponse><tds:DiscoveryMode>Discoverable</tds:DiscoveryMode>"
                "</tds:GetDiscoveryModeResponse>")

    def d_GetRelayOutputs(self, op, req):
        return "<tds:GetRelayOutputsResponse/>"

    # ---------------------------------------------------------------- media service

    def m_GetServiceCapabilities(self, op, req):
        return ("<trt:GetServiceCapabilitiesResponse>"
                '<trt:Capabilities SnapshotUri="true" Rotation="false" VideoSourceMode="false" OSD="false">'
                '<trt:ProfileCapabilities MaximumNumberOfProfiles="2"/>'
                '<trt:StreamingCapabilities RTPMulticast="false" RTP_TCP="true" RTP_RTSP_TCP="true"/>'
                "</trt:Capabilities></trt:GetServiceCapabilitiesResponse>")

    def m_GetProfiles(self, op, req):
        items = "".join(self.profile_xml(t, p, "trt:Profiles") for t, p in self.profiles.items())
        return f"<trt:GetProfilesResponse>{items}</trt:GetProfilesResponse>"

    def m_GetProfile(self, op, req):
        token, p = self.profile(findtext(op, "ProfileToken"))
        return f"<trt:GetProfileResponse>{self.profile_xml(token, p, 'trt:Profile')}</trt:GetProfileResponse>"

    def m_GetStreamUri(self, op, req):
        _, p = self.profile(findtext(op, "ProfileToken"))
        return (f"<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>{escape(self.stream_uri(p))}</tt:Uri>"
                "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
                "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT0S</tt:Timeout>"
                "</trt:MediaUri></trt:GetStreamUriResponse>")

    def m_GetSnapshotUri(self, op, req):
        token, _ = self.profile(findtext(op, "ProfileToken"))
        return (f"<trt:GetSnapshotUriResponse><trt:MediaUri><tt:Uri>{self.base}/snapshot/{token}.jpg</tt:Uri>"
                "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
                "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT0S</tt:Timeout>"
                "</trt:MediaUri></trt:GetSnapshotUriResponse>")

    def m_GetVideoSources(self, op, req):
        m = self.profiles["MainStream"]
        return ('<trt:GetVideoSourcesResponse><trt:VideoSources token="VideoSource">'
                f"<tt:Framerate>{m['fps']}</tt:Framerate><tt:Resolution><tt:Width>{m['width']}</tt:Width>"
                f"<tt:Height>{m['height']}</tt:Height></tt:Resolution></trt:VideoSources></trt:GetVideoSourcesResponse>")

    def m_GetVideoSourceConfigurations(self, op, req):
        return (f"<trt:GetVideoSourceConfigurationsResponse>{self.vsc_xml('trt:Configurations')}"
                "</trt:GetVideoSourceConfigurationsResponse>")

    def m_GetVideoSourceConfiguration(self, op, req):
        return (f"<trt:GetVideoSourceConfigurationResponse>{self.vsc_xml('trt:Configuration')}"
                "</trt:GetVideoSourceConfigurationResponse>")

    def m_GetVideoEncoderConfigurations(self, op, req):
        items = "".join(self.vec_xml(p, "trt:Configurations") for p in self.profiles.values())
        return f"<trt:GetVideoEncoderConfigurationsResponse>{items}</trt:GetVideoEncoderConfigurationsResponse>"

    m_GetCompatibleVideoEncoderConfigurations = m_GetVideoEncoderConfigurations

    def _encoder(self, op):
        tok = findtext(op, "ConfigurationToken") or ""
        for t, p in self.profiles.items():
            if p["encoder"] == tok or t == findtext(op, "ProfileToken"):
                return p
        if tok:
            raise LookupError(f"unknown configuration {tok}")
        return self.profiles["MainStream"]

    def m_GetVideoEncoderConfiguration(self, op, req):
        return (f"<trt:GetVideoEncoderConfigurationResponse>{self.vec_xml(self._encoder(op), 'trt:Configuration')}"
                "</trt:GetVideoEncoderConfigurationResponse>")

    def m_GetVideoEncoderConfigurationOptions(self, op, req):
        p = self._encoder(op)
        return ("<trt:GetVideoEncoderConfigurationOptionsResponse><trt:Options>"
                "<tt:QualityRange><tt:Min>1</tt:Min><tt:Max>6</tt:Max></tt:QualityRange><tt:H264>"
                f"<tt:ResolutionsAvailable><tt:Width>{p['width']}</tt:Width><tt:Height>{p['height']}</tt:Height></tt:ResolutionsAvailable>"
                "<tt:GovLengthRange><tt:Min>1</tt:Min><tt:Max>150</tt:Max></tt:GovLengthRange>"
                f"<tt:FrameRateRange><tt:Min>1</tt:Min><tt:Max>{p['fps']}</tt:Max></tt:FrameRateRange>"
                "<tt:EncodingIntervalRange><tt:Min>1</tt:Min><tt:Max>1</tt:Max></tt:EncodingIntervalRange>"
                "<tt:H264ProfilesSupported>Main</tt:H264ProfilesSupported></tt:H264>"
                "</trt:Options></trt:GetVideoEncoderConfigurationOptionsResponse>")

    def m_GetAudioSources(self, op, req):
        return "<trt:GetAudioSourcesResponse/>"

    def m_GetAudioEncoderConfigurations(self, op, req):
        return "<trt:GetAudioEncoderConfigurationsResponse/>"

    # ---------------------------------------------------------------- events service

    def e_GetServiceCapabilities(self, op, req):
        return ('<tev:GetServiceCapabilitiesResponse><tev:Capabilities WSSubscriptionPolicySupport="false" '
                'WSPullPointSupport="true" WSPausableSubscriptionManagerInterfaceSupport="false" '
                'MaxNotificationProducers="0" MaxPullPoints="10"/></tev:GetServiceCapabilitiesResponse>')

    def e_GetEventProperties(self, op, req):
        def desc(source, data_name, src_type="tt:ReferenceToken"):
            s = "".join(f'<tt:SimpleItemDescription Name="{n}" Type="{src_type}"/>' for n, _ in source)
            return ('<tt:MessageDescription IsProperty="true">'
                    f"<tt:Source>{s}</tt:Source><tt:Data>"
                    f'<tt:SimpleItemDescription Name="{data_name}" Type="xs:boolean"/></tt:Data></tt:MessageDescription>')
        (t1, s1, d1), (t2, s2, d2) = TOPICS["motion"]
        (t3, s3, d3), = TOPICS["ring"]
        topics = (
            "<wstop:TopicSet>"
            f'<tns1:VideoSource><MotionAlarm wstop:topic="true">{desc(s1, d1)}</MotionAlarm></tns1:VideoSource>'
            "<tns1:RuleEngine><CellMotionDetector>"
            f'<Motion wstop:topic="true">{desc(s2, d2)}</Motion></CellMotionDetector></tns1:RuleEngine>'
            "<tns1:Device><Trigger>"
            f'<DigitalInput wstop:topic="true">{desc(s3, d3)}</DigitalInput></Trigger></tns1:Device>'
            "</wstop:TopicSet>")
        return ("<tev:GetEventPropertiesResponse>"
                "<tev:TopicNamespaceLocation>http://www.onvif.org/onvif/ver10/topics/topicns.xml</tev:TopicNamespaceLocation>"
                f"<wsnt:FixedTopicSet>true</wsnt:FixedTopicSet>{topics}"
                "<wsnt:TopicExpressionDialect>http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet</wsnt:TopicExpressionDialect>"
                "<wsnt:TopicExpressionDialect>http://docs.oasis-open.org/wsn/t-1/TopicExpression/Concrete</wsnt:TopicExpressionDialect>"
                "<tev:MessageContentFilterDialect>http://www.onvif.org/ver10/tev/messageContentFilter/ItemFilter</tev:MessageContentFilterDialect>"
                "<tev:MessageContentSchemaLocation>http://www.onvif.org/onvif/ver10/schema/onvif.xsd</tev:MessageContentSchemaLocation>"
                "</tev:GetEventPropertiesResponse>")

    def e_CreatePullPointSubscription(self, op, req):
        ttl = parse_timeout(findtext(op, "InitialTerminationTime"), 60)
        sub = Subscription(ttl)
        self.bus.subs[sub.id] = sub
        self.bus.initialize(sub)
        log.info("pull-point subscription %s created (%ss)", sub.id, int(ttl))
        return ("<tev:CreatePullPointSubscriptionResponse><tev:SubscriptionReference>"
                f"<wsa:Address>{self.base}/onvif/events/sub/{sub.id}</wsa:Address></tev:SubscriptionReference>"
                f"<wsnt:CurrentTime>{iso(utcnow())}</wsnt:CurrentTime>"
                f"<wsnt:TerminationTime>{iso(sub.expires)}</wsnt:TerminationTime>"
                "</tev:CreatePullPointSubscriptionResponse>")

    def _sub(self, req) -> Subscription:
        sid = req.match_info.get("sub")
        sub = self.bus.subs.get(sid or "")
        if sub is None:
            raise LookupError("unknown or expired subscription")
        return sub

    async def e_PullMessages(self, op, req):
        sub = self._sub(req)
        timeout = min(parse_timeout(findtext(op, "Timeout"), 10), 60)
        limit = int(findtext(op, "MessageLimit", "100") or 100)
        sub.expires = max(sub.expires, time.time() + timeout + 60)
        if not sub.queue:
            sub.event.clear()
            try:
                await asyncio.wait_for(sub.event.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        msgs, sub.queue[:] = sub.queue[:limit], sub.queue[limit:]
        if not sub.queue:
            sub.event.clear()
        return (f"<tev:PullMessagesResponse><tev:CurrentTime>{iso(utcnow())}</tev:CurrentTime>"
                f"<tev:TerminationTime>{iso(sub.expires)}</tev:TerminationTime>{''.join(msgs)}"
                "</tev:PullMessagesResponse>")

    def e_Renew(self, op, req):
        sub = self._sub(req)
        sub.expires = time.time() + parse_timeout(findtext(op, "TerminationTime"), 60)
        return (f"<wsnt:RenewResponse><wsnt:TerminationTime>{iso(sub.expires)}</wsnt:TerminationTime>"
                f"<wsnt:CurrentTime>{iso(utcnow())}</wsnt:CurrentTime></wsnt:RenewResponse>")

    def e_Unsubscribe(self, op, req):
        sub = self._sub(req)
        self.bus.subs.pop(sub.id, None)
        return "<wsnt:UnsubscribeResponse/>"

    def e_SetSynchronizationPoint(self, op, req):
        subs = [self._sub(req)] if req.match_info.get("sub") else list(self.bus.subs.values())
        for sub in subs:
            self.bus.initialize(sub)
        return "<tev:SetSynchronizationPointResponse/>"


# --------------------------------------------------------------------------- WS-Discovery

MCAST = ("239.255.255.250", 3702)
WSD_NS = ('xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
          'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
          'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
          'xmlns:dn="http://www.onvif.org/ver10/network/wsdl" '
          'xmlns:tds="http://www.onvif.org/ver10/device/wsdl"')


class Discovery(asyncio.DatagramProtocol):
    def __init__(self, onvif: Onvif):
        self.o = onvif
        self.transport = None

    def _match(self) -> str:
        o = self.o
        return (f"<a:EndpointReference><a:Address>urn:uuid:{o.uuid}</a:Address></a:EndpointReference>"
                "<d:Types>dn:NetworkVideoTransmitter tds:Device</d:Types>"
                f"<d:Scopes>{' '.join(o.scopes())}</d:Scopes>"
                f"<d:XAddrs>{o.base}/onvif/device_service</d:XAddrs><d:MetadataVersion>1</d:MetadataVersion>")

    def _msg(self, action, to, body, relates="") -> bytes:
        rel = f"<a:RelatesTo>{relates}</a:RelatesTo>" if relates else ""
        return (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {WSD_NS}><s:Header>'
                f"<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>{rel}<a:To>{to}</a:To>"
                f"<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/{action}</a:Action>"
                f"</s:Header><s:Body>{body}</s:Body></s:Envelope>").encode()

    def connection_made(self, transport):
        self.transport = transport
        self.hello()

    def hello(self):
        msg = self._msg("Hello", "urn:schemas-xmlsoap-org:ws:2005:04:discovery",
                        f"<d:Hello>{self._match()}</d:Hello>")
        self.transport.sendto(msg, MCAST)

    def datagram_received(self, data, addr):
        text = data.decode(errors="ignore")
        if "Probe" not in text or "ProbeMatches" in text:
            return
        types = re.search(r"<(?:\w+:)?Types[^>]*>([^<]*)<", text)
        if types and types.group(1).strip() and not re.search(
                r"NetworkVideoTransmitter|Device\b", types.group(1)):
            return
        mid = re.search(r"<(?:\w+:)?MessageID[^>]*>([^<]+)<", text)
        reply = self._msg("ProbeMatches",
                          "http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous",
                          f"<d:ProbeMatches><d:ProbeMatch>{self._match()}</d:ProbeMatch></d:ProbeMatches>",
                          mid.group(1).strip() if mid else "")
        self.transport.sendto(reply, addr)
        log.debug("answered WS-Discovery probe from %s", addr[0])


def discovery_socket(iface_ip: str) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    s.bind(("", MCAST[1]))
    mreq = struct.pack("4s4s", socket.inet_aton(MCAST[0]), socket.inet_aton(iface_ip))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface_ip))
    s.setblocking(False)
    return s


# --------------------------------------------------------------------------- glue

class DoorbellMapper:
    """Maps Dahua event codes to ONVIF motion / doorbell state."""

    RING_CODES = {"Invite", "DoorBell", "CallSnap"}

    def __init__(self, bus: EventBus, cfg: dict):
        self.bus = bus
        self.hold = float(cfg["doorbell"]["hold_seconds"])
        self.extra = set(cfg["doorbell"].get("extra_codes") or [])
        self._release: asyncio.TimerHandle | None = None

    def __call__(self, code, action, index, data):
        log.debug("dahua event Code=%s action=%s index=%s data=%s", code, action, index, data)
        if code == "VideoMotion":
            if action == "Start":
                self.bus.set("motion", True)
            elif action == "Stop":
                self.bus.set("motion", False)
            return
        ring = False
        if code == "BackKeyLight":
            try:
                ring = int(data.get("State", -1)) in (1, 2)
            except (TypeError, ValueError):
                pass
        elif code in self.RING_CODES or code in self.extra:
            ring = action in ("Start", "Pulse")
        if ring:
            self.ring()

    def ring(self):
        self.bus.set("ring", True)
        if self._release:
            self._release.cancel()
        self._release = asyncio.get_running_loop().call_later(
            self.hold, self.bus.set, "ring", False)


async def main(path: str):
    cfg = load_config(path)
    logging.basicConfig(level=getattr(logging, str(cfg["log_level"]).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    d = cfg["dahua"]
    dahua = Dahua(d)
    info = await dahua.probe()
    log.info("device: %s serial=%s firmware=%s streams=%s",
             info["model"], info["serial"] or "?", info["firmware"] or "?", info["streams"] or "defaults")

    ip = cfg["onvif"]["advertise_ip"] or local_ip_towards(d["host"])
    bus = EventBus()
    onvif = Onvif(cfg, info, dahua, bus, ip)

    app = web.Application()
    onvif.routes(app)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", onvif.port).start()
    log.info("ONVIF device at %s/onvif/device_service (uuid %s)", onvif.base, onvif.uuid)

    tasks = [asyncio.create_task(bus.reaper())]
    if d["events"]:
        tasks.append(asyncio.create_task(dahua.events(DoorbellMapper(bus, cfg))))
    if cfg["onvif"]["discovery"]:
        try:
            sock = discovery_socket(ip)
            await asyncio.get_running_loop().create_datagram_endpoint(lambda: Discovery(onvif), sock=sock)
            log.info("WS-Discovery listening on udp/3702")
        except OSError as e:
            log.warning("WS-Discovery disabled (%s) - add the camera by IP:%s instead", e, onvif.port)
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "config.yaml"))
    except KeyboardInterrupt:
        pass
