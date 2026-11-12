"""
app.py - ChemSafe web dashboard server
=======================================

Run:
    pip install -r requirements.txt
    python app.py
    open http://localhost:5000

A background thread steps the plant simulation once per second and appends
every reading to logs/plant_log.csv (the data-logging part of the assignment).
The dashboard polls /api/state; /api/report gives a run summary.
"""

import csv
import os
import threading
import time

from flask import Flask, jsonify, request, send_from_directory

from plant import Plant

app = Flask(__name__, static_folder="static")
plant = Plant()
_lock = threading.Lock()
_latest = {}            # most recent plant state
LOG_PATH = os.path.join("logs", "plant_log.csv")

CSV_HEADER = [
    "timestamp", "elapsed_s", "temperature_c", "pressure_bar", "ph",
    "flow_lpm", "feed_pump_on", "cooling_valve_pct", "caustic_dose_pct",
    "relief_valve_open", "status", "active_alarms",
]


def simulation_loop():
    """1-second simulation loop: step the plant, log to CSV, publish state."""
    global _latest
    os.makedirs("logs", exist_ok=True)
    write_header = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(CSV_HEADER)
        while True:
            with _lock:
                state = plant.step(1.0)
                _latest = state
            writer.writerow([
                state["timestamp"], state["elapsed_s"],
                state["sensors"]["temperature"], state["sensors"]["pressure"],
                state["sensors"]["ph"], state["sensors"]["flow"],
                state["equipment"]["feed_pump_on"],
                state["equipment"]["cooling_valve_pct"],
                state["equipment"]["caustic_dose_pct"],
                state["equipment"]["relief_valve_open"],
                state["status"],
                "; ".join(a["message"] for a in state["alarms"]["active"]),
            ])
            f.flush()
            time.sleep(1)


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/state")
def api_state():
    with _lock:
        return jsonify(_latest)


def reset_demo_plant():
    """Called with _lock held. Start a repeatable, normal training run."""
    global plant, _latest
    plant = Plant()
    plant.temperature, plant.pressure = 80.0, 2.35
    plant.ph, plant.flow = 7.0, 40.0
    plant.cooling_valve_pct, plant.caustic_dose_pct = 5.5, 20.0
    plant.next_disturbance_in = 600.0
    _latest = plant.step(0.0)


@app.route("/api/disturbance", methods=["POST"])
def api_disturbance():
    global _latest
    kind = (request.get_json(silent=True) or {}).get("kind", "cooling_failure")
    if kind not in ("cooling_failure", "acid_surge"):
        return jsonify(error="Unknown disturbance"), 400
    with _lock:
        reset_demo_plant()
        if kind == "cooling_failure":
            plant.temperature = 80.0
            plant.cooling_efficiency = 0.0
            plant.active_disturbance = (kind, 120.0)
        else:
            plant.ph = 5.7
            plant.active_disturbance = (kind, 1.0)
        plant._log_event("INFO", "Demo started: " + kind.replace("_", " "))
        _latest = plant.step(0.0)
        return jsonify(_latest)


@app.route("/api/reset", methods=["POST"])
def api_reset():
    with _lock:
        reset_demo_plant()
        return jsonify(_latest)


@app.route("/api/report")
def api_report():
    return jsonify(plant.report())


if __name__ == "__main__":
    threading.Thread(target=simulation_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, threaded=True)
