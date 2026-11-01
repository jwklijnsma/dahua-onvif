"""End-to-end check of dahua-onvif with the onvif-zeep client (python-onvif)."""
import socket, time, urllib.request, uuid
from onvif import ONVIFCamera
from zeep.exceptions import Fault

cam = ONVIFCamera("127.0.0.1", 8000, "nvr", "secret")
info = cam.devicemgmt.GetDeviceInformation()
print("device:", info.Manufacturer, info.Model, info.SerialNumber, info.FirmwareVersion)
print("iface MAC:", cam.devicemgmt.GetNetworkInterfaces()[0].Info.HwAddress)

media = cam.create_media_service()
for p in media.GetProfiles():
    enc = p.VideoEncoderConfiguration
    uri = media.GetStreamUri({"StreamSetup": {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}},
                              "ProfileToken": p.token}).Uri
    snap = media.GetSnapshotUri({"ProfileToken": p.token}).Uri
    print(f"profile {p.token}: {enc.Encoding} {enc.Resolution.Width}x{enc.Resolution.Height}"
          f"@{enc.RateControl.FrameRateLimit}  stream={uri}  snapshot={snap}")
data = urllib.request.urlopen(snap).read()
print("snapshot bytes:", len(data), "jpeg" if data[:2] == b"\xff\xd8" else "NOT JPEG")

events = cam.create_events_service()
props = events.GetEventProperties()
print("topics advertised:", props.TopicSet is not None)
pp = cam.create_pullpoint_service()
seen = []
end = time.time() + 14
while time.time() < end:
    res = pp.PullMessages({"Timeout": "PT5S", "MessageLimit": 20})
    for m in res.NotificationMessage:
        el = m.Message._value_1
        data_items = el.find(".//{*}Data/{*}SimpleItem")
        op = el.get("PropertyOperation")
        topic = data_items.get("Name")
        seen.append((topic, data_items.get("Value"), op))
        print(f"  {op:11} {topic} = {data_items.get('Value')}")
assert any(t == "LogicalState" and v == "true" and o == "Changed" for t, v, o in seen), "no doorbell ring"
assert any(t == "State" and v == "true" and o == "Changed" for t, v, o in seen), "no motion"
print("events OK")

try:
    ONVIFCamera("127.0.0.1", 8000, "nvr", "wrong").devicemgmt.GetDeviceInformation()
    print("AUTH NOT ENFORCED")
except Exception as f:
    print("wrong password rejected:", f)

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(3)
probe = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
         f'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
         f'xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><s:Header><a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>'
         f'<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action></s:Header>'
         f'<s:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></s:Body></s:Envelope>')
s.sendto(probe.encode(), ("127.0.0.1", 3702))
reply = s.recv(65535).decode()
print("discovery:", "ProbeMatches" in reply and "/onvif/device_service" in reply)
