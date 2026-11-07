## New

- **Acknowledge a finding.** Mark a finding as handled for this report only: **Acknowledge** on the dashboard, or select its issue in Settings → Repairs and **Submit**. Its Repairs issue clears, *Needs attention* stops counting it, and the dashboard greys it out (*Hide acknowledged* hides them). Its rating doesn't change, so the next downtime reports it again; rate it instead to stop it coming back. **Undo acknowledge** reverses it. There's also a `downtime_auditor.acknowledge` action for scripts.
