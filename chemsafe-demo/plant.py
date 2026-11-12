"""
plant.py - ChemSafe simulation core
====================================

Simulates a small continuous stirred-tank reactor (CSTR) making a product
via an exothermic (heat-releasing) reaction that also generates acid as a
by-product. The whole point of the project is to show three engineering
disciplines working together in one control system:

  CHEMICAL   - the reactor itself: an exothermic reaction whose rate roughly
               doubles every ~10 C (Arrhenius behaviour), producing acid over
               time, with safe operating limits on temperature / pressure / pH.
  MECHANICAL - the equipment that moves and cools material: a feed pump, a
               cooling-water valve on the reactor jacket, a caustic (NaOH)
               dosing pump for neutralisation, and a pressure relief valve.
  ELECTRONICS- the simulated sensors. Real plants measure with 4-20 mA
               transmitters wired to a microcontroller (Arduino / ESP32).
               Those signals are never clean, so every sensor here reports
               the true process value plus Gaussian noise, exactly like an
               ADC reading a noisy analogue channel.

The control logic closes the loop:

  * Cooling valve  -> proportional controller holding the reactor at 80 C.
  * Caustic dosing -> pH controller dosing NaOH to counteract the acid the
                      reaction produces, holding pH near neutral (7).
  * Pressure trip  -> critical pressure trips the feed pump and opens the
                      relief valve; a restart interlock only re-starts the
                      pump once pressure has been safely low for a while.
  * Random disturbances (cooling failures, feed surges, acid slugs,
    downstream blockages) so that WARNING / CRITICAL alarms occur naturally
    during a 10-minute run - useful for the demo and the report.
"""

import math
import random
from collections import deque
from datetime import datetime

# ---------------------------------------------------------------------------
# CHEMICAL ENGINEERING - safe operating envelope
# ---------------------------------------------------------------------------
# Every plant has a safe operating envelope written down by the process
# engineers. Outside WARNING you alert the operator; outside CRITICAL the
# safety system acts on its own.
SAFE_LIMITS = {
    "temperature": {"warning": 85.0, "critical": 95.0, "unit": "C"},
    "pressure": {"warning": 2.5, "critical": 3.0, "unit": "bar"},
    "ph": {
        "warn_low": 6.5, "warn_high": 7.5,
        "crit_low": 6.0, "crit_high": 8.0,
        "unit": "pH",
    },
    "flow": {"warn_high": 58.0, "unit": "L/min"},
}

TEMP_SETPOINT = 80.0   # C   - reaction runs best here
PH_SETPOINT = 7.0      # neutral - acid by-product must be neutralised
PRESSURE_RESTART = 2.2 # bar - interlock: pump may restart below this
RESTART_HOLD_S = 8.0   # s   - ...and it must stay low for this long


class Plant:
    """One simulated chemical plant. Call step(dt) once per second."""

    def __init__(self, seed=None):
        if seed is not None:
            random.seed(seed)

        # --- true (hidden) process state ----------------------------------
        self.temperature = 72.0     # C, reactor contents
        self.pressure = 1.8         # bar
        self.ph = 7.05              # pH of reactor contents
        self.flow = 0.0             # L/min feed flow

        # --- MECHANICAL ENGINEERING - equipment state ----------------------
        self.feed_pump_on = True            # feed pump contactor
        self.cooling_valve_pct = 0.0        # 0-100 % cooling-water valve
        self.caustic_dose_pct = 0.0         # 0-100 % NaOH dosing pump
        self.relief_valve_open = False      # pressure relief valve
        self.cooling_efficiency = 1.0       # 1.0 healthy, 0.0 = pump failed
        self.flow_offset = 0.0              # disturbance: feed surge
        self.pressure_offset = 0.0          # disturbance: downstream blockage

        # --- control / safety bookkeeping ----------------------------------
        self.tripped = False                # pressure trip latched?
        self.trip_time = 0.0                # elapsed time of the trip
        self.safe_since = None              # when pressure fell below restart
        self.pump_start_elapsed = 0.0       # when the pump last started

        # --- disturbance scheduler -----------------------------------------
        self.active_disturbance = None      # (kind, seconds_remaining)
        self.next_disturbance_in = random.uniform(45, 75)

        # --- history / logging ---------------------------------------------
        self.elapsed = 0.0
        self.temp_history = deque(maxlen=300)   # last 5 min for the trend chart
        self.alarm_events = deque(maxlen=200)   # timestamped alarm history
        self.active_alarm_ids = set()
        self._stats = {
            k: {"min": float("inf"), "max": float("-inf"), "sum": 0.0, "n": 0}
            for k in ("temperature", "pressure", "ph", "flow")
        }

    # -----------------------------------------------------------------------
    # Disturbances - things that go wrong in real plants
    # -----------------------------------------------------------------------
    def _maybe_start_disturbance(self):
        if self.active_disturbance is not None:
            return
        self.next_disturbance_in -= 1.0
        if self.next_disturbance_in > 0:
            return
        kind = random.choices(
            ["cooling_failure", "feed_surge", "acid_surge", "blockage"],
            weights=[30, 25, 25, 20],
        )[0]
        if kind == "cooling_failure":
            # Cooling-water pump failure: the jacket gets almost no flow and
            # the exotherm runs away. Long failures push past CRITICAL.
            duration = random.uniform(12, 20)
            self.cooling_efficiency = 0.0
        elif kind == "feed_surge":
            # Feed pump surge: too much reactant, pressure and heat climb.
            duration = random.uniform(10, 15)
            self.flow_offset = random.uniform(20, 28)
        elif kind == "acid_surge":
            # Off-spec feed: a slug of acid hits the reactor.
            duration = 1.0
            self.ph -= random.uniform(0.5, 0.95)
        else:  # blockage
            # Downstream blockage: pressure backs up toward the trip point.
            duration = random.uniform(15, 25)
            self.pressure_offset = random.uniform(0.5, 0.9)
        self.active_disturbance = (kind, duration)
        self.next_disturbance_in = random.uniform(55, 95)

    def _advance_disturbance(self, dt):
        if self.active_disturbance is None:
            return
        kind, remaining = self.active_disturbance
        remaining -= dt
        if remaining <= 0:
            # disturbance over: equipment recovers
            if kind == "cooling_failure":
                self.cooling_efficiency = 1.0
            elif kind == "feed_surge":
                self.flow_offset = 0.0
            elif kind == "blockage":
                self.pressure_offset = 0.0
            self.active_disturbance = None
        else:
            self.active_disturbance = (kind, remaining)

    # -----------------------------------------------------------------------
    # One simulation step
    # -----------------------------------------------------------------------
    def step(self, dt=1.0):
        self.elapsed += dt
        self._maybe_start_disturbance()
        self._advance_disturbance(dt)

        # -- MECHANICAL: feed pump and flow ---------------------------------
        # The pump moves reactant into the reactor. Flow does not change
        # instantly - the line has inertia - so it lerps toward its target.
        flow_target = (40.0 + self.flow_offset) if self.feed_pump_on else 0.0
        # a tripped pump spins down fast; a started pump ramps up gently
        flow_rate = 0.2 if self.feed_pump_on else 0.5
        self.flow += (flow_target - self.flow) * min(1.0, flow_rate * dt)

        # -- CHEMICAL: exothermic reaction ----------------------------------
        # Reaction rate ~ reactant flow, and roughly doubles every 10 C
        # (simplified Arrhenius). Each unit of reaction releases heat AND
        # produces a little acid, which is why the pH controller must keep
        # dosing caustic while the plant runs.
        # the Arrhenius factor saturates at high temperature - the reaction
        # becomes reagent-limited, so heat release cannot grow without bound
        arrhenius = min(2.0 ** ((self.temperature - 80.0) / 10.0), 6.0)
        rate = (self.flow / 40.0) * arrhenius
        heat_in = 6.5 * rate

        # -- MECHANICAL: cooling-water valve on the jacket -------------------
        # Cooling duty scales with valve opening and the temperature gap to
        # the 15 C cooling water, times the (possibly failed) pump efficiency.
        cooling = (self.cooling_valve_pct / 100.0) * 0.9 \
            * (self.temperature - 15.0) * self.cooling_efficiency
        losses = 0.06 * (self.temperature - 25.0)  # heat leak to ambient
        self.temperature += (heat_in - cooling - losses) * 0.1 * dt
        # physical ceiling: above ~150 C the mixture boils/decomposes and the
        # temperature plateaus instead of climbing forever
        self.temperature = min(self.temperature, 150.0)

        # -- CHEMICAL: acid by-product and neutralisation --------------------
        # The reaction produces acid: pH falls while the plant runs.
        # The dosing pump adds NaOH, driving pH back up. Correct direction:
        # acid pushes pH DOWN, caustic pushes pH UP, controller holds pH ~ 7.
        self.ph += (-0.004 * rate + 0.020 * (self.caustic_dose_pct / 100.0)) * dt
        self.ph = min(14.0, max(0.0, self.ph))

        # -- MECHANICAL: pressure --------------------------------------------
        # Pressure follows temperature (vapour pressure) and flow (line
        # fill), plus any blockage. The relief valve vents to a safe 1 bar.
        p_target = 0.5 + 0.025 * (self.temperature - 25.0) \
            + 0.012 * self.flow + self.pressure_offset
        self.pressure += (p_target - self.pressure) * min(1.0, 0.3 * dt)
        if self.relief_valve_open:
            self.pressure += (1.0 - self.pressure) * min(1.0, 0.8 * dt)

        # -- ELECTRONICS: noisy sensors --------------------------------------
        # What the microcontroller actually reads: true value + noise, like a
        # 4-20 mA transmitter on an ADC pin. Controllers and alarms below use
        # the SENSED values, never the hidden true ones - just like real life.
        t_s = self.temperature + random.gauss(0, 0.3)
        p_s = self.pressure + random.gauss(0, 0.02)
        ph_s = self.ph + random.gauss(0, 0.05)
        f_s = self.flow + random.gauss(0, 0.8)

        # -- CONTROL: cooling valve holds 80 C (proportional) ----------------
        self.cooling_valve_pct = min(100.0, max(0.0, 5.5 + 2.5 * (t_s - TEMP_SETPOINT)))

        # -- CONTROL: pH holds neutral by dosing caustic ---------------------
        self.caustic_dose_pct = min(100.0, max(0.0, 400.0 * (PH_SETPOINT - ph_s)))

        # -- SAFETY: pressure trip and restart interlock ---------------------
        if not self.tripped and p_s >= SAFE_LIMITS["pressure"]["critical"]:
            # CRITICAL pressure: trip the feed pump, open the relief valve.
            self.tripped = True
            self.trip_time = self.elapsed
            self.feed_pump_on = False
            self.relief_valve_open = True
            self.pressure_offset = 0.0  # venting clears the blockage
            self._log_event("CRITICAL",
                            "Pressure trip - feed pump tripped, relief valve opened")
        if self.tripped:
            # Interlock: the pump may only restart once pressure has stayed
            # below the restart threshold for a safe holding time AND the
            # reactor has cooled back below the temperature warning limit -
            # feeding fresh reactant into an overheated reactor would just
            # re-trip the plant.
            if p_s < PRESSURE_RESTART and t_s < SAFE_LIMITS["temperature"]["warning"]:
                if self.safe_since is None:
                    self.safe_since = self.elapsed
                if self.elapsed - self.safe_since >= RESTART_HOLD_S:
                    self.tripped = False
                    self.relief_valve_open = False
                    self.feed_pump_on = True
                    self.pump_start_elapsed = self.elapsed
                    self.safe_since = None
                    self._log_event("INFO",
                                    "Interlock cleared - feed pump restarted")
            else:
                self.safe_since = None

        # -- ALARMS on the sensed values -------------------------------------
        alarms = self._evaluate_alarms(t_s, p_s, ph_s, f_s)

        # -- statistics and trend history ------------------------------------
        for key, val in (("temperature", t_s), ("pressure", p_s),
                         ("ph", ph_s), ("flow", f_s)):
            st = self._stats[key]
            st["min"] = min(st["min"], val)
            st["max"] = max(st["max"], val)
            st["sum"] += val
            st["n"] += 1
        self.temp_history.append(round(t_s, 2))

        return self._state(t_s, p_s, ph_s, f_s, alarms)

    # -----------------------------------------------------------------------
    # Alarm evaluation with hysteresis (raise at limit, clear just inside it)
    # -----------------------------------------------------------------------
    def _evaluate_alarms(self, t, p, ph, f):
        lim = SAFE_LIMITS
        checks = [
            ("temp_crit", "CRITICAL", t >= lim["temperature"]["critical"],
             t >= lim["temperature"]["critical"] - 1.0,
             "Reactor temperature CRITICAL (%.1f C)" % t),
            ("temp_warn", "WARNING", t >= lim["temperature"]["warning"],
             t >= lim["temperature"]["warning"] - 1.0,
             "Reactor temperature high (%.1f C)" % t),
            ("press_crit", "CRITICAL", p >= lim["pressure"]["critical"],
             p >= lim["pressure"]["critical"] - 0.1,
             "Reactor pressure CRITICAL (%.2f bar)" % p),
            ("press_warn", "WARNING", p >= lim["pressure"]["warning"],
             p >= lim["pressure"]["warning"] - 0.1,
             "Reactor pressure high (%.2f bar)" % p),
            ("ph_low_crit", "CRITICAL", ph <= lim["ph"]["crit_low"],
             ph <= lim["ph"]["crit_low"] + 0.1,
             "Reactor pH CRITICALLY low (%.2f)" % ph),
            ("ph_low_warn", "WARNING", ph <= lim["ph"]["warn_low"],
             ph <= lim["ph"]["warn_low"] + 0.1,
             "Reactor pH low (%.2f)" % ph),
            ("ph_high_warn", "WARNING", ph >= lim["ph"]["warn_high"],
             ph >= lim["ph"]["warn_high"] - 0.1,
             "Reactor pH high (%.2f)" % ph),
            ("flow_warn", "WARNING", f >= lim["flow"]["warn_high"],
             f >= lim["flow"]["warn_high"] - 2.0,
             "Feed flow high (%.1f L/min)" % f),
        ]
        active = []
        for alarm_id, level, raise_cond, hold_cond, message in checks:
            if alarm_id in self.active_alarm_ids:
                if not hold_cond:
                    self.active_alarm_ids.discard(alarm_id)
                    self._log_event("CLEARED", message.split(" (")[0] + " - returned to normal")
                    continue
            elif raise_cond:
                self.active_alarm_ids.add(alarm_id)
                self._log_event(level, message)
            if alarm_id in self.active_alarm_ids:
                active.append({"id": alarm_id, "level": level, "message": message})
        return active

    def _log_event(self, level, message):
        self.alarm_events.appendleft({
            "elapsed_s": round(self.elapsed, 1),
            "wall_time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        })

    # -----------------------------------------------------------------------
    # State snapshots for the web layer
    # -----------------------------------------------------------------------
    def _state(self, t, p, ph, f, alarms):
        status = "NORMAL"
        if any(a["level"] == "CRITICAL" for a in alarms):
            status = "CRITICAL"
        elif alarms:
            status = "WARNING"
        return {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "elapsed_s": round(self.elapsed, 1),
            "status": status,
            "sensors": {
                "temperature": round(t, 1),
                "pressure": round(p, 2),
                "ph": round(ph, 2),
                "flow": round(f, 1),
            },
            "equipment": {
                "feed_pump_on": self.feed_pump_on,
                "cooling_valve_pct": round(self.cooling_valve_pct, 0),
                "caustic_dose_pct": round(self.caustic_dose_pct, 0),
                "relief_valve_open": self.relief_valve_open,
            },
            "alarms": {"active": alarms},
            "events": list(self.alarm_events)[:50],
            "history": {"temperature": list(self.temp_history)},
            "limits": SAFE_LIMITS,
            "setpoints": {"temperature": TEMP_SETPOINT, "ph": PH_SETPOINT},
        }

    def report(self):
        """Run summary for /api/report - handy for the assignment write-up."""
        stats = {}
        for key, st in self._stats.items():
            stats[key] = {
                "min": round(st["min"], 2) if st["n"] else None,
                "max": round(st["max"], 2) if st["n"] else None,
                "avg": round(st["sum"] / st["n"], 2) if st["n"] else None,
            }
        events = list(self.alarm_events)
        return {
            "uptime_s": round(self.elapsed, 1),
            "stats": stats,
            "alarm_event_counts": {
                "critical": sum(1 for e in events if e["level"] == "CRITICAL"),
                "warning": sum(1 for e in events if e["level"] == "WARNING"),
                "cleared": sum(1 for e in events if e["level"] == "CLEARED"),
            },
            "events": events,
            "log_file": "logs/plant_log.csv",
        }
