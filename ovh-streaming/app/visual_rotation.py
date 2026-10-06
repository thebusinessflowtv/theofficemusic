import time
from datetime import datetime, timezone

SLOTS=("twitch","kick")
INTERVAL_SECONDS=1800
STATUS_NAME="visual-rotation-status.json"


def _iso(ts=None):
    dt=datetime.fromtimestamp(ts or time.time(),timezone.utc)
    return dt.isoformat().replace("+00:00","Z")


def tick(state, read_json, atomic_json, apply_command):
    status_path=state/"agent"/STATUS_NAME
    services={}
    for slot in SLOTS:
        base=state/slot
        h=read_json(base/"health.json",{}) or {}
        d=read_json(base/"desired.json",{}) or {}
        v=read_json(base/"visual-health.json",{}) or {}
        services[slot]=(h,d,v)

    # Never touch a stream unless both publishers explicitly advertise
    # persistent hot-swap readiness.
    for slot,(h,d,v) in services.items():
        if str(h.get("status") or "")!="live" or h.get("hot_swap") is not True or not h.get("encoder_pid"):
            return

    st=read_json(status_path,{}) or {}
    urls={slot:str(d.get("loop_url") or h.get("loop_url") or "") for slot,(h,d,v) in services.items()}
    if not all(urls.values()):
        return

    # First boot records the currently playing visual and waits for one
    # deliberate hot-swap to a distinct second visual.
    if not st:
        atomic_json(status_path,{
            "status":"waiting_second_visual",
            "interval_seconds":INTERVAL_SECONDS,
            "first_urls":urls,
            "captured_first_at":_iso(),
            "rtmp_restart":False,
            "publisher_restart":False,
        })
        return

    if st.get("status")=="waiting_second_visual":
        first=st.get("first_urls") or {}
        changed=all(urls.get(slot) and urls.get(slot)!=first.get(slot) for slot in SLOTS)
        ready=all(
            str(v.get("status") or "")=="streaming" and str(v.get("loop_url") or "")==urls.get(slot)
            for slot,(h,d,v) in services.items()
        )
        if not changed or not ready:
            return
        now=time.time()
        atomic_json(status_path,{
            "status":"armed",
            "interval_seconds":INTERVAL_SECONDS,
            "current_index":1,
            "visuals":[first,urls],
            "captured_first_at":st.get("captured_first_at"),
            "captured_second_at":_iso(now),
            "last_switch_at":_iso(now),
            "next_switch_epoch":now+INTERVAL_SECONDS,
            "next_switch_at":_iso(now+INTERVAL_SECONDS),
            "rtmp_restart":False,
            "publisher_restart":False,
        })
        return

    visuals=st.get("visuals") or []
    if len(visuals)!=2:
        return

    if st.get("status")=="switching":
        target=int(st.get("target_index") or 0)
        expected=visuals[target]
        before=st.get("before") or {}
        for slot,(h,d,v) in services.items():
            b=before.get(slot) or {}
            if b and (
                str(h.get("encoder_pid"))!=str(b.get("encoder_pid"))
                or int(h.get("restarts") or 0)!=int(b.get("restarts") or 0)
                or d.get("generation")!=b.get("generation")
            ):
                st.update({"status":"safety_stop","error":slot+" publisher identity changed","updated_at":_iso()})
                atomic_json(status_path,st)
                return
        complete=all(
            str(d.get("loop_url") or "")==str(expected.get(slot) or "")
            and str(v.get("status") or "")=="streaming"
            and str(v.get("loop_url") or "")==str(expected.get(slot) or "")
            for slot,(h,d,v) in services.items()
        )
        if complete:
            now=time.time()
            st.update({
                "status":"armed",
                "current_index":target,
                "last_switch_at":_iso(now),
                "next_switch_epoch":now+INTERVAL_SECONDS,
                "next_switch_at":_iso(now+INTERVAL_SECONDS),
                "updated_at":_iso(now),
            })
            st.pop("target_index",None)
            st.pop("before",None)
            atomic_json(status_path,st)
        return

    if st.get("status")!="armed":
        return

    now=time.time()
    if now<float(st.get("next_switch_epoch") or 0):
        return

    current=int(st.get("current_index") or 0)
    target=1-current
    expected=visuals[target]
    before={}
    for slot,(h,d,v) in services.items():
        before[slot]={
            "encoder_pid":h.get("encoder_pid"),
            "restarts":int(h.get("restarts") or 0),
            "generation":d.get("generation"),
        }

    # set_visual changes visual_revision only; generation/RTMP publisher are
    # intentionally preserved.
    for slot in SLOTS:
        apply_command({
            "id":f"visual-rotation-{slot}-{int(now)}",
            "action":"set_visual",
            "runtime_slot":slot,
            "platform":slot,
            "loop_url":str(expected.get(slot) or ""),
            "source":"mediaforge-visual-rotation",
            "requested_at":_iso(now),
        })

    st.update({
        "status":"switching",
        "target_index":target,
        "before":before,
        "switch_started_at":_iso(now),
        "updated_at":_iso(now),
    })
    atomic_json(status_path,st)
