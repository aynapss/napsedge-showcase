# Phase 9C -- Private-Only Access to Streamlit (and the API)

**Decision (Anthony, 2026-10-09; readiness section 16 item 5):** Streamlit is **private only**: reachable through an **SSH tunnel** (or a private network), with **no public login page**. The API stays private the same way. No reverse proxy, OIDC or public ingress is used, and no `X-Forwarded-For` is trusted.

**Acceptance (readiness section 15, 9C):** "Streamlit is verifiably unreachable by an unauthenticated party in the target deployment environment." This has two halves, both to be run **on the server** once it exists:
1. the on-host check, `deploy/app/check-private-exposure.sh`;
2. the off-host check from your own PC (section 3).

**Status:** prepared and rehearsed locally (section 4). **Not verified in the target environment** (the server is deferred).

## 1. How it stays private

| Layer | Setting | Where |
|---|---|---|
| Port binding | API `127.0.0.1:8000`, Streamlit `127.0.0.1:8501`; Discord and the database publish no port | `compose.yaml` (tested by `tests/regression/test_deployment_container_contract.py`) |
| Server firewall | DigitalOcean **Cloud Firewall**: inbound **TCP 22 only** (SSH, ideally from your home IP), everything else denied | set at provisioning |
| Access | an SSH tunnel from your PC; nothing listens publicly | section 2 |
| API docs | `/v1/docs` and `/v1/openapi.json` are unauthenticated; they stay private only because the API is loopback-bound | never expose port 8000 |

## 2. Using Streamlit (once the server exists)

From PowerShell on your PC (the SSH key you added to DigitalOcean, `%USERPROFILE%\.ssh\id_ed25519`):
```powershell
ssh -N -L 8501:127.0.0.1:8501 root@<droplet-ip>
```
Leave it running, then open **http://localhost:8501** in your browser. Close the tunnel with Ctrl+C. For the API, use `-L 8000:127.0.0.1:8000` the same way.

## 3. Verifying it in the target environment

**On the server:**
```bash
sh /opt/napsedge/deploy/app/check-private-exposure.sh
```
Expect `RESULT: private`. Any `FAIL` line names the port or container that is exposed. This half is read-only.

**From your own PC, WITHOUT the tunnel** (both must fail: time out or be refused):
```powershell
curl.exe --max-time 5 http://<droplet-ip>:8501/_stcore/health
curl.exe --max-time 5 http://<droplet-ip>:8000/v1/health
```
Then, **with** the tunnel running, `curl.exe http://localhost:8501/_stcore/health` must return `ok`.

Record the three results in the checkpoint. Only then is 9C accepted.

## 4. Local rehearsal (2026-10-09, this PC, Docker Desktop)

The real compose stack (`api` + `streamlit`, project `napsedge9c`) ran with dummy settings and the throwaway SQLite database.

**Reachability:**

| Probe | Result |
|---|---|
| `127.0.0.1:8000/v1/health` and `127.0.0.1:8501/_stcore/health` from this PC | **200** (reachable locally, as intended) |
| the same ports via this PC's LAN address `192.168.1.152` (how another device on the network would try) | **unreachable** (000) |
| `docker ps` publications | only `127.0.0.1` bindings; `check-private-exposure.sh`: **RESULT: private** |
| from another container via `host.docker.internal` | **reachable** -- a **Docker Desktop artifact**: Desktop forwards loopback-published ports to its own containers. On a Linux server a `127.0.0.1` binding is not reachable that way, and containers on the same host are trusted parts of the deployment anyway. The server-side checks in section 3 are the ones that count. |

**The check script itself:** offline cases with fake `ss`/`docker`:
- private: exit 0;
- Streamlit published on `0.0.0.0`: exit 1;
- API on IPv6 `:::`: exit 1;
- IPv6 loopback only: exit 0.

**Clean-up:** the stack was removed (`docker compose -p napsedge9c down`), leaving no containers, networks or volumes.

**Not rehearsed:**
- a real SSH tunnel (it needs the server);
- the socket half of the check on Windows (no `ss`; it runs on the Linux server).
