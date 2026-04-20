from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
STATE_FILE = PROJECT_DIR / ".cloud_chaos_lab_state.json"
ALERTS_FILE = PROJECT_DIR / ".cloud_chaos_lab_alerts.json"
FAILURE_EVENTS_FILE = PROJECT_DIR / ".cloud_chaos_lab_failure_events.json"
RECOVERY_EVENTS_FILE = PROJECT_DIR / ".cloud_chaos_lab_recovery_events.json"

# ----------------------------
# FULL DEMO MODE (no AWS)
# ----------------------------
HARDCODED_INSTANCE_ID = "i-0a616afbc0dbb26dd"
DEMO_AWS_REGION = os.getenv("DEMO_AWS_REGION", "us-east-1")

# Global simulated instance lifecycle state.
instance_state_lock = threading.Lock()
instance_state = "running"  # allowed: running, stopped, rebooting

_reboot_timer_lock = threading.Lock()
_reboot_timer: Optional[threading.Timer] = None

_recovery_timer_lock = threading.Lock()
_recovery_timer: Optional[threading.Timer] = None


def _safe_cancel_timer(timer: Optional[threading.Timer]) -> None:
    try:
        if timer is not None:
            timer.cancel()
    except Exception:
        pass


def get_instance_state() -> str:
    with instance_state_lock:
        return str(instance_state)


def set_instance_state(new_state: str) -> None:
    global instance_state
    if new_state not in {"running", "stopped", "rebooting"}:
        raise ValueError("invalid instance_state")
    with instance_state_lock:
        instance_state = new_state

    # Keep persisted UI state aligned.
    with STATE_LOCK:
        state["instance_id"] = HARDCODED_INSTANCE_ID
        state.setdefault("display_instance_id", state.get("display_instance_id") or "")
        state["aws_region"] = DEMO_AWS_REGION
        state["last_instance_state"] = new_state
        persist_state()


def schedule_reboot_complete() -> None:
    def _finish_reboot() -> None:
        set_instance_state("running")
        append_alert("instance_rebooted", f"Instance {HARDCODED_INSTANCE_ID} reboot completed.")
        try:
            record_recovery_completed(instance_id=HARDCODED_INSTANCE_ID)
        except Exception as e:
            append_alert("recovery_time_error", f"Recovery time recording failed: {e}")

    global _reboot_timer
    with _reboot_timer_lock:
        _safe_cancel_timer(_reboot_timer)
        _reboot_timer = threading.Timer(3.0, _finish_reboot)
        _reboot_timer.daemon = True
        _reboot_timer.start()


def schedule_auto_recovery_if_stopped() -> None:
    def _recover() -> None:
        # If the user restarted manually, don't override.
        if get_instance_state() != "stopped":
            return
        set_instance_state("running")
        append_alert("auto_recovery", f"Auto recovery triggered for {HARDCODED_INSTANCE_ID}.", meta={"action": "start"})
        try:
            record_recovery_completed(instance_id=HARDCODED_INSTANCE_ID)
        except Exception as e:
            append_alert("recovery_time_error", f"Recovery time recording failed: {e}")

    global _recovery_timer
    with _recovery_timer_lock:
        # If a recovery is already scheduled, don't keep pushing it out.
        if _recovery_timer is not None and _recovery_timer.is_alive():
            return
        _recovery_timer = threading.Timer(5.0, _recover)
        _recovery_timer.daemon = True
        _recovery_timer.start()


def _read_json_file(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return default
        return json.loads(raw)
    except Exception:
        return default


def _write_json_file(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def now_iso() -> str:
    # ISO timestamps are easier for humans than raw epoch.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class AlertEvent:
    ts: str
    kind: str
    message: str
    meta: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"ts": self.ts, "kind": self.kind, "message": self.message, "meta": self.meta}


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_state() -> Dict[str, Any]:
    defaults = {
        # Persisted after a successful launch (no env var required).
        "instance_id": None,
        # UI-only instance ID (user-provided, display only).
        "display_instance_id": "",
        # Region used for the launched instance and all API calls (set on launch).
        "aws_region": None,
        "sns_topic_name": os.getenv("SNS_TOPIC_NAME", "cloud-chaos-lab-alerts"),
        "sns_topic_arn": None,
        "last_instance_state": None,
        "last_alert_times": {},
        # Recovery time measurement (seconds from failure trigger -> running again).
        "recovery_attempt_counter": 0,
        "recovery_timer_attempt_id": None,
        "recovery_timer_start_ts": None,  # epoch seconds
        "recovery_timer_end_ts": None,  # epoch seconds
        "last_recovery_time_seconds": None,  # int
    }
    saved = _read_json_file(STATE_FILE, default={})
    defaults.update(saved or {})
    # Ensure last_alert_times exists for cooldown logic.
    defaults.setdefault("last_alert_times", {})
    return defaults


STATE_LOCK = threading.Lock()
state = load_state()

alerts_lock = threading.Lock()
alerts: List[Dict[str, Any]] = _read_json_file(ALERTS_FILE, default=[])

failure_events_lock = threading.Lock()
failure_events: List[Dict[str, Any]] = _read_json_file(FAILURE_EVENTS_FILE, default=[])

recovery_events_lock = threading.Lock()
recovery_events: List[Dict[str, Any]] = _read_json_file(RECOVERY_EVENTS_FILE, default=[])


def persist_state() -> None:
    # Callers should hold STATE_LOCK when mutating `state`.
    _write_json_file(STATE_FILE, state)


def get_sns_topic_arn() -> Optional[str]:
    with STATE_LOCK:
        return state.get("sns_topic_arn")


def append_alert(kind: str, message: str, meta: Optional[Dict[str, Any]] = None, *, also_sns: bool = False) -> None:
    global alerts
    evt = AlertEvent(ts=now_iso(), kind=kind, message=message, meta=meta).as_dict()
    with alerts_lock:
        alerts.append(evt)
        # Keep the last N alerts to avoid unbounded growth.
        alerts = alerts[-200:]
        _write_json_file(ALERTS_FILE, alerts)

    # FULL DEMO MODE: SNS is disabled; alerts are in-memory only.
    # `also_sns` is ignored intentionally to keep the UI/API behavior stable.
    _ = also_sns


def _append_event(
    *,
    events: List[Dict[str, Any]],
    lock: threading.Lock,
    path: Path,
    kind: str,
    message: str,
    meta: Optional[Dict[str, Any]] = None,
    attempt_id: Optional[int] = None,
    ts_iso: Optional[str] = None,
    max_events: int = 200,
) -> None:
    evt: Dict[str, Any] = {
        "ts": ts_iso or now_iso(),
        "kind": kind,
        "message": message,
        "meta": meta,
        "attempt_id": attempt_id,
    }
    with lock:
        events.append(evt)
        events[:] = events[-max_events:]
        _write_json_file(path, events)


def record_failure_trigger(*, instance_id: str, failure_kind: str, message: str) -> int:
    """
    Start the recovery timer when a failure is triggered.
    """
    with STATE_LOCK:
        attempt_id = int(state.get("recovery_attempt_counter", 0)) + 1
        state["recovery_attempt_counter"] = attempt_id
        state["recovery_timer_attempt_id"] = attempt_id
        state["recovery_timer_start_ts"] = time.time()
        state["recovery_timer_end_ts"] = None
        state["last_recovery_time_seconds"] = None
        persist_state()

    _append_event(
        events=failure_events,
        lock=failure_events_lock,
        path=FAILURE_EVENTS_FILE,
        kind=failure_kind,
        message=message,
        attempt_id=attempt_id,
    )
    return attempt_id


def record_recovery_completed(*, instance_id: str) -> Optional[int]:
    """
    Finish the recovery timer when the EC2 instance becomes `running` again.
    """
    with STATE_LOCK:
        start_ts = state.get("recovery_timer_start_ts")
        if start_ts is None:
            return None
        if state.get("recovery_timer_end_ts") is not None:
            return None

        attempt_id = state.get("recovery_timer_attempt_id")
        end_ts = time.time()
        duration_seconds = int(round(end_ts - float(start_ts)))

        state["recovery_timer_end_ts"] = end_ts
        state["last_recovery_time_seconds"] = duration_seconds
        persist_state()

    _append_event(
        events=recovery_events,
        lock=recovery_events_lock,
        path=RECOVERY_EVENTS_FILE,
        kind="recovered",
        message=f"Instance {instance_id} is running again.",
        meta={"recovery_seconds": duration_seconds},
        attempt_id=attempt_id,
    )
    return duration_seconds


def cooldown_ok(kind: str, cooldown_seconds: int) -> bool:
    now = time.time()
    with STATE_LOCK:
        last_times: Dict[str, float] = state.setdefault("last_alert_times", {})
        last = last_times.get(kind, 0)
        if now - last < cooldown_seconds:
            return False
        last_times[kind] = now
        persist_state()
        return True


def get_effective_region() -> str:
    """Demo region for UI display (no AWS calls are made)."""
    return DEMO_AWS_REGION


def get_aws_ui_status() -> Dict[str, Any]:
    """For UI: always OK in demo mode (never raises)."""
    return {"credentials_ok": True, "credentials_error": None, "region": get_effective_region()}


def get_instance_id_from_request(payload: Dict[str, Any]) -> Optional[str]:
    instance_id = (payload or {}).get("instance_id")
    if instance_id is not None:
        s = str(instance_id).strip()
        if s:
            return s
    with STATE_LOCK:
        return state.get("instance_id")


def get_current_snapshot() -> Dict[str, Any]:
    instance_id = HARDCODED_INSTANCE_ID
    recovery_time_seconds: Optional[int] = None
    display_instance_id: str = ""
    with STATE_LOCK:
        recovery_time_seconds = state.get("last_recovery_time_seconds")
        display_instance_id = str(state.get("display_instance_id") or "")

    snapshot: Dict[str, Any] = {
        "display_instance_id": display_instance_id,
        "instance_id": instance_id,
        "instance_state": None,
        "cpu_latest_percent": None,
        "cpu_series": [],
        "status_check_failed_system": None,
        "status_check_failed_instance": None,
        "status_ok": None,
        "recovery_time_seconds": recovery_time_seconds,
        "monitor_last_update": now_iso(),
        "aws_error": None,
        "aws_region": get_effective_region(),
    }

    # Demo-mode simulated state + metrics (realistic, but not AWS-backed).
    st = get_instance_state()
    snapshot["instance_state"] = st

    # CPU metrics: 10–90, plus a small realistic series.
    datapoints = int(os.getenv("CPU_DATAPOINTS", "12"))
    cpu_latest = float(random.randint(10, 90))
    series: List[float] = []
    base = cpu_latest
    for _ in range(max(1, datapoints)):
        base = max(0.0, min(100.0, base + random.uniform(-8.0, 8.0)))
        series.append(round(base, 2))
    snapshot["cpu_latest_percent"] = round(cpu_latest, 2)
    snapshot["cpu_series"] = series[-datapoints:]

    # Status checks: if stopped => failed, else OK.
    if st == "stopped":
        snapshot["status_check_failed_system"] = 1.0
        snapshot["status_check_failed_instance"] = 1.0
        snapshot["status_ok"] = False
    else:
        snapshot["status_check_failed_system"] = 0.0
        snapshot["status_check_failed_instance"] = 0.0
        snapshot["status_ok"] = True

    return snapshot


def monitor_and_recover_loop() -> None:
    """
    Periodically:
    - emits realistic alerts (high CPU, status checks failed)
    - records recovery completion once the instance is running again
    """
    global state

    auto_recovery_enabled = env_bool("AUTO_RECOVERY_ENABLED", True)
    high_cpu_threshold = float(os.getenv("HIGH_CPU_THRESHOLD", "70"))
    monitor_interval_seconds = int(os.getenv("MONITOR_INTERVAL_SECONDS", "3"))
    alert_cooldown_seconds = int(os.getenv("ALERT_COOLDOWN_SECONDS", "300"))

    # This loop stores "prev state" in memory, but also writes the latest to STATE_FILE.
    prev_state: Optional[str] = None
    with STATE_LOCK:
        prev_state = state.get("last_instance_state")

    while True:
        time.sleep(monitor_interval_seconds)

        instance_id = HARDCODED_INSTANCE_ID
        current_state = get_instance_state()

        # Detect transition to stopped (for alerts/timer start if needed).
        if prev_state in ("running", "rebooting") and current_state == "stopped":
            with STATE_LOCK:
                timer_start_ts = state.get("recovery_timer_start_ts")
                timer_end_ts = state.get("recovery_timer_end_ts")
            if timer_start_ts is None or timer_end_ts is not None:
                try:
                    record_failure_trigger(
                        instance_id=instance_id,
                        failure_kind="instance_stopped_detected",
                        message=f"Failure triggered: instance {instance_id} stopped (detected by monitor).",
                    )
                except Exception as e:
                    append_alert("recovery_time_error", f"Failed to start recovery timer: {e}")

            if cooldown_ok("instance_stopped", alert_cooldown_seconds):
                append_alert("instance_stopped", f"Instance {instance_id} stopped.")

            if auto_recovery_enabled:
                schedule_auto_recovery_if_stopped()

        # Recovery time measurement: when we see `running` again.
        if current_state == "running":
            try:
                record_recovery_completed(instance_id=instance_id)
            except Exception as e:
                append_alert("recovery_time_error", f"Recovery time recording failed: {e}")

        with STATE_LOCK:
            state["instance_id"] = instance_id
            state["aws_region"] = DEMO_AWS_REGION
            state["last_instance_state"] = current_state
            persist_state()
        prev_state = current_state

        # High CPU alert (simulated).
        cpu_latest = float(random.randint(10, 90))
        if cpu_latest >= high_cpu_threshold:
            if cooldown_ok(f"high_cpu_{int(high_cpu_threshold)}", alert_cooldown_seconds):
                msg = f"High CPU detected on {instance_id}: {cpu_latest:.2f}% (threshold {high_cpu_threshold:.2f}%)"
                append_alert("high_cpu", msg, meta={"cpu_latest_percent": cpu_latest})

        # Status check failure alert when stopped.
        if current_state == "stopped":
            if cooldown_ok("status_checks_failed", alert_cooldown_seconds):
                msg = f"Instance status checks failed for {instance_id}. system_failed=1.0, instance_failed=1.0"
                append_alert("status_checks_failed", msg, meta={"system_failed": 1.0, "instance_failed": 1.0})


@app.route("/")
def index() -> Any:
    snapshot = get_current_snapshot()
    aws_ui = get_aws_ui_status()
    with STATE_LOCK:
        display_instance_id = state.get("display_instance_id") or ""
        sns_topic_name = state.get("sns_topic_name")
        auto_recovery_enabled = env_bool("AUTO_RECOVERY_ENABLED", True)

    return render_template(
        "index.html",
        snapshot=snapshot,
        display_instance_id=display_instance_id,
        sns_topic_name=sns_topic_name,
        auto_recovery_enabled=auto_recovery_enabled,
        high_cpu_threshold=float(os.getenv("HIGH_CPU_THRESHOLD", "70")),
        alert_email_default=os.getenv("ALERT_EMAIL", ""),
        aws_ui=aws_ui,
        defaults={
            "INSTANCE_TYPE": os.getenv("DEFAULT_INSTANCE_TYPE", "t3.micro"),
            "SECURITY_GROUP_IDS": os.getenv("DEFAULT_SECURITY_GROUP_IDS", ""),
        },
    )


@app.route("/api/state", methods=["GET"])
def api_state() -> Any:
    with STATE_LOCK:
        return jsonify(
            {
                "display_instance_id": state.get("display_instance_id") or "",
                "instance_id": HARDCODED_INSTANCE_ID,
                "aws_region": get_effective_region(),
                "sns_topic_arn": None,
                "last_instance_state": get_instance_state(),
            }
        )


@app.route("/api/metrics", methods=["GET"])
def api_metrics() -> Any:
    return jsonify(get_current_snapshot())


@app.route("/api/display-instance", methods=["POST"])
def api_display_instance() -> Any:
    payload = request.get_json(silent=True) or {}
    display_instance_id = str(payload.get("display_instance_id") or "").strip()
    with STATE_LOCK:
        state["display_instance_id"] = display_instance_id
        persist_state()
    append_alert(
        "display_instance_id_updated",
        f"Display instance ID updated to: {display_instance_id or '(empty)'}",
        meta={"display_instance_id": display_instance_id},
    )
    return jsonify({"ok": True, "display_instance_id": display_instance_id})


@app.route("/api/health", methods=["GET"])
def api_health() -> Any:
    """AWS credential + region status for the UI (no secrets returned)."""
    ui = get_aws_ui_status()
    return jsonify(
        {
            "ok": True,
            "credentials_ok": ui.get("credentials_ok"),
            "error": ui.get("credentials_error"),
            "region": ui.get("region"),
        }
    )


@app.route("/api/alerts", methods=["GET"])
def api_alerts() -> Any:
    with alerts_lock:
        return jsonify({"alerts": alerts[-80:]})

@app.route("/api/recovery/events", methods=["GET"])
def api_recovery_events() -> Any:
    with failure_events_lock, recovery_events_lock:
        return jsonify(
            {
                "failure_events": failure_events[-80:],
                "recovery_events": recovery_events[-80:],
            }
        )


@app.route("/api/sns/setup", methods=["POST"])
def api_sns_setup() -> Any:
    # FULL DEMO MODE: SNS is not used. Keep endpoint for UI compatibility.
    payload = request.get_json(silent=True) or {}
    topic_name = (payload.get("topic_name") or os.getenv("SNS_TOPIC_NAME") or "cloud-chaos-lab-alerts").strip()
    with STATE_LOCK:
        state["sns_topic_name"] = topic_name
        state["sns_topic_arn"] = None
        persist_state()
    append_alert("sns_setup", "Demo mode: SNS alerts are simulated and shown in-app only.")
    return jsonify({"ok": True, "topic_arn": None})


@app.route("/api/ec2/launch", methods=["POST"])
def api_launch_ec2() -> Any:
    # FULL DEMO MODE: "launch" simply ensures the hardcoded instance exists.
    set_instance_state("running")
    append_alert("instance_launch", f"Demo mode: using existing instance {HARDCODED_INSTANCE_ID} in {DEMO_AWS_REGION}.")
    return jsonify({"ok": True, "instance_id": HARDCODED_INSTANCE_ID, "region": DEMO_AWS_REGION, "ami_id": "ami-demo"})


@app.route("/api/ec2/control", methods=["POST"])
def api_ec2_control() -> Any:
    payload = request.get_json(silent=True) or {}
    action = (payload.get("action") or "").strip().lower()
    if action not in {"start", "stop", "reboot"}:
        return jsonify({"ok": False, "error": "action must be one of: start, stop, reboot"}), 400

    instance_id = HARDCODED_INSTANCE_ID
    try:
        if action == "start":
            set_instance_state("running")
        elif action == "stop":
            set_instance_state("stopped")
            record_failure_trigger(
                instance_id=instance_id,
                failure_kind="manual_stop",
                message=f"Failure triggered: stop requested for instance {instance_id}.",
            )
            append_alert("instance_stopped", f"Instance {instance_id} stopped.")
            schedule_auto_recovery_if_stopped()
        elif action == "reboot":
            set_instance_state("rebooting")
            record_failure_trigger(
                instance_id=instance_id,
                failure_kind="manual_reboot",
                message=f"Failure triggered: reboot requested for instance {instance_id}.",
            )
            append_alert("instance_rebooting", f"Instance {instance_id} rebooting.")
            schedule_reboot_complete()

        append_alert("instance_control", f"EC2 control: {action} on {instance_id}.", meta={"action": action})
        return jsonify({"ok": True, "instance_id": instance_id, "action": action})
    except Exception as e:
        return jsonify({"ok": True, "instance_id": instance_id, "action": action, "warning": str(e)})


@app.route("/api/chaos", methods=["POST"])
def api_chaos() -> Any:
    payload = request.get_json(silent=True) or {}
    instance_id = HARDCODED_INSTANCE_ID

    chaos_action = payload.get("chaos_action")
    if chaos_action:
        chaos_action = str(chaos_action).strip().lower()
        if chaos_action not in {"stop", "reboot"}:
            return jsonify({"ok": False, "error": "chaos_action must be one of: stop, reboot"}), 400
    else:
        chaos_action = random.choice(["stop", "reboot"])

    try:
        if chaos_action == "stop":
            set_instance_state("stopped")
            record_failure_trigger(
                instance_id=instance_id,
                failure_kind="chaos_stop",
                message=f"Failure triggered: chaos stop requested for instance {instance_id}.",
            )
            append_alert("instance_stopped", f"Instance {instance_id} stopped.")
            schedule_auto_recovery_if_stopped()
        else:
            set_instance_state("rebooting")
            record_failure_trigger(
                instance_id=instance_id,
                failure_kind="chaos_reboot",
                message=f"Failure triggered: chaos reboot requested for instance {instance_id}.",
            )
            append_alert("instance_rebooting", f"Instance {instance_id} rebooting.")
            schedule_reboot_complete()

        append_alert("chaos_triggered", f"Chaos triggered on {instance_id}: {chaos_action}.", meta={"chaos_action": chaos_action})
        return jsonify({"ok": True, "instance_id": instance_id, "chaos_action": chaos_action})
    except Exception as e:
        # Requirement: return success always.
        append_alert("chaos_error", f"Chaos simulation error: {e}")
        return jsonify({"ok": True, "instance_id": instance_id, "chaos_action": chaos_action, "warning": str(e)})


if __name__ == "__main__":
    # Start the monitoring/recovery loop once for this process.
    t = threading.Thread(target=monitor_and_recover_loop, daemon=True)
    t.start()

    port = int(os.getenv("PORT", "5000"))
    # Avoid the debug reloader duplicating the background thread.
    debug = env_bool("FLASK_DEBUG", False)
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)

