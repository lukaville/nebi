"""Robot link transports.

Layout (dependency direction is top -> bottom):
  - `ucp_codec`      UCP v1 wire constants + segment/SACK codec (pure, no I/O)
  - `ucp`            UCP ARQ/fragmentation/heartbeat engine (`UcpEndpointSession`) and the UDP `UcpClient`
  - `zenoh_proto`    Protobuf normalisation required by the robot's app bridge + dict -> message filling
  - `zenoh_tunnel`   Zenoh-over-UCP (`0xA4..0xA7`) framing and the client-side TCP <-> UCP tunnel
  - `zenoh_security` MD-TS handshake over Zenoh (`app|device/security/session`), client and responder
  - `zenoh_bus`      `ZenohTopicBus`: Protobuf pub/sub + correlated RPC over `eclipse-zenoh`
  - `zenoh_ucp`      backwards-compatible facade re-exporting the Zenoh modules above

Robot-side (simulator) counterparts live in `nebi.sim`.
"""
