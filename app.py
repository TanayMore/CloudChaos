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

from aws_utils import (
    DEFAULT_AWS_REGION,
    check_credentials,
    cloudwatch_get_instance_status_checks,
    cloudwatch_get_latest_cpu_and_timeseries,
    ec2_control_instance,
    ec2_get_instance_state,
    ec2_launch_instance,
    format_aws_error,
    get_latest_amazon_linux_ami_id,
    make_session,
    sns_ensure_topic_and_subscription,
    sns_send_alert,
    validate_region_name,
)


app = Flask(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
STATE_FILE = PROJECT_DIR / ".cloud_chaos_lab_state.json"
ALERTS_FILE = PROJECT_DIR / ".cloud_chaos_lab_alerts.json"
FAILURE_EVENTS_FILE = PROJECT_DIR / ".cloud_chaos_lab_failure_events.json"
RECOVERY_EVENTS_FILE = PROJECT_DIR / ".cloud_chaos_lab_recovery_events.json"


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

    # Sending to SNS is optional (we don't want to fail core functionality).
    if also_sns:
        topic_arn = get_sns_topic_arn()
        if topic_arn:
            try:
                sns = make_session(region_name=get_effective_region()).client("sns")
                subject = f"Cloud Chaos Lab alert: {kind}"
                sns_send_alert(sns, topic_arn=topic_arn, subject=subject, message=message)
            except Exception as e:
                # Log locally (do not throw).
                with alerts_lock:
                    alerts.append(
                        AlertEvent(
                            ts=now_iso(),
                            kind="sns_error",
                            message=f"Failed to send SNS alert: {e}",
                        ).as_dict()
                    )
                    alerts[:] = alerts[-200:]
                    _write_json_file(ALERTS_FILE, alerts)


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
    """Single region for all AWS calls: saved after launch, else env, else default."""
    with STATE_LOCK:
        saved = state.get("aws_region")
    if saved and str(saved).strip():
        return str(saved).strip()
    return os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or DEFAULT_AWS_REGION


def get_aws_ui_status() -> Dict[str, Any]:
    """For UI: credential check + active region (never raises)."""
    region = get_effective_region()
    try:
        session = make_session(region_name=region)
        ok, err = check_credentials(session)
        return {"credentials_ok": ok, "credentials_error": err, "region": region}
    except Exception as e:
        return {
            "credentials_ok": False,
            "credentials_error": format_aws_error(e),
            "region": region,
        }


def get_instance_id_from_request(payload: Dict[str, Any]) -> Optional[str]:
    instance_id = (payload or {}).get("instance_id")
    if instance_id is not None:
        s = str(instance_id).strip()
        if s:
            return s
    with STATE_LOCK:
        return state.get("instance_id")


def get_current_snapshot() -> Dict[str, Any]:
    instance_id = None
    recovery_time_seconds: Optional[int] = None
    with STATE_LOCK:
        instance_id = state.get("instance_id")
        recovery_time_seconds = state.get("last_recovery_time_seconds")

    snapshot: Dict[str, Any] = {
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

    if not instance_id:
        return snapshot

    session = make_session(region_name=get_effective_region())
    ec2 = session.client("ec2")
    cw = session.client("cloudwatch")
    errs: List[str] = []
    try:
        snapshot["instance_state"] = ec2_get_instance_state(ec2, instance_id)
    except Exception as e:
        snapshot["instance_state"] = None
        errs.append(format_aws_error(e))

    try:
        period = int(os.getenv("CPU_PERIOD_SECONDS", "60"))
        datapoints = int(os.getenv("CPU_DATAPOINTS", "12"))
        cpu = cloudwatch_get_latest_cpu_and_timeseries(
            cw, instance_id=instance_id, period_seconds=period, datapoints=datapoints
        )
        snapshot["cpu_latest_percent"] = cpu["latest_cpu_percent"]
        snapshot["cpu_series"] = cpu["cpu_series"]
    except Exception as e:
        snapshot["cpu_latest_percent"] = None
        snapshot["cpu_series"] = []
        errs.append(format_aws_error(e))

    try:
        checks = cloudwatch_get_instance_status_checks(cw, instance_id=instance_id)
        snapshot["status_check_failed_system"] = checks["status_check_failed_system"]
        snapshot["status_check_failed_instance"] = checks["status_check_failed_instance"]
        snapshot["status_ok"] = checks["status_ok"]
    except Exception as e:
        snapshot["status_check_failed_system"] = None
        snapshot["status_check_failed_instance"] = None
        snapshot["status_ok"] = None
        errs.append(format_aws_error(e))

    if errs:
        snapshot["aws_error"] = " ".join(errs[:3])

    return snapshot


def monitor_and_recover_loop() -> None:
    """
    Periodically:
    - checks instance state
    - sends alerts (stop/restart chaos, high CPU, failed status checks)
    - auto-recover by starting the instance if it's stopped
    """
    global state

    auto_recovery_enabled = env_bool("AUTO_RECOVERY_ENABLED", True)
    high_cpu_threshold = float(os.getenv("HIGH_CPU_THRESHOLD", "70"))
    monitor_interval_seconds = int(os.getenv("MONITOR_INTERVAL_SECONDS", "30"))
    alert_cooldown_seconds = int(os.getenv("ALERT_COOLDOWN_SECONDS", "300"))

    # This loop stores "prev state" in memory, but also writes the latest to STATE_FILE.
    prev_state: Optional[str] = None
    with STATE_LOCK:
        prev_state = state.get("last_instance_state")

    while True:
        time.sleep(monitor_interval_seconds)

        with STATE_LOCK:
            instance_id = state.get("instance_id")
            sns_topic_arn = state.get("sns_topic_arn")

        if not instance_id:
            continue

        session = make_session(region_name=get_effective_region())
        ec2 = session.client("ec2")
        cw = session.client("cloudwatch")

        # 1) Instance state + stop alert
        try:
            current_state = ec2_get_instance_state(ec2, instance_id)
        except Exception:
            current_state = None

        if current_state:
            # Detect transition to stopped.
            if prev_state in ("running", "pending", "stopping") and current_state == "stopped":
                # If we didn't start a recovery timer via an API call, start it now
                # when we *observe* the instance enter `stopped`.
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

                msg = f"Instance {instance_id} stopped."
                if cooldown_ok("instance_stopped", alert_cooldown_seconds):
                    append_alert("instance_stopped", msg, also_sns=bool(sns_topic_arn))

                    # Auto-recovery will start below.

            # Auto-recovery
            if auto_recovery_enabled and current_state == "stopped":
                # Prevent constant retries if AWS is slow to update.
                if cooldown_ok("auto_recovery_start", alert_cooldown_seconds):
                    try:
                        ec2_control_instance(ec2, instance_id, "start")
                        append_alert(
                            "auto_recovery",
                            f"Auto-recovery started instance {instance_id}.",
                            meta={"action": "start"},
                            also_sns=bool(sns_topic_arn),
                        )
                    except Exception as e:
                        append_alert("auto_recovery_error", f"Auto-recovery failed: {e}")

            # Recovery time measurement: when we see `running` again.
            if current_state == "running":
                try:
                    record_recovery_completed(instance_id=instance_id)
                except Exception as e:
                    append_alert("recovery_time_error", f"Recovery time recording failed: {e}")

            with STATE_LOCK:
                state["last_instance_state"] = current_state
                persist_state()

            prev_state = current_state

        # 2) CloudWatch CPU alert
        try:
            period = int(os.getenv("CPU_PERIOD_SECONDS", "60"))
            datapoints = int(os.getenv("CPU_DATAPOINTS", "12"))
            cpu = cloudwatch_get_latest_cpu_and_timeseries(
                cw, instance_id=instance_id, period_seconds=period, datapoints=datapoints
            )
            cpu_latest = cpu.get("latest_cpu_percent")
        except Exception:
            cpu_latest = None

        if cpu_latest is not None and cpu_latest >= high_cpu_threshold:
            if cooldown_ok(f"high_cpu_{int(high_cpu_threshold)}", alert_cooldown_seconds):
                msg = f"High CPU detected on {instance_id}: {cpu_latest:.2f}% (threshold {high_cpu_threshold:.2f}%)"
                append_alert("high_cpu", msg, meta={"cpu_latest_percent": cpu_latest}, also_sns=bool(sns_topic_arn))

        # 3) Instance status check alerts
        try:
            checks = cloudwatch_get_instance_status_checks(cw, instance_id=instance_id)
            sys_failed = checks.get("status_check_failed_system")
            inst_failed = checks.get("status_check_failed_instance")
        except Exception:
            sys_failed = None
            inst_failed = None

        if (sys_failed == 1.0) or (inst_failed == 1.0):
            if cooldown_ok("status_checks_failed", alert_cooldown_seconds):
                msg = (
                    f"Instance status checks failed for {instance_id}. "
                    f"system_failed={sys_failed}, instance_failed={inst_failed}"
                )
                append_alert(
                    "status_checks_failed",
                    msg,
                    meta={"system_failed": sys_failed, "instance_failed": inst_failed},
                    also_sns=bool(sns_topic_arn),
                )


@app.route("/")
def index() -> Any:
    snapshot = get_current_snapshot()
    aws_ui = get_aws_ui_status()
    with STATE_LOCK:
        instance_id = state.get("instance_id")
        sns_topic_name = state.get("sns_topic_name")
        auto_recovery_enabled = env_bool("AUTO_RECOVERY_ENABLED", True)

    return render_template(
        "index.html",
        snapshot=snapshot,
        instance_id=instance_id,
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
                "instance_id": state.get("instance_id"),
                "aws_region": get_effective_region(),
                "sns_topic_arn": state.get("sns_topic_arn"),
                "last_instance_state": state.get("last_instance_state"),
            }
        )


@app.route("/api/metrics", methods=["GET"])
def api_metrics() -> Any:
    return jsonify(get_current_snapshot())


@app.route("/api/health", methods=["GET"])
def api_health() -> Any:
    """AWS credential + region status for the UI (no secrets returned)."""
    ui = get_aws_ui_status()
    return jsonify(
        {
            "ok": bool(ui.get("credentials_ok")),
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
    payload = request.get_json(silent=True) or {}
    email = (payload.get("email") or os.getenv("ALERT_EMAIL") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "Missing alert email. Provide JSON {\"email\": \"...\"} or set ALERT_EMAIL."}), 400

    topic_name = (payload.get("topic_name") or os.getenv("SNS_TOPIC_NAME") or "cloud-chaos-lab-alerts").strip()
    try:
        session = make_session(region_name=get_effective_region())
        sns = session.client("sns")
        topic_arn = sns_ensure_topic_and_subscription(sns, topic_name=topic_name, email=email)
        with STATE_LOCK:
            state["sns_topic_name"] = topic_name
            state["sns_topic_arn"] = topic_arn
            persist_state()

        append_alert("sns_setup", f"SNS topic ready ({topic_name}). Subscription request sent to {email}.")
        return jsonify({"ok": True, "topic_arn": topic_arn})
    except Exception as e:
        return jsonify({"ok": False, "error": format_aws_error(e)}), 500


@app.route("/api/ec2/launch", methods=["POST"])
def api_launch_ec2() -> Any:
    payload = request.get_json(silent=True) or {}
    instance_type = (payload.get("instance_type") or "t3.micro").strip()
    security_group_ids_raw = (payload.get("security_group_ids") or "").strip()
    # Optional overrides for advanced users (not shown in the simplified UI).
    key_name = (payload.get("key_name") or "").strip() or None
    subnet_id = (payload.get("subnet_id") or "").strip() or None
    iam_instance_profile = (payload.get("iam_instance_profile") or "").strip() or None
    tag_name = (payload.get("tag_name") or "CloudChaosLab").strip() or None
    region_override = (payload.get("region") or "").strip() or None

    if not security_group_ids_raw:
        return jsonify({"ok": False, "error": "security_group_ids is required (comma-separated)."}), 400

    security_group_ids = [s.strip() for s in security_group_ids_raw.split(",") if s.strip()]
    if not security_group_ids:
        return jsonify({"ok": False, "error": "No valid security group IDs were parsed."}), 400

    # Region: explicit in request, else current effective region (env/default/saved).
    region = region_override or get_effective_region()
    session = make_session(region_name=region)
    ok, cred_err = check_credentials(session)
    if not ok:
        return jsonify({"ok": False, "error": cred_err or "AWS credentials check failed."}), 401

    try:
        validate_region_name(region)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    try:
        ami_id = get_latest_amazon_linux_ami_id(session)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": format_aws_error(e)}), 400

    try:
        ec2 = session.client("ec2")
        instance_id = ec2_launch_instance(
            ec2,
            ami_id=ami_id,
            instance_type=instance_type,
            key_name=key_name,
            security_group_ids=security_group_ids,
            subnet_id=subnet_id,
            iam_instance_profile=iam_instance_profile,
            tag_name=tag_name,
        )
        with STATE_LOCK:
            state["instance_id"] = instance_id
            state["aws_region"] = region
            state["last_instance_state"] = None
            persist_state()

        append_alert(
            "instance_launch",
            f"Launched instance {instance_id} in {region} using AMI {ami_id}.",
        )
        return jsonify({"ok": True, "instance_id": instance_id, "region": region, "ami_id": ami_id})
    except Exception as e:
        return jsonify({"ok": False, "error": format_aws_error(e)}), 500


@app.route("/api/ec2/control", methods=["POST"])
def api_ec2_control() -> Any:
    payload = request.get_json(silent=True) or {}
    action = (payload.get("action") or "").strip().lower()
    if action not in {"start", "stop", "reboot"}:
        return jsonify({"ok": False, "error": "action must be one of: start, stop, reboot"}), 400

    instance_id = get_instance_id_from_request(payload)
    if not instance_id:
        return jsonify(
            {
                "ok": False,
                "error": "No instance ID yet. Launch an instance from this app first (instance ID is saved automatically).",
            }
        ), 400

    try:
        session = make_session(region_name=get_effective_region())
        ec2 = session.client("ec2")
        ec2_control_instance(ec2, instance_id, action)
        # Treat stopping as the "failure trigger" for recovery time measurement.
        if action == "stop":
            record_failure_trigger(
                instance_id=instance_id,
                failure_kind="manual_stop",
                message=f"Failure triggered: stop requested for instance {instance_id}.",
            )
        append_alert("instance_control", f"EC2 control: {action} on {instance_id}.", meta={"action": action}, also_sns=bool(get_sns_topic_arn()))
        return jsonify({"ok": True, "instance_id": instance_id, "action": action})
    except Exception as e:
        return jsonify({"ok": False, "error": format_aws_error(e)}), 500


@app.route("/api/chaos", methods=["POST"])
def api_chaos() -> Any:
    payload = request.get_json(silent=True) or {}
    instance_id = get_instance_id_from_request(payload)
    if not instance_id:
        return jsonify(
            {
                "ok": False,
                "error": "No instance ID yet. Launch an instance from this app first (instance ID is saved automatically).",
            }
        ), 400

    chaos_action = payload.get("chaos_action")
    if chaos_action:
        chaos_action = str(chaos_action).strip().lower()
        if chaos_action not in {"stop", "reboot"}:
            return jsonify({"ok": False, "error": "chaos_action must be one of: stop, reboot"}), 400
    else:
        chaos_action = random.choice(["stop", "reboot"])

    try:
        session = make_session(region_name=get_effective_region())
        ec2 = session.client("ec2")
        # Chaos engine: stop or reboot.
        ec2_control_instance(ec2, instance_id, chaos_action)

        # Treat chaos stop as the "failure trigger" for recovery time measurement.
        if chaos_action == "stop":
            record_failure_trigger(
                instance_id=instance_id,
                failure_kind="chaos_stop",
                message=f"Failure triggered: chaos stop requested for instance {instance_id}.",
            )

        # Alert immediately when chaos is triggered.
        append_alert(
            "chaos_triggered",
            f"Chaos triggered on {instance_id}: {chaos_action}.",
            meta={"chaos_action": chaos_action},
            also_sns=bool(get_sns_topic_arn()),
        )
        return jsonify({"ok": True, "instance_id": instance_id, "chaos_action": chaos_action})
    except Exception as e:
        return jsonify({"ok": False, "error": format_aws_error(e)}), 500


if __name__ == "__main__":
    # Start the monitoring/recovery loop once for this process.
    t = threading.Thread(target=monitor_and_recover_loop, daemon=True)
    t.start()

    port = int(os.getenv("PORT", "5000"))
    # Avoid the debug reloader duplicating the background thread.
    debug = env_bool("FLASK_DEBUG", False)
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)

