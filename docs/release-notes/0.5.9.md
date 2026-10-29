## Fixed

- **Log warnings every minute.** Since 0.5.5, each heartbeat rendered the template conditions inside `choose` and `if` steps to find the entities they read, without trigger data, so templates using `trigger` (such as `trigger.id`) logged *'trigger' is undefined* warnings, many per minute. Those templates can't be checked anyway, so they're no longer rendered.
