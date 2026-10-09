This release cuts findings that wouldn't have done anything, and lets you rate any missed trigger.

## New

- **Time patterns that run again soon are None.** A missed `/15` or `/30` tick is only a delay, because the pattern runs again within minutes of startup. The new option *Time patterns that run again soon* (60 minutes by default; 0 turns it off) sets how soon counts. The dashboard shows when the pattern runs again.
- **Actions are checked, not just conditions.** When an automation's actions start with a `choose` (no default), an `if` (no else) or a condition step, and every one of those would have failed at the missed time, nothing would have run, so the finding is None. For example, a sync whose only `choose` option needs a different time of day.

## Changed

- **Rate any missed trigger.** *Rate this trigger* is now on every missed trigger, not only in findings with several triggers.
- **Fewer "probably failed" conditions.** A condition entity with the same value after startup as before the downtime is taken as unchanged throughout. A garage door that was closed before and is still closed now counts as a real fail, so the finding is None instead of Medium.
