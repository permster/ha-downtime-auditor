This release settles `for:` triggers from what happens after startup, and makes findings easier to read.

## New

- **`for:` triggers are checked after startup.** A state or numeric-state trigger with `for:` fires only if the new value holds that long. Downtime Auditor now watches the entity after startup:
  - if it changed again too soon for the value to have held (for example a clock sensor that ticks every minute, watched for going stale), or only changed after startup, where Home Assistant saw it itself, the finding is dropped;
  - if it still has the value once `for:` has passed, the trigger would have fired: Confirmed;
  - otherwise it stays Probable, and the finding says why.
  It watches for up to 10 minutes after the analysis.

## Changed

- **Clearer findings:**
  - The trigger starts with its type, for example *State: sensor.opnpool_time changes, then holds for 5 min*, with its position and id on a second line.
  - Before/after values are labeled with the entity: *22:56 before the downtime → 22:59 after startup*. The separate `for:` and Range rows are gone, since the trigger says it.
  - **Conditions** lead with the answer (*Would have run*, *Wouldn't have run*) and the check that decided it. For actions, they name the `choose` option that would run, by its alias or by what it does, such as *“Resync pool clock” (option 8) → script.opnpool_resync*. All checks are folded under *Show all checks*, where options for other triggers collapse to one line.
- Repairs issues use the same wording.
