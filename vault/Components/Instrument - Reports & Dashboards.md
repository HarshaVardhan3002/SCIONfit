# Instrument - Reports & Dashboards (`scionarena.instrument.report`)

> [report.py](file:///C:/Users/Von/Desktop/PAS_Research_desktop/PAS_Research_desktop/scionfit/src/scionarena/instrument/report.py)

---

## 📌 Role & Responsibilities
Renders self-contained interactive HTML dashboards, terminal ASCII tables, and Markdown/JSON evaluation artifacts.

## 🎨 Interactive HTML Dashboard
* Zero external JavaScript or CSS dependencies.
* Renders interactive SVG time-series charts (link utilisation, path allocation splits, power spectra).
* Color-coded verdict badges:
  * 🟩 `PASS` / `CONFORMANT`
  * 🟨 `PARTIAL` / `WEAK`
  * 🟥 `FAIL` / `FALSE CLAIM` / `MISDECLARED`
  * ⬛ `ABSENT` / `OPEN-LOOP ONLY`
