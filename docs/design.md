# Design

The service uses a small Python controller, a static web console, and a tiny Go HTTP discarder helper. The controller owns configuration, the daily schedule, worker lifecycle, metrics snapshots, source resolution, and adaptive worker counts. Public HTTP workers are long-running `discarder` processes that force IPv4 and stream response bodies to `io.Discard`; Steam workers are optional `steamcmd` processes using anonymous login.

Traffic distribution is connection based: base workers are calculated as `line_count * connections_per_line`, and worker line indexes rotate evenly across configured lines. The controller samples container RX bytes every second and grows workers by one line group when the 60-second average is below 90% of `target_mbps`, up to `line_count * max_connections_per_line`; `max_connections_per_line` is hard-capped at 12. Public HTTP source assignment spreads workers across resolved IPv4 endpoints to avoid piling most connections onto a single remote IP.

Runtime configuration is stored in `/data/config.json` and can be changed from the web console. Docker Compose exposes the same parameters as environment variables for first boot and scripted deployment. The default network uses macvlan with IP `192.168.1.233` and gateway `192.168.1.1`; deployment scans `.233-.240` and selects the first available IP.
