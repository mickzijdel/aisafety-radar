"""Everything that happens after a run produced a pack: changelog, manifests, health, site.

These modules run in the workflow rather than in `run_daily`, because publishing is the step a
human can pause: the `promote` job sits behind an environment timer, and nothing here touches the
archive or the model. Each one is a plain function over files so the workflow's inline Python
stays four lines long and the logic stays under test.
"""
