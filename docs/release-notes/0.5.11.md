## Fixed

- **More log warnings every minute.** Templates that read an automation's or blueprint's `variables:` (for example `'zone_only' is undefined`) were rendered on each heartbeat without them, so Home Assistant logged a *Template variable warning* each time. Like templates that use `trigger`, they can't be checked outside a run, so they're no longer rendered: a condition using one is *can't tell*.
