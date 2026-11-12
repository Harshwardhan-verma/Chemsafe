# ChemSafe - Smart Chemical Plant Monitoring System

An interdisciplinary software project simulating a chemical reactor with live
monitoring, alarms, and automatic safety responses. A background simulation
of an exothermic (heat-releasing) reaction drives a Flask web dashboard that
shows sensor readings, equipment states, a temperature trend chart, and a
timestamped alarm history, while logging every reading to CSV.

## How to run

```bash
pip install -r requirements.txt
python app.py
```

Then open http://localhost:5000 in any browser.

Let it run for a few minutes: random disturbances (cooling failures, feed
surges, acid slugs, blockages) push the plant into WARNING and CRITICAL
states, and the control system responds automatically - exactly what you want
to demo and screenshot for the report.

## Files

| File | Purpose |
| --- | --- |
| `plant.py` | Simulation core: reactor physics, equipment, sensors, control logic, alarms |
| `app.py` | Flask server: 1-second simulation loop, CSV logging, JSON API |
| `static/index.html` | Live dashboard (sensor cards, equipment states, trend chart, alarm history) |
| `logs/plant_log.csv` | Data log, one row per second (created on first run) |

## API

| Endpoint | Returns |
| --- | --- |
| `/api/state` | Live sensor values, equipment states, active alarms, recent events, trend data |
| `/api/report` | Run summary: min/max/avg per sensor, alarm counts, alarm event list |

## How the three engineering disciplines are covered

| Discipline | Where it lives in the system |
| --- | --- |
| Chemical Engineering | The reactor model in `plant.py`: an exothermic reaction whose rate rises with temperature (simplified Arrhenius), producing acid over time. Safe operating limits for temperature (85/95 C), pressure (2.5/3.0 bar), and pH (6.5-7.5) define the safe operating envelope. The pH controller neutralises the acid by-product with caustic to hold pH near 7. |
| Mechanical Engineering | The equipment: feed pump, cooling-water valve on the reactor jacket, caustic (NaOH) dosing pump, and pressure relief valve. Flow inertia, cooling duty vs. valve opening, and pressure build-up/venting are all modelled. Critical pressure trips the feed pump and opens the relief valve; an interlock restarts the pump only after pressure stays safe. |
| Electronics Engineering | The simulated sensors: every measurement is the true value plus Gaussian noise, like 4-20 mA transmitters read by an Arduino/ESP32 ADC. Controllers and alarms run on the noisy sensed values, never the hidden true values, just like a real embedded monitoring system. |

## Control and safety logic

- **Cooling valve** - proportional controller holding the reactor at the 80 C setpoint.
- **Caustic dosing** - pH controller dosing NaOH to counteract the acid the reaction produces, holding pH near 7.
- **Pressure trip + interlock** - critical pressure (3.0 bar) trips the feed pump and opens the relief valve; the pump restarts automatically only after pressure stays below 2.2 bar for 8 seconds.
- **Alarms** - WARNING and CRITICAL thresholds per sensor, with hysteresis, a live alarm banner, and a timestamped alarm history.

## Extension ideas

- Add a PID controller (instead of proportional-only) and compare stability in the report.
- Add more unit operations: a separator, a heat exchanger with its own loop, a storage tank level.
- Persist alarm history to a small SQLite database and add a per-day report page.
- Add user-configurable alarm limits in the dashboard (write back to the API).
- Run the sensor noise model on a real ESP32 and publish readings over MQTT.
- Add authentication and multi-user roles (operator vs. supervisor).

## Demo mode

Use the dashboard buttons for a repeatable professor demo, without waiting for
random disturbances. This is an educational simulation, not a real plant controller.

- **Trigger emergency** starts a fresh run at 80 C with steady feed and a failed
  cooling-water pump for 120 seconds. Temperature passes WARNING (85 C) at about
  10-15 seconds and CRITICAL (95 C) at about 25 seconds (sensor noise varies slightly).
  The cooling valve opens, but with failed cooling the temperature keeps rising.
  If pressure reaches 3.0 bar, the existing feed-pump trip and relief valve respond.
- **Acid surge** starts a fresh run with pH at 5.7, immediately showing a critical
  pH alarm and increased automatic caustic dosing.
- **Reset plant** returns to normal operation at 80 C and pH 7. It clears the
  on-screen trend, alarm history and run statistics; previous CSV log rows remain.

Each demo starts a fresh run so it is repeatable. Random disturbances are delayed
for 10 minutes after a demo/reset. The normal simulation advances one simulated
second per real second. No edits to plant.py are required.

POST `/api/disturbance` accepts JSON `{"kind":"cooling_failure"}` (the default)
or `{"kind":"acid_surge"}`. POST `/api/reset` restores the normal demo baseline.
