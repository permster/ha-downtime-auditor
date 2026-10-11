This release explains how severity is decided, and says which trigger each finding is about.

## New

- **Rules tab.** The dashboard has a new *Rules* tab that explains how severity is decided, using your current options: where the starting rating comes from (this trigger's rating, the automation's label, otherwise Medium), then each adjustment in the order it's applied, and which ones are off.

## Changed

- **Each finding says which trigger it is**, in words and with its position, for example *2 of 3 · event doorbell_pressed* or *every 5 seconds*. That's shown above *Rate this trigger*, and on each trigger in a finding with several. Reports from before this version show the trigger's type and number.
- **Starting rating and result are told apart.** When a rule changed the rating, the severity line says so, for example *Medium (unrated) → None: the pattern runs again under a minute after Home Assistant started*, with a link to the Rules tab. The rating selector's default now reads *Unrated (starts at Medium)*.
- "Runs again" says *under a minute* instead of *0 min* for fast time patterns.
